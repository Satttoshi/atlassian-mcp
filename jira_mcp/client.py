"""Read-only Atlassian HTTP clients (Jira and Confluence).

Two invariants live here:

1. **Read-only enforcement.** Every request funnels through ``_request``,
   which permits only GET — plus the product's explicit POST allowlist
   (for Jira: ``POST /rest/api/3/search/jql``, a read that Atlassian
   requires as POST; for Confluence: nothing). Any other method/path
   combination raises ``ReadOnlyViolationError``, so a future edit that adds
   a write call fails loudly instead of quietly working.

2. **Base URL auto-detection.** Unscoped API tokens work against
   ``https://<site>.atlassian.net``; scoped tokens fail *silently* there
   (anonymous-looking responses) and must use
   ``https://api.atlassian.com/ex/<product>/{cloudId}``. We probe at startup,
   fall back via ``{site}/_edge/tenant_info``, cache the winner for the
   process lifetime, and log the decision to stderr.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings

logger = logging.getLogger(__name__)

MYSELF_PATH = "/rest/api/3/myself"
CONFLUENCE_CURRENT_USER_PATH = "/wiki/rest/api/user/current"

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# 429s are retried, honouring Retry-After up to a cap so a tool call never
# hangs for minutes; past the retries the 429 surfaces as an error.
MAX_RETRIES = 2
MAX_RETRY_DELAY = 10.0


@dataclass(frozen=True)
class Product:
    name: str
    # Path segment on the gateway: api.atlassian.com/ex/<gateway>/{cloudId}.
    gateway: str
    # Must return the authenticated account (with accountId) when auth works.
    probe_path: str
    token_env: str
    scopes: str
    not_found_hint: str
    allowed_post_paths: frozenset[str] = frozenset()


JIRA = Product(
    name="Jira",
    gateway="jira",
    probe_path=MYSELF_PATH,
    token_env="ATLASSIAN_API_TOKEN",
    scopes="read:jira-work and read:jira-user",
    not_found_hint="Jira reports issues you cannot view as 404",
    allowed_post_paths=frozenset({"/rest/api/3/search/jql"}),
)

CONFLUENCE = Product(
    name="Confluence",
    gateway="confluence",
    probe_path=CONFLUENCE_CURRENT_USER_PATH,
    token_env="CONFLUENCE_API_TOKEN (or ATLASSIAN_API_TOKEN)",
    scopes="the Confluence read scopes listed in the README",
    not_found_hint="Confluence reports pages you cannot view as 404",
)


class ReadOnlyViolationError(RuntimeError):
    """A code path attempted a request that could modify Jira or Confluence."""


class AtlassianError(Exception):
    """An API call failed; the message is written to be actionable."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BaseUrlDetectionError(AtlassianError):
    """Neither candidate base URL authenticated successfully."""


class AtlassianClient:
    def __init__(
        self,
        settings: Settings,
        product: Product,
        api_token: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self.product = product
        # Basic auth (email:token); the token itself is never logged.
        self._http = http or httpx.AsyncClient(
            auth=(settings.email, api_token),
            timeout=DEFAULT_TIMEOUT,
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )
        self._base_url: str | None = None
        # Diagnostics for --check; filled in by _ensure_base_url().
        self.used_cloud_id_fallback: bool = False
        self.account: dict[str, Any] | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def base_url(self) -> str | None:
        return self._base_url

    @property
    def site_url(self) -> str:
        return self._settings.site_url

    # -- public request helpers ------------------------------------------------

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._request("GET", path, params=params)
        return response.json()

    async def get_bytes(self, path: str) -> tuple[bytes, str]:
        """Download binary content (e.g. attachment bytes).

        Returns (content, mime type). The Accept header must be widened per
        request: the client default of application/json would make the API
        refuse to serve the raw file.
        """
        response = await self._request("GET", path, headers={"Accept": "*/*"})
        content_type = response.headers.get("content-type", "")
        return response.content, content_type.split(";")[0].strip()

    # -- the single funnel -----------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        method = method.upper()
        allowed_posts = self.product.allowed_post_paths
        if method != "GET" and not (method == "POST" and path in allowed_posts):
            raise ReadOnlyViolationError(
                f"Blocked {method} {path}: this server is strictly read-only. "
                f"Only GET requests are permitted, plus POST to {sorted(allowed_posts)}."
            )
        base = await self._ensure_base_url()
        for attempt in range(MAX_RETRIES + 1):
            response = await self._http.request(
                method, f"{base}{path}", params=params, json=json_body, headers=headers
            )
            if response.status_code != 429 or attempt == MAX_RETRIES:
                break
            delay = _retry_delay(response, attempt)
            logger.info(
                "%s rate-limited %s; retrying in %.1fs", self.product.name, path, delay
            )
            await asyncio.sleep(delay)
        self._raise_for_status(response, path)
        return response

    # -- base URL detection ----------------------------------------------------

    def _gateway_base(self, cloud_id: str) -> str:
        return f"https://api.atlassian.com/ex/{self.product.gateway}/{cloud_id}"

    async def _ensure_base_url(self) -> str:
        if self._base_url is not None:
            return self._base_url

        name = self.product.name
        if self._settings.cloud_id:
            base = self._gateway_base(self._settings.cloud_id)
            account = await self._probe(base)
            if account is None:
                raise BaseUrlDetectionError(
                    f"ATLASSIAN_CLOUD_ID is set, but {name} authentication against "
                    f"{base} failed. Check the cloud ID, ATLASSIAN_EMAIL and "
                    f"{self.product.token_env}."
                )
            logger.info(
                "%s base URL: %s (ATLASSIAN_CLOUD_ID override, no detection)",
                name,
                base,
            )
            return self._select(base, account, fallback=False)

        site = self._settings.site_url
        account = await self._probe(site)
        if account is not None:
            logger.info(
                "%s base URL: %s (site URL authenticated directly — unscoped token)",
                name,
                site,
            )
            return self._select(site, account, fallback=False)

        logger.info(
            "%s: site URL %s did not authenticate (typical for scoped tokens); "
            "fetching cloud ID from _edge/tenant_info",
            name,
            site,
        )
        cloud_id = await self._fetch_cloud_id(site)
        base = self._gateway_base(cloud_id)
        account = await self._probe(base)
        if account is None:
            raise BaseUrlDetectionError(
                f"{name} authentication failed against both {site} and {base}. "
                f"Check ATLASSIAN_EMAIL and {self.product.token_env}. If the token "
                f"is scoped, confirm it was created for {name} with "
                f"{self.product.scopes}."
            )
        logger.info("%s base URL: %s (cloud-ID fallback — scoped token)", name, base)
        return self._select(base, account, fallback=True)

    def _select(self, base: str, account: dict[str, Any], fallback: bool) -> str:
        self._base_url = base
        self.account = account
        self.used_cloud_id_fallback = fallback
        logger.info(
            "%s: authenticated as %s (%s)",
            self.product.name,
            account.get("displayName", "?"),
            account.get("emailAddress")
            or account.get("email")
            or account.get("accountId", "?"),
        )
        return base

    async def _probe(self, base: str) -> dict[str, Any] | None:
        """GET the product's current-user endpoint against a candidate base URL.

        Returns the account JSON only for a real authenticated account: a
        scoped token hitting the wrong base can produce 200 responses that
        merely *look* anonymous, so a 200 without an accountId is a failure.
        """
        try:
            response = await self._http.get(f"{base}{self.product.probe_path}")
        except httpx.HTTPError as exc:
            logger.info("Probe of %s failed: %s", base, exc)
            return None
        if response.status_code != 200:
            logger.info("Probe of %s returned HTTP %s", base, response.status_code)
            return None
        try:
            data = response.json()
        except ValueError:
            logger.info("Probe of %s returned non-JSON", base)
            return None
        if not isinstance(data, dict) or not data.get("accountId"):
            logger.info("Probe of %s returned an anonymous-looking response", base)
            return None
        return data

    async def _fetch_cloud_id(self, site: str) -> str:
        """Resolve the cloud ID from the site's tenant info endpoint.

        Note: _edge/tenant_info is not part of Atlassian's formally supported
        REST API (though Atlassian support docs tell users to call it), hence
        the explicit escape hatch in the error message.
        """
        try:
            response = await self._http.get(f"{site}/_edge/tenant_info")
            response.raise_for_status()
            cloud_id = response.json().get("cloudId")
        except (httpx.HTTPError, ValueError) as exc:
            raise BaseUrlDetectionError(
                f"Could not determine the cloud ID from {site}/_edge/tenant_info ({exc}). "
                "Set ATLASSIAN_CLOUD_ID manually in .env to skip detection."
            ) from exc
        if not cloud_id:
            raise BaseUrlDetectionError(
                f"{site}/_edge/tenant_info returned no cloudId. "
                "Set ATLASSIAN_CLOUD_ID manually in .env to skip detection."
            )
        return str(cloud_id)

    # -- error mapping ---------------------------------------------------------

    def _raise_for_status(self, response: httpx.Response, path: str) -> None:
        status = response.status_code
        if status < 400:
            return
        name = self.product.name
        if status == 401:
            message = (
                f"401 Unauthorized: {name} rejected the credentials. Check "
                f"ATLASSIAN_EMAIL and {self.product.token_env} (tokens expire and "
                "can be revoked)."
            )
        elif status == 403:
            message = (
                "403 Forbidden: the token or account lacks access. If this is a "
                f"scoped token, confirm it has {self.product.scopes}."
            )
        elif status == 404:
            message = (
                f"404 Not Found for {path}: the resource does not exist — or your "
                f"account lacks permission to see it. {self.product.not_found_hint}, "
                "and an API token never grants more access than the account it "
                "belongs to."
            )
        elif status == 429:
            message = f"429 Too Many Requests: {name} is rate-limiting; retry shortly."
        else:
            message = f"{name} returned HTTP {status} for {path}: {response.text[:300]}"
        raise AtlassianError(message, status_code=status)


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    try:
        delay = float(response.headers.get("retry-after", ""))
    except ValueError:
        delay = 2.0**attempt
    return max(0.0, min(delay, MAX_RETRY_DELAY))


class JiraClient(AtlassianClient):
    def __init__(
        self, settings: Settings, http: httpx.AsyncClient | None = None
    ) -> None:
        super().__init__(settings, JIRA, settings.api_token, http)

    async def search_jql(self, body: dict[str, Any]) -> Any:
        response = await self._request("POST", "/rest/api/3/search/jql", json_body=body)
        return response.json()


class ConfluenceClient(AtlassianClient):
    def __init__(
        self, settings: Settings, http: httpx.AsyncClient | None = None
    ) -> None:
        super().__init__(settings, CONFLUENCE, settings.confluence_token, http)
