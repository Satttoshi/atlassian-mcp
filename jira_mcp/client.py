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
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings, Site, Token

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
    scopes: str
    not_found_hint: str
    allowed_post_paths: frozenset[str] = frozenset()


JIRA = Product(
    name="Jira",
    gateway="jira",
    probe_path=MYSELF_PATH,
    scopes="read:jira-work and read:jira-user",
    not_found_hint="Jira reports issues you cannot view as 404",
    allowed_post_paths=frozenset({"/rest/api/3/search/jql"}),
)

CONFLUENCE = Product(
    name="Confluence",
    gateway="confluence",
    probe_path=CONFLUENCE_CURRENT_USER_PATH,
    scopes="the Confluence read scopes listed in the README",
    not_found_hint="Confluence reports pages you cannot view as 404",
)


class ReadOnlyViolationError(RuntimeError):
    """A code path attempted a request that could modify Jira or Confluence."""


class AtlassianError(Exception):
    """An API call failed; the message is written to be actionable."""

    def __init__(
        self, message: str, status_code: int | None = None, detail: str = ""
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        # The raw response body, for callers that explain an error further.
        self.detail = detail


class BaseUrlDetectionError(AtlassianError):
    """Neither candidate base URL authenticated successfully."""


GRAPHQL_PATH = "/gateway/api/graphql"

# The rich-text section of a release page exists only in Atlassian's GraphQL
# gateway (experimental there, hence @optIn), not in the REST API.
_SECTION = (
    'richTextSection @optIn(to: "JiraVersionRichTextSection") '
    "{ title content { json } }"
)
GRAPHQL_QUERIES: dict[str, str] = {
    "VersionAri": (
        "query VersionAri($cloudId: ID!, $projects: [String!]) { jira { "
        "versionSearch(cloudId: $cloudId, filter: {projectKeys: $projects}, "
        "first: 1) { edges { node { id } } } } }"
    ),
    "VersionSection": (
        "query VersionSection($id: ID!) { jira { versionById(id: $id) "
        f"{{ versionId {_SECTION} }} }} }}"
    ),
}
GRAPHQL_MUTATIONS: dict[str, str] = {
    "SetVersionSectionTitle": (
        "mutation SetVersionSectionTitle("
        "$input: JiraUpdateVersionRichTextSectionTitleInput!) { jira { "
        "updateJiraVersionRichTextSectionTitle(input: $input) "
        '@optIn(to: "JiraUpdateVersionRichTextSectionTitle") '
        "{ success errors { message } } } }"
    ),
    "SetVersionSectionContent": (
        "mutation SetVersionSectionContent("
        "$input: JiraUpdateVersionRichTextSectionContentInput!) { jira { "
        "updateJiraVersionRichTextSectionContent(input: $input) "
        '@optIn(to: "JiraUpdateVersionRichTextSectionContent") '
        "{ success errors { message } } } }"
    ),
}


class AtlassianClient:
    def __init__(
        self,
        settings: Settings,
        product: Product,
        token: Token,
        http: httpx.AsyncClient | None = None,
        site: Site | None = None,
    ) -> None:
        self._settings = settings
        self.product = product
        self.site = site or settings.default_site
        self.token_source = token.source
        # Basic auth (email:token); the token itself is never logged.
        self._http = http or httpx.AsyncClient(
            auth=(settings.email, token.value),
            timeout=DEFAULT_TIMEOUT,
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )
        self._base_url: str | None = None
        self._version_ari_prefix: str | None = None
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
        return self.site.url

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

    async def graphql(self, operation: str, variables: dict[str, Any]) -> Any:
        """Run one of the fixed GraphQL documents; returns its `jira` data."""
        document = self._graphql_document(operation, variables)
        response = await self._http.post(
            f"{self.site_url}{GRAPHQL_PATH}",
            json={"query": document, "variables": variables},
        )
        self._raise_for_status(response, GRAPHQL_PATH)
        payload = response.json()
        data = (payload.get("data") or {}).get("jira")
        if data is None:
            messages = "; ".join(
                str(e.get("message")) for e in payload.get("errors") or []
            )
            raise AtlassianError(
                f"{self.product.name} GraphQL {operation} failed: "
                f"{messages or 'no data returned'}",
                detail=response.text[:1000],
            )
        return data

    def _graphql_document(self, operation: str, variables: dict[str, Any]) -> str:
        if operation not in GRAPHQL_QUERIES:
            raise ReadOnlyViolationError(
                f"Blocked GraphQL {operation}: not an allowed read."
            )
        return GRAPHQL_QUERIES[operation]

    async def version_ari(self, project: str, version_id: str) -> str:
        """The GraphQL ID of a version; all versions of a site share its prefix."""
        if self._version_ari_prefix is None:
            cloud_id = self._settings.cloud_id or await self._fetch_cloud_id(
                self.site_url
            )
            data = await self.graphql(
                "VersionAri", {"cloudId": cloud_id, "projects": [project]}
            )
            edges = (data.get("versionSearch") or {}).get("edges") or []
            if not edges:
                raise AtlassianError(
                    f"Could not determine the GraphQL ID of version {version_id} "
                    f"in {project}: the version search returned nothing."
                )
            self._version_ari_prefix = str(edges[0]["node"]["id"]).rsplit("/", 1)[0]
        return f"{self._version_ari_prefix}/{version_id}"

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
        self._check_allowed(method, path, json_body)
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

    def _check_allowed(
        self, method: str, path: str, json_body: dict[str, Any] | None
    ) -> None:
        allowed_posts = self.product.allowed_post_paths
        if method != "GET" and not (method == "POST" and path in allowed_posts):
            raise ReadOnlyViolationError(
                f"Blocked {method} {path}: this server is strictly read-only. "
                f"Only GET requests are permitted, plus POST to {sorted(allowed_posts)}."
            )

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
                    f"{self.token_source}."
                )
            logger.info(
                "%s base URL: %s (ATLASSIAN_CLOUD_ID override, no detection)",
                name,
                base,
            )
            return self._select(base, account, fallback=False)

        site = self.site.url
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
                f"Check ATLASSIAN_EMAIL and {self.token_source}. If the token "
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
                f"ATLASSIAN_EMAIL and {self.token_source} (tokens expire and "
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
        raise AtlassianError(message, status_code=status, detail=response.text[:1000])


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    try:
        delay = float(response.headers.get("retry-after", ""))
    except ValueError:
        delay = 2.0**attempt
    return max(0.0, min(delay, MAX_RETRY_DELAY))


class JiraClient(AtlassianClient):
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient | None = None,
        site: Site | None = None,
    ) -> None:
        super().__init__(settings, JIRA, settings.jira_token, http, site)

    async def search_jql(self, body: dict[str, Any]) -> Any:
        response = await self._request("POST", "/rest/api/3/search/jql", json_body=body)
        return response.json()


class ConfluenceClient(AtlassianClient):
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient | None = None,
        site: Site | None = None,
    ) -> None:
        super().__init__(settings, CONFLUENCE, settings.confluence_token, http, site)


JIRA_WRITE = Product(
    name="Jira (write)",
    gateway="jira",
    probe_path=MYSELF_PATH,
    scopes=(
        "read:jira-work, write:jira-work and read:jira-user (plus "
        "manage:jira-project for the version tools)"
    ),
    not_found_hint="Jira reports issues you cannot view as 404",
    allowed_post_paths=frozenset({"/rest/api/3/search/jql"}),
)

_KEY = r"(?P<key>[A-Z][A-Z0-9_]*-\d+)"

# The complete list of writes. No DELETE except removing an issue link, so an
# issue, comment or attachment can never be deleted through this server.
WRITE_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(r"^/rest/api/2/issue$")),
    ("PUT", re.compile(rf"^/rest/api/2/issue/{_KEY}$")),
    ("POST", re.compile(rf"^/rest/api/2/issue/{_KEY}/comment$")),
    ("PUT", re.compile(rf"^/rest/api/2/issue/{_KEY}/comment/\d+$")),
    ("POST", re.compile(rf"^/rest/api/2/issue/{_KEY}/transitions$")),
    ("POST", re.compile(r"^/rest/api/2/issueLink$")),
    ("DELETE", re.compile(r"^/rest/api/2/issueLink/\d+$")),
    ("POST", re.compile(r"^/rest/api/3/version$")),
    ("PUT", re.compile(r"^/rest/api/3/version/(?P<version>\d+)$")),
    ("POST", re.compile(r"^/rest/api/3/version/(?P<version>\d+)/relatedwork$")),
)

# No name (rename), archived or moveUnfixedIssuesTo: a version can't be renamed
# or archived, and releasing one never moves its issues.
VERSION_CREATE_FIELDS = frozenset(
    {"name", "projectId", "description", "startDate", "releaseDate", "driver"}
)
VERSION_UPDATE_FIELDS = frozenset(
    {"released", "description", "startDate", "releaseDate", "driver"}
)
RELATED_WORK_FIELDS = frozenset({"category", "title", "url"})


class WriteNotAllowedError(ReadOnlyViolationError):
    """A write outside the allowed routes or the allowed projects."""


def project_of(issue_key: str) -> str:
    return issue_key.rsplit("-", 1)[0].upper()


class JiraWriteClient(AtlassianClient):
    """Jira client for the opt-in write tools.

    Every write is checked against WRITE_ROUTES and JIRA_WRITE_PROJECTS before
    it is sent. Removing a link (DELETE issueLink) can't be checked here, since
    the path has no issue key: callers must check the linked issues first.
    Version writes carry numeric IDs only, so they are allowed just for IDs
    this client looked up itself through project_id() and project_versions().
    The same holds for the GraphQL mutations, which are a fixed set.
    """

    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient | None = None,
        site: Site | None = None,
    ) -> None:
        if not settings.writes_enabled:
            raise WriteNotAllowedError(
                "Jira writes are disabled: set JIRA_WRITE_PROJECTS in .env."
            )
        super().__init__(settings, JIRA_WRITE, settings.write_token, http, site)
        self.projects = settings.jira_write_projects
        self._project_keys: dict[str, str] = {}
        self._version_projects: dict[str, str] = {}

    def require_project(self, issue_key: str) -> None:
        project = project_of(issue_key)
        if project not in self.projects:
            raise WriteNotAllowedError(
                f"Blocked write to {issue_key}: project {project} is not in "
                f"JIRA_WRITE_PROJECTS ({', '.join(sorted(self.projects))})."
            )

    async def project_id(self, project: str) -> str:
        data = await self.get_json(f"/rest/api/3/project/{project}")
        project_id = str(data["id"])
        self._project_keys[project_id] = project
        return project_id

    async def project_versions(self, project: str) -> list[dict[str, Any]]:
        """All versions of a project, in Jira's order (oldest first)."""
        versions = list(await self.get_json(f"/rest/api/3/project/{project}/versions"))
        for version in versions:
            self.track_version(str(version["id"]), project)
        return versions

    def track_version(self, version_id: str, project: str) -> None:
        self._version_projects[version_id] = project

    async def send(
        self, method: str, path: str, json_body: dict[str, Any] | None = None
    ) -> Any:
        response = await self._request(method, path, json_body=json_body)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _check_allowed(
        self, method: str, path: str, json_body: dict[str, Any] | None
    ) -> None:
        if method == "GET" or (
            method == "POST" and path in self.product.allowed_post_paths
        ):
            return
        for route_method, pattern in WRITE_ROUTES:
            match = pattern.match(path)
            if route_method == method and match:
                break
        else:
            raise WriteNotAllowedError(
                f"Blocked {method} {path}: not an allowed write."
            )
        body = json_body or {}
        groups = match.groupdict()
        if "key" in groups:
            self.require_project(match["key"])
        if "version" in groups:
            self._require_known(
                self._version_projects, match["version"], "version", path
            )
            is_link = path.endswith("/relatedwork")
            _require_fields(
                body, RELATED_WORK_FIELDS if is_link else VERSION_UPDATE_FIELDS, path
            )
        elif path == "/rest/api/3/version":
            self._require_known(
                self._project_keys, str(body.get("projectId")), "project ID", path
            )
            _require_fields(body, VERSION_CREATE_FIELDS, path)
        for key in _body_issue_keys(path, body):
            self.require_project(key)

    def _graphql_document(self, operation: str, variables: dict[str, Any]) -> str:
        if operation not in GRAPHQL_MUTATIONS:
            return super()._graphql_document(operation, variables)
        ari = str((variables.get("input") or {}).get("id", ""))
        self._require_known(
            self._version_projects, ari.rsplit("/", 1)[-1], "version", GRAPHQL_PATH
        )
        return GRAPHQL_MUTATIONS[operation]

    def _require_known(
        self, known: dict[str, str], ref: str, what: str, path: str
    ) -> None:
        project = known.get(ref)
        if project is None:
            raise WriteNotAllowedError(
                f"Blocked write to {path}: {what} {ref} was not looked up first, "
                "so its project is unknown."
            )
        self.require_project(f"{project}-0")


def _require_fields(body: dict[str, Any], allowed: frozenset[str], path: str) -> None:
    extra = sorted(set(body) - allowed)
    if extra:
        raise WriteNotAllowedError(
            f"Blocked write to {path}: field(s) {', '.join(extra)} may not be set."
        )


def _body_issue_keys(path: str, body: dict[str, Any]) -> list[str]:
    if path == "/rest/api/2/issue":
        project = (body.get("fields") or {}).get("project") or {}
        return [f"{project.get('key', '')}-0"]
    if path == "/rest/api/2/issueLink":
        return [
            str((body.get(side) or {}).get("key", ""))
            for side in ("inwardIssue", "outwardIssue")
        ]
    return []
