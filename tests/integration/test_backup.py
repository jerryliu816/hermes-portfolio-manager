"""Backup and restore.

The property that matters: the database and the source documents are one
backup. The database records what each document was *read* to say; the
documents are the only way to re-derive that if the schema changes or a
provider improves. An archive with one and not the other is not a backup.
"""

from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from portfolio_manager.config import Settings
from portfolio_manager.extraction.providers.mock_provider import MockExtractionProvider
from portfolio_manager.services.backup import (
    ARCHIVE_VERSION,
    BackupError,
    BackupManifest,
    create_backup,
    inspect_backup,
    restore_backup,
)
from portfolio_manager.services.importer import import_document

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures/extraction/fidelity/fullview_anonymized.csv"
)

#: pg_dump and psql ship with the application container. A developer machine may
#: not have them, and a backup test that fails for that reason is noise rather
#: than a finding.
requires_pg_tools = pytest.mark.skipif(
    shutil.which("pg_dump") is None or shutil.which("psql") is None,
    reason="pg_dump/psql not on PATH (install postgresql-client or libpq)",
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", _env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def with_documents(session: Session, settings: Settings) -> Settings:
    import_document(session, FIXTURE, MockExtractionProvider(), settings=settings)
    session.flush()
    return settings


@requires_pg_tools
def test_an_archive_carries_the_database_and_the_documents_together(
    with_documents: Settings, tmp_path: Path
) -> None:
    result = create_backup(tmp_path / "backup.tar.gz", settings=with_documents)
    with tarfile.open(result.path, "r:gz") as tar:
        names = set(tar.getnames())
    assert "manifest.json" in names
    assert "database.sql" in names
    assert any(name.startswith("sources") for name in names)
    assert result.manifest.source_file_count == 1


@requires_pg_tools
def test_the_archive_is_owner_only(with_documents: Settings, tmp_path: Path) -> None:
    """It contains original brokerage statements."""
    result = create_backup(tmp_path / "backup.tar.gz", settings=with_documents)
    assert oct(result.path.stat().st_mode)[-3:] == "600"


@requires_pg_tools
def test_a_directory_destination_gets_a_timestamped_name(
    with_documents: Settings, tmp_path: Path
) -> None:
    target = tmp_path / "archives"
    result = create_backup(target, settings=with_documents)
    assert result.path.parent == target
    assert result.path.name.startswith("portfolio-backup-")
    assert result.path.name.endswith(".tar.gz")


@requires_pg_tools
def test_the_manifest_is_readable_without_unpacking(
    with_documents: Settings, tmp_path: Path
) -> None:
    """An archive from the wrong database is far better discovered now."""
    result = create_backup(tmp_path / "backup.tar.gz", settings=with_documents)
    manifest = inspect_backup(result.path)
    assert manifest.version == ARCHIVE_VERSION
    assert manifest.database == with_documents.postgres_db
    assert manifest.source_file_count == 1
    assert manifest.created_at.endswith("+00:00")


@requires_pg_tools
def test_the_documents_survive_a_round_trip(with_documents: Settings, tmp_path: Path) -> None:
    """The point of keeping them: byte-identical, so a reading can be re-derived."""
    stored = list(with_documents.sources_dir.rglob("*"))
    original = next(p for p in stored if p.is_file())
    original_bytes = original.read_bytes()

    archive = create_backup(tmp_path / "backup.tar.gz", settings=with_documents).path

    # Destroy the stored documents, then restore them.
    for path in with_documents.sources_dir.rglob("*"):
        if path.is_file():
            path.unlink()

    restore_backup(archive, settings=with_documents, confirm=True)

    restored = [p for p in with_documents.sources_dir.rglob("*") if p.is_file()]
    assert len(restored) == 1
    assert restored[0].read_bytes() == original_bytes
    assert oct(restored[0].stat().st_mode)[-3:] == "600"


@requires_pg_tools
def test_restore_refuses_without_explicit_confirmation(
    with_documents: Settings, tmp_path: Path
) -> None:
    """It replaces state that may be newer than the backup."""
    archive = create_backup(tmp_path / "backup.tar.gz", settings=with_documents).path
    with pytest.raises(BackupError, match="confirm"):
        restore_backup(archive, settings=with_documents)


@requires_pg_tools
def test_restore_refuses_an_archive_missing_its_documents(
    with_documents: Settings, tmp_path: Path
) -> None:
    """Restoring only the database leaves readings that cannot be re-derived."""
    staging = tmp_path / "partial"
    staging.mkdir()
    (staging / "database.sql").write_text("-- empty\n")
    manifest = BackupManifest(
        version=ARCHIVE_VERSION,
        created_at="2026-08-27T00:00:00+00:00",
        database="portfolio",
        source_file_count=0,
        source_bytes=0,
    )
    (staging / "manifest.json").write_text(manifest.to_json())

    archive = tmp_path / "partial.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(staging / "manifest.json", arcname="manifest.json")
        tar.add(staging / "database.sql", arcname="database.sql")

    with pytest.raises(BackupError, match="cannot be re-derived"):
        restore_backup(archive, settings=with_documents, confirm=True)


@requires_pg_tools
def test_a_newer_archive_format_is_refused(with_documents: Settings, tmp_path: Path) -> None:
    """Silently misreading a future format would corrupt state."""
    staging = tmp_path / "future"
    staging.mkdir()
    (staging / "database.sql").write_text("-- empty\n")
    (staging / "sources").mkdir()
    (staging / "manifest.json").write_text(
        BackupManifest(
            version=ARCHIVE_VERSION + 1,
            created_at="2027-01-01T00:00:00+00:00",
            database="portfolio",
            source_file_count=0,
            source_bytes=0,
        ).to_json()
    )
    archive = tmp_path / "future.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in ("manifest.json", "database.sql", "sources"):
            tar.add(staging / name, arcname=name)

    with pytest.raises(BackupError, match="Upgrade before restoring"):
        restore_backup(archive, settings=with_documents, confirm=True)


@requires_pg_tools
def test_an_archive_that_escapes_its_destination_is_refused(
    with_documents: Settings, tmp_path: Path
) -> None:
    """A restore is exactly when someone hands you a file from elsewhere."""
    staging = tmp_path / "evil"
    staging.mkdir()
    (staging / "manifest.json").write_text(
        BackupManifest(
            version=ARCHIVE_VERSION,
            created_at="2026-08-27T00:00:00+00:00",
            database="portfolio",
            source_file_count=0,
            source_bytes=0,
        ).to_json()
    )
    (staging / "database.sql").write_text("-- empty\n")
    (staging / "sources").mkdir()
    payload = staging / "escaped.txt"
    payload.write_text("should not land outside the staging directory")

    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in ("manifest.json", "database.sql", "sources"):
            tar.add(staging / name, arcname=name)
        tar.add(payload, arcname="../escaped.txt")

    with pytest.raises(BackupError, match="escapes the destination"):
        restore_backup(archive, settings=with_documents, confirm=True)
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_a_corrupt_manifest_reports_clearly(tmp_path: Path) -> None:
    """A restore must not surface a raw decoder traceback."""
    archive = tmp_path / "corrupt.tar.gz"
    junk = tmp_path / "manifest.json"
    junk.write_text("this is not json")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(junk, arcname="manifest.json")
    with pytest.raises(BackupError, match="unreadable manifest.json"):
        inspect_backup(archive)


def test_inspecting_a_non_archive_reports_clearly(tmp_path: Path) -> None:
    junk = tmp_path / "notes.txt"
    junk.write_text("this is not a backup")
    with pytest.raises(BackupError, match="not a readable archive"):
        inspect_backup(junk)


def test_inspecting_a_missing_file_reports_clearly(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="no such archive"):
        inspect_backup(tmp_path / "absent.tar.gz")
