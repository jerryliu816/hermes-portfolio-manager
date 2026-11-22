"""Backup and restore.

**The database and the source documents are one backup, not two.** The database
records what each document was read to say; the documents are the only way to
re-derive that if the schema changes, a provider improves, or a figure is ever
disputed. Backing up one without the other leaves you with either numbers you
cannot re-check or documents you have to re-import from scratch — so an archive
here always contains both, and a restore refuses an archive missing either.

An archive is a plain tar.gz holding:

    manifest.json     what this is, when, and what it contains
    database.sql      pg_dump, plain SQL so it is readable and greppable
    sources/          the original documents, laid out as data/sources

Plain SQL rather than a custom-format dump because a backup you cannot inspect
without the exact tool that wrote it is a backup you are trusting on faith.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..config import Settings, get_settings

MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "database.sql"
SOURCES_DIR = "sources"
ARCHIVE_VERSION = 1


class BackupError(RuntimeError):
    """A backup or restore could not be completed."""


@dataclass(frozen=True)
class BackupManifest:
    version: int
    created_at: str
    database: str
    source_file_count: int
    source_bytes: int
    app_version: str = "0.1.0"

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2)

    @staticmethod
    def from_json(text: str) -> BackupManifest:
        data = json.loads(text)
        return BackupManifest(
            version=int(data["version"]),
            created_at=str(data["created_at"]),
            database=str(data["database"]),
            source_file_count=int(data["source_file_count"]),
            source_bytes=int(data["source_bytes"]),
            app_version=str(data.get("app_version", "unknown")),
        )


@dataclass(frozen=True)
class BackupResult:
    path: Path
    manifest: BackupManifest

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size


def _pg_env(settings: Settings) -> dict[str, str]:
    """Pass the password by environment, never on the command line.

    A password in argv is visible to every process on the machine.
    """
    return {
        **os.environ,
        "PGPASSWORD": settings.postgres_password.get_secret_value(),
    }


def _require_tool(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise BackupError(
            f"{name} is not installed. It ships with the postgresql-client package; "
            "the application container already has it."
        )
    return found


def create_backup(destination: Path, *, settings: Settings | None = None) -> BackupResult:
    """Write a complete archive: database plus every preserved document."""
    settings = settings or get_settings()
    pg_dump = _require_tool("pg_dump")

    destination = destination.expanduser()
    # A destination is a directory if it already is one, or if it has no
    # extension. Otherwise `portfolio backup create backups/` on a first run
    # writes a *file* named "backups", which is a surprising way to lose a
    # backup.
    if destination.is_dir() or not destination.suffix:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination.mkdir(parents=True, exist_ok=True)
        destination = destination / f"portfolio-backup-{stamp}.tar.gz"
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        dump_path = staging / DATABASE_NAME

        result = subprocess.run(
            [
                pg_dump,
                "--host",
                settings.postgres_host,
                "--port",
                str(settings.postgres_port),
                "--username",
                settings.postgres_user,
                "--dbname",
                settings.postgres_db,
                "--no-owner",
                "--no-privileges",
                # Drop before create, so restoring over an existing database
                # works. Without this a restore fails on the first CREATE TABLE
                # -- which is every restore except onto an empty database, i.e.
                # exactly the case you need one in.
                "--clean",
                "--if-exists",
                "--file",
                str(dump_path),
            ],
            env=_pg_env(settings),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise BackupError(f"pg_dump failed: {result.stderr.strip()[:400]}")

        sources = settings.sources_dir
        staged_sources = staging / SOURCES_DIR
        file_count = 0
        total_bytes = 0
        if sources.is_dir():
            shutil.copytree(sources, staged_sources)
            for path in staged_sources.rglob("*"):
                if path.is_file():
                    file_count += 1
                    total_bytes += path.stat().st_size
        else:
            staged_sources.mkdir()

        manifest = BackupManifest(
            version=ARCHIVE_VERSION,
            created_at=datetime.now(UTC).isoformat(),
            database=settings.postgres_db,
            source_file_count=file_count,
            source_bytes=total_bytes,
        )
        (staging / MANIFEST_NAME).write_text(manifest.to_json())

        with tarfile.open(destination, "w:gz") as archive:
            for name in (MANIFEST_NAME, DATABASE_NAME, SOURCES_DIR):
                archive.add(staging / name, arcname=name)

    # The archive contains original brokerage statements; keep it owner-only.
    destination.chmod(0o600)
    return BackupResult(path=destination, manifest=manifest)


def inspect_backup(archive: Path) -> BackupManifest:
    """Read an archive's manifest without unpacking it.

    Worth doing before a restore: an archive that turns out to be from the
    wrong database is much better discovered now.
    """
    archive = archive.expanduser()
    if not archive.exists():
        raise BackupError(f"no such archive: {archive}")
    try:
        with tarfile.open(archive, "r:gz") as tar:
            member = tar.extractfile(MANIFEST_NAME)
            if member is None:
                raise BackupError(
                    f"{archive} has no {MANIFEST_NAME}; it may not be a portfolio backup"
                )
            try:
                return BackupManifest.from_json(member.read().decode())
            except (json.JSONDecodeError, KeyError, ValueError, UnicodeDecodeError) as exc:
                # A corrupt or foreign manifest must report itself as such, not
                # surface a raw decoder traceback from inside a restore.
                raise BackupError(f"{archive} has an unreadable {MANIFEST_NAME}: {exc}") from exc
    except tarfile.TarError as exc:
        raise BackupError(f"{archive} is not a readable archive: {exc}") from exc


def _is_within(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return True


def _safe_extract(tar: tarfile.TarFile, destination: Path) -> None:
    """Extract, refusing any member that would escape the destination.

    A tar entry may contain `..` or an absolute path. This archive is one we
    wrote, but a restore is exactly when someone hands you a file from
    elsewhere, so the check is unconditional.
    """
    for member in tar.getmembers():
        target = destination / member.name
        if member.name.startswith("/") or not _is_within(destination, target):
            raise BackupError(f"archive member escapes the destination: {member.name!r}")
        if member.issym() or member.islnk():
            raise BackupError(f"archive contains a link, which is not expected: {member.name!r}")
    tar.extractall(destination, filter="data")


def restore_backup(
    archive: Path,
    *,
    settings: Settings | None = None,
    confirm: bool = False,
) -> BackupManifest:
    """Restore an archive over the current database and documents.

    Destructive, so it requires `confirm=True` rather than defaulting to it:
    a restore replaces portfolio state that may be more recent than the backup.
    """
    settings = settings or get_settings()
    psql = _require_tool("psql")
    manifest = inspect_backup(archive)

    if not confirm:
        raise BackupError(
            "restore replaces the current database and stored documents. "
            "Pass confirm=True (or --yes on the command line) to proceed."
        )
    if manifest.version > ARCHIVE_VERSION:
        raise BackupError(
            f"this archive is version {manifest.version}, but this build understands "
            f"up to {ARCHIVE_VERSION}. Upgrade before restoring."
        )

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        with tarfile.open(archive.expanduser(), "r:gz") as tar:
            _safe_extract(tar, staging)

        dump_path = staging / DATABASE_NAME
        if not dump_path.exists():
            raise BackupError(f"archive has no {DATABASE_NAME}")
        staged_sources = staging / SOURCES_DIR
        if not staged_sources.is_dir():
            raise BackupError(
                f"archive has no {SOURCES_DIR}/ directory. The documents and the database "
                "are one backup; restoring only the database would leave readings that "
                "cannot be re-derived."
            )

        result = subprocess.run(
            [
                psql,
                "--host",
                settings.postgres_host,
                "--port",
                str(settings.postgres_port),
                "--username",
                settings.postgres_user,
                "--dbname",
                settings.postgres_db,
                "--set",
                "ON_ERROR_STOP=on",
                "--quiet",
                "--file",
                str(dump_path),
            ],
            env=_pg_env(settings),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise BackupError(f"psql restore failed: {result.stderr.strip()[:400]}")

        target = settings.sources_dir
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(staged_sources, target)
        target.chmod(0o700)
        for path in target.rglob("*"):
            if path.is_file():
                path.chmod(0o600)

    return manifest
