"""Wiring this system into a local Hermes installation.

Everything here was verified against the installed Hermes source rather than
assumed:

* `hermes mcp add <name> --url <endpoint>` stores a plain `url` key under
  `mcp_servers` in `~/.hermes/config.yaml`, and the client uses **Streamable
  HTTP** for URL servers by default (`transport: sse` is an opt-in override).
* Skills live at `~/.hermes/skills/<category>/<name>/SKILL.md` with YAML
  frontmatter.
* Webhooks require `platforms.webhook.enabled` in the config and a running
  gateway, which is why the webhook step prints instructions instead of trying
  to configure it.
* The api_server platform (what the Advisor page chats through) is enabled by a
  usable `API_SERVER_KEY` in `~/.hermes/.env` (`gateway/config_env.py`), and
  listens on 127.0.0.1:8642 by default.
* `platform_toolsets.<platform>` given as an explicit list replaces that
  platform's default bundle, and MCP server names in the list pass through
  (`hermes_cli/tools_config.py::_get_platform_tools`). The api_server default,
  `hermes-api-server`, includes terminal and file-writing tools -- too much for
  an agent that reads third-party pages and documents.

The helper prints commands by default rather than running them. Modifying
someone's agent configuration is not something to do silently as a side effect
of a different command.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

HERMES_HOME = Path.home() / ".hermes"
SKILL_SOURCE = Path(__file__).parent / "skill" / "SKILL.md"
SKILL_CATEGORY = "productivity"
SKILL_NAME = "portfolio-review"
ADVISOR_SKILL_SOURCE = Path(__file__).parent / "skill" / "advisor" / "SKILL.md"
ADVISOR_SKILL_NAME = "investment-advisor"
SERVER_NAME = "portfolio-manager"

#: What the advisor may use when reached through the api_server: web research,
#: skills, a scratch todo list, and this server. No terminal, no file writes,
#: no browser control, no messaging -- the content it reads is untrusted.
ADVISOR_TOOLSETS = ("web", "skills", "todo", SERVER_NAME)


@dataclass(frozen=True)
class InstallPlan:
    mcp_command: str
    test_command: str
    skill_target: Path
    webhook_command: str
    hermes_present: bool
    notes: list[str]
    advisor_skill_target: Path = (
        HERMES_HOME / "skills" / "finance" / ADVISOR_SKILL_NAME / "SKILL.md"
    )

    def render(self) -> str:
        lines = [
            "To connect this portfolio to Hermes:",
            "",
            "1. Register the MCP server (uses Streamable HTTP, the client default):",
            f"     {self.mcp_command}",
            f"     {self.test_command}",
            "",
            "2. Install the analyst skill:",
            f"     cp {SKILL_SOURCE} \\",
            f"        {self.skill_target}",
            "",
            "3. Optional - be notified when a new snapshot is imported.",
            "   This needs `platforms.webhook.enabled: true` in ~/.hermes/config.yaml",
            "   and a running `hermes gateway`:",
            f"     {self.webhook_command}",
            "   Then put the URL and secret it prints into .env as",
            "   HERMES_WEBHOOK_URL and HERMES_WEBHOOK_SECRET, and set HERMES_ENABLED=true.",
            "",
            "4. The advisor (the Advisor page chats through Hermes's api_server):",
            "   a. Install the advisor skill:",
            f"        cp {ADVISOR_SKILL_SOURCE} \\",
            f"           {self.advisor_skill_target}",
            "   b. Enable the api_server: put a long random key in ~/.hermes/.env as",
            "        API_SERVER_KEY=<key>",
            "      and the same value in this project's .env as HERMES_API_KEY, with",
            "      HERMES_ENABLED=true. It listens on 127.0.0.1:8642.",
            "   c. Restrict what the advisor can do. It reads news, web pages and your",
            "      documents, which can contain instructions, so it gets no terminal,",
            "      no file writes and no browser. In ~/.hermes/config.yaml:",
            "        platform_toolsets:",
            f"          api_server: [{', '.join(ADVISOR_TOOLSETS)}]",
            "      Leaving it unset gives the api_server the full `hermes-api-server` bundle.",
            "   d. Restart the gateway (`hermes gateway restart`), then check:",
            "        curl -s http://127.0.0.1:8642/health",
            "        portfolio advisor status",
        ]
        if self.notes:
            lines += ["", "Notes:"] + [f"   - {n}" for n in self.notes]
        return "\n".join(lines)


def _load_hermes_config(home: Path) -> dict[str, Any]:
    """The Hermes config, or {} when it is absent or unreadable. Read-only."""
    try:
        loaded = yaml.safe_load((home / "config.yaml").read_text())
    except (OSError, yaml.YAMLError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def advisor_notes(config: dict[str, Any]) -> list[str]:
    """What the existing Hermes config means for the advisor, stated, not fixed."""
    notes: list[str] = []
    web = config.get("web") or {}
    backend = web.get("search_backend") or web.get("backend") if isinstance(web, dict) else None
    if backend:
        notes.append(f"Web search backend: {backend}. The advisor will research with it.")
    else:
        notes.append(
            "No web search backend is set in ~/.hermes/config.yaml (web.backend). Run "
            "`hermes tools` to choose one, or confirm one is detected from your keys; "
            "without it the advisor cannot research current events, and should say so."
        )
    toolsets = (config.get("platform_toolsets") or {}).get("api_server")
    if toolsets is None:
        notes.append(
            "platform_toolsets.api_server is not set, so the api_server would run with "
            "the full tool bundle, terminal included. Set it as in step 4c."
        )
    elif isinstance(toolsets, list):
        risky = sorted({"terminal", "file", "browser", "hermes-api-server"} & set(toolsets))
        if risky:
            notes.append(
                f"platform_toolsets.api_server includes {', '.join(risky)}. The advisor "
                "reads untrusted content; consider step 4c's narrower list."
            )
        if SERVER_NAME not in toolsets and f"mcp-{SERVER_NAME}" not in toolsets:
            notes.append(
                f"platform_toolsets.api_server does not list {SERVER_NAME}; the advisor "
                "would not see the portfolio."
            )
    return notes


def build_plan(api_url: str = "http://localhost:8787") -> InstallPlan:
    # The trailing slash is load-bearing: it is the mount point itself, and
    # some MCP clients do not follow the redirect from the bare path.
    endpoint = f"{api_url.rstrip('/')}/mcp/"
    notes: list[str] = []

    hermes_present = HERMES_HOME.is_dir()
    if not hermes_present:
        notes.append(
            f"No Hermes installation found at {HERMES_HOME}. These steps will only "
            "work once Hermes is installed; the portfolio app itself does not need it."
        )
    if shutil.which("hermes") is None:
        notes.append(
            "The `hermes` command is not on PATH, so the commands below may need to be "
            "run from wherever it is installed."
        )
    notes.append(
        "Hermes gets read-only access. The only writes it can make are recording a "
        "recommendation, your decision about one, and a note of a view or preference "
        "you asked it to remember; it cannot change holdings."
    )
    if hermes_present:
        notes += advisor_notes(_load_hermes_config(HERMES_HOME))

    return InstallPlan(
        mcp_command=f"hermes mcp add {SERVER_NAME} --url {endpoint}",
        test_command=f"hermes mcp test {SERVER_NAME}",
        skill_target=HERMES_HOME / "skills" / SKILL_CATEGORY / SKILL_NAME / "SKILL.md",
        webhook_command=(
            "hermes webhook subscribe portfolio-snapshot "
            "--events portfolio_snapshot_created "
            '--prompt "Review the portfolio snapshot that was just imported." '
            f"--skills {SKILL_NAME}"
        ),
        hermes_present=hermes_present,
        notes=notes,
    )


def _copy_skill(
    plan: InstallPlan, source: Path, target: Path, label: str, *, overwrite: bool
) -> tuple[bool, str]:
    """Refuses to clobber an existing skill unless asked: the user may have
    edited the instructions, and those edits are theirs."""
    if not plan.hermes_present:
        return False, f"no Hermes installation at {HERMES_HOME}"
    if target.exists() and not overwrite:
        return False, f"{target} already exists; pass --overwrite to replace it"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text())
    return True, f"installed the {label} skill to {target}"


def install_skill(plan: InstallPlan, *, overwrite: bool = False) -> tuple[bool, str]:
    """Copy the analyst skill into the Hermes skills directory."""
    return _copy_skill(plan, SKILL_SOURCE, plan.skill_target, "analyst", overwrite=overwrite)


def install_advisor_skill(plan: InstallPlan, *, overwrite: bool = False) -> tuple[bool, str]:
    """Copy the advisor skill into the Hermes skills directory."""
    return _copy_skill(
        plan, ADVISOR_SKILL_SOURCE, plan.advisor_skill_target, "advisor", overwrite=overwrite
    )
