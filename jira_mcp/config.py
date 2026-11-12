"""Configuration loading.

The .env file is resolved relative to this source file (via ``__file__``),
never the working directory: Claude Code spawns MCP servers with an arbitrary
cwd, so a cwd-relative lookup works in manual testing and then fails silently
under Claude.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Path(__file__) is this file; .parent.parent walks up to the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

REQUIRED_KEYS = ("ATLASSIAN_EMAIL", "ATLASSIAN_API_TOKEN", "ATLASSIAN_SITE_URL")


class ConfigError(Exception):
    """Raised when required configuration is missing or malformed."""


# frozen=True makes instances immutable — settings should never change after load.
@dataclass(frozen=True)
class Settings:
    email: str
    api_token: str
    site_url: str
    cloud_id: str | None
    env_file_found: bool
    # Scoped tokens are bound to one app, so Confluence may need its own token.
    confluence_api_token: str | None = None
    # Writes are opt-in: enabled by listing projects. A separate write token is
    # optional; without one the main token is used (it needs write scope then).
    jira_write_api_token: str | None = None
    jira_write_projects: frozenset[str] = frozenset()

    @property
    def confluence_token(self) -> str:
        return self.confluence_api_token or self.api_token

    @property
    def write_token(self) -> str:
        return self.jira_write_api_token or self.api_token

    @property
    def writes_enabled(self) -> bool:
        return bool(self.jira_write_projects)


def _parse_projects(raw: str) -> frozenset[str]:
    return frozenset(p.strip().upper() for p in raw.split(",") if p.strip())


def _normalize_site_url(raw: str) -> str:
    url = raw.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url


def load_settings() -> Settings:
    env_file_found = ENV_FILE.is_file()
    # override=False: real environment variables take precedence over the file.
    load_dotenv(ENV_FILE, override=False)

    missing = [key for key in REQUIRED_KEYS if not os.environ.get(key)]
    if missing:
        raise ConfigError(
            f"Missing required configuration: {', '.join(missing)}. "
            f"Set them in the environment or in {ENV_FILE} "
            f"(.env file {'found' if env_file_found else 'NOT found'} there; "
            f"see .env.example for the template)."
        )

    return Settings(
        email=os.environ["ATLASSIAN_EMAIL"].strip(),
        api_token=os.environ["ATLASSIAN_API_TOKEN"].strip(),
        site_url=_normalize_site_url(os.environ["ATLASSIAN_SITE_URL"]),
        cloud_id=os.environ.get("ATLASSIAN_CLOUD_ID", "").strip() or None,
        env_file_found=env_file_found,
        confluence_api_token=os.environ.get("CONFLUENCE_API_TOKEN", "").strip() or None,
        jira_write_api_token=os.environ.get("JIRA_WRITE_API_TOKEN", "").strip() or None,
        jira_write_projects=_parse_projects(os.environ.get("JIRA_WRITE_PROJECTS", "")),
    )
