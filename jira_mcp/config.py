"""Configuration loading.

The .env file is resolved relative to this source file (via ``__file__``),
never the working directory: Claude Code spawns MCP servers with an arbitrary
cwd, so a cwd-relative lookup works in manual testing and then fails silently
under Claude.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Path(__file__) is this file; .parent.parent walks up to the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

REQUIRED_KEYS = ("ATLASSIAN_EMAIL", "ATLASSIAN_SITE_URL")
SHARED_TOKEN_ENV = "ATLASSIAN_API_TOKEN"
JIRA_TOKEN_ENV = "JIRA_API_TOKEN"
CONFLUENCE_TOKEN_ENV = "CONFLUENCE_API_TOKEN"
JIRA_WRITE_TOKEN_ENV = "JIRA_WRITE_API_TOKEN"
TOKEN_KEYS = (SHARED_TOKEN_ENV, JIRA_TOKEN_ENV, CONFLUENCE_TOKEN_ENV)


class ConfigError(Exception):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True)
class Site:
    # Short handle used in tool arguments and output, e.g. "acme".
    name: str
    url: str

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()


class Token(NamedTuple):
    value: str
    # The env var the value came from; safe to show in diagnostics.
    source: str


# frozen=True makes instances immutable — settings should never change after load.
@dataclass(frozen=True)
class Settings:
    email: str
    # The first site is the default.
    sites: tuple[Site, ...]
    cloud_id: str | None
    env_file_found: bool
    # Shared by both products unless a product-specific token is set. Scoped
    # tokens are bound to one app, so they need the product-specific variables.
    api_token: str | None = None
    jira_api_token: str | None = None
    confluence_api_token: str | None = None
    # Writes are opt-in: enabled by listing projects. A separate write token is
    # optional; without one the Jira token is used (it needs write scope then).
    jira_write_api_token: str | None = None
    jira_write_projects: frozenset[str] = frozenset()

    @property
    def default_site(self) -> Site:
        return self.sites[0]

    @property
    def jira_token(self) -> Token:
        return self._token(self.jira_api_token, JIRA_TOKEN_ENV, "Jira")

    @property
    def confluence_token(self) -> Token:
        return self._token(
            self.confluence_api_token, CONFLUENCE_TOKEN_ENV, "Confluence"
        )

    def _token(self, specific: str | None, specific_env: str, product: str) -> Token:
        if specific:
            return Token(specific, specific_env)
        if self.api_token:
            return Token(self.api_token, SHARED_TOKEN_ENV)
        raise ConfigError(
            f"No API token configured for {product}. Set {specific_env} (a token "
            f"scoped to {product}) or {SHARED_TOKEN_ENV} (one unscoped token for "
            f"both products) in the environment or in {ENV_FILE}."
        )

    @property
    def write_token(self) -> Token:
        """Falls back to the Jira read token, which then needs write scope."""
        if self.jira_write_api_token:
            return Token(self.jira_write_api_token, JIRA_WRITE_TOKEN_ENV)
        return self.jira_token

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


def parse_sites(raw: str) -> tuple[Site, ...]:
    """Parse ATLASSIAN_SITE_URL: one site URL, or several separated by commas.

    Names are derived from the hostname: "acme" for acme.atlassian.net, the
    full hostname for custom domains.
    """
    sites: list[Site] = []
    for part in raw.split(","):
        if not part.strip():
            continue
        site = Site(name="", url=_normalize_site_url(part))
        name = site.host.removesuffix(".atlassian.net")
        if not name:
            raise ConfigError(
                f"ATLASSIAN_SITE_URL entry '{part.strip()}' is not a URL."
            )
        if any(existing.name == name for existing in sites):
            raise ConfigError(f"ATLASSIAN_SITE_URL lists the site '{name}' twice.")
        sites.append(Site(name=name, url=site.url))
    if not sites:
        raise ConfigError("ATLASSIAN_SITE_URL contains no site URL.")
    return tuple(sites)


def load_settings() -> Settings:
    env_file_found = ENV_FILE.is_file()
    # override=False: real environment variables take precedence over the file.
    load_dotenv(ENV_FILE, override=False)
    where = (
        f"Set them in the environment or in {ENV_FILE} "
        f"(.env file {'found' if env_file_found else 'NOT found'} there; "
        f"see .env.example for the template)."
    )

    def optional(key: str) -> str | None:
        return os.environ.get(key, "").strip() or None

    missing = [key for key in REQUIRED_KEYS if not optional(key)]
    if missing:
        raise ConfigError(
            f"Missing required configuration: {', '.join(missing)}. {where}"
        )
    tokens = {key: optional(key) for key in TOKEN_KEYS}
    if not any(tokens.values()):
        raise ConfigError(
            f"No API token configured: set {SHARED_TOKEN_ENV} (one token for both "
            f"products), or {JIRA_TOKEN_ENV} and/or {CONFLUENCE_TOKEN_ENV} (scoped "
            f"tokens). {where}"
        )
    if all(tokens.values()):
        logger.warning(
            "%s is unused: %s and %s are both set and take precedence.",
            SHARED_TOKEN_ENV,
            JIRA_TOKEN_ENV,
            CONFLUENCE_TOKEN_ENV,
        )

    sites = parse_sites(os.environ["ATLASSIAN_SITE_URL"])
    cloud_id = optional("ATLASSIAN_CLOUD_ID")
    if cloud_id and len(sites) > 1:
        raise ConfigError(
            "ATLASSIAN_CLOUD_ID identifies a single site, so it cannot be combined "
            "with several sites in ATLASSIAN_SITE_URL. Remove it; the cloud ID of "
            "each site is detected automatically."
        )

    return Settings(
        email=os.environ["ATLASSIAN_EMAIL"].strip(),
        sites=sites,
        cloud_id=cloud_id,
        env_file_found=env_file_found,
        api_token=tokens[SHARED_TOKEN_ENV],
        jira_api_token=tokens[JIRA_TOKEN_ENV],
        confluence_api_token=tokens[CONFLUENCE_TOKEN_ENV],
        jira_write_api_token=optional(JIRA_WRITE_TOKEN_ENV),
        jira_write_projects=_parse_projects(optional("JIRA_WRITE_PROJECTS") or ""),
    )


def find_site(settings: Settings, ref: str) -> Site:
    """Match an explicit site reference: a site name, hostname or URL."""
    wanted = ref.strip().lower().rstrip("/")
    host = urlsplit(wanted if "://" in wanted else f"https://{wanted}").hostname
    for site in settings.sites:
        if wanted == site.name or host == site.host:
            return site
    raise ValueError(
        f"Unknown site '{ref}'. Configured sites: {describe_sites(settings)}."
    )


def site_from_url(settings: Settings, text: str) -> Site | None:
    """The configured site a pasted URL points at; None if `text` is no URL."""
    text = text.strip()
    if not text.lower().startswith(("http://", "https://")):
        return None
    host = (urlsplit(text).hostname or "").lower()
    for site in settings.sites:
        if host == site.host:
            return site
    if len(settings.sites) == 1:
        return None
    raise ValueError(
        f"'{host}' is not a configured site ({describe_sites(settings)}). Add it "
        "to ATLASSIAN_SITE_URL to read from it."
    )


def describe_sites(settings: Settings) -> str:
    return ", ".join(f"{site.name} ({site.url})" for site in settings.sites)
