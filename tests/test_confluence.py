"""Tests for Confluence: references, detection, pages, comments, tree, search,
attachments (mocked HTTP)."""

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from fastmcp.utilities.types import Image

from jira_mcp import confluence
from jira_mcp.client import AtlassianError, ConfluenceClient, ReadOnlyViolationError
from jira_mcp.config import Settings
from jira_mcp.confluence_render import normalize_attachment_id, parse_page_ref

SITE = "https://example.atlassian.net"
GATEWAY = "https://api.atlassian.com/ex/confluence/cid-123"
V2 = "/wiki/api/v2"
ACCOUNT = {"accountId": "me", "displayName": "Ada", "email": "ada@x.io"}
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"x" * 64

Route = dict[str, Any] | list[Any] | httpx.Response


@pytest.fixture(autouse=True)
def reset_caches() -> Iterator[None]:
    confluence._user_names.clear()
    confluence._spaces.clear()
    yield
    confluence._user_names.clear()
    confluence._spaces.clear()


def settings(confluence_token: str | None = None) -> Settings:
    return Settings(
        email="ada@x.io",
        api_token="jira-secret",
        site_url=SITE,
        cloud_id=None,
        env_file_found=True,
        confluence_api_token=confluence_token,
    )


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> ConfluenceClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ConfluenceClient(settings(), http=http)


class Router:
    """Serves auth plus a path → JSON map (on the site URL); records requests."""

    def __init__(self, routes: dict[str, Route]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/wiki/rest/api/user/current":
            return httpx.Response(200, json=ACCOUNT)
        if path == "/wiki/rest/api/user":
            key = f"user:{request.url.params['accountId']}"
            route = self.routes.get(key)
        else:
            route = self.routes.get(path)
        if route is None:
            return httpx.Response(404)
        if isinstance(route, httpx.Response):
            return route
        return httpx.Response(200, json=route)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def adf(*content: dict[str, Any]) -> dict[str, Any]:
    return {
        "atlas_doc_format": {"value": json.dumps({"type": "doc", "content": content})}
    }


def para(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


PAGE = {
    "id": "100",
    "title": "Checkout redesign",
    "status": "current",
    "spaceId": "9",
    "authorId": "u1",
    "ownerId": "u1",
    "createdAt": "2026-01-01T10:00:00.000Z",
    "version": {"number": 4, "authorId": "u2", "createdAt": "2026-02-01T10:00:00.000Z"},
    "labels": {"results": [{"name": "spec"}, {"name": "checkout"}]},
    "_links": {"webui": "/spaces/ENG/pages/100/Checkout+redesign"},
    "body": adf(
        para("Intro text."),
        {
            "type": "mediaSingle",
            "content": [
                {"type": "media", "attrs": {"id": "file-uuid", "type": "file"}}
            ],
        },
        {
            "type": "extension",
            "attrs": {
                "extensionKey": "jira",
                "parameters": {"macroParams": {"key": {"value": "SHOP-42"}}},
            },
        },
    ),
}

ATTACHMENTS = {
    "results": [
        {
            "id": "att555",
            "title": "flow.png",
            "mediaType": "image/png",
            "fileSize": 2048,
            "fileId": "file-uuid",
        }
    ],
    "_links": {},
}


def comment(cid: str, author: str, text: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": cid,
        "version": {
            "number": 1,
            "authorId": author,
            "createdAt": f"2026-03-0{cid[-1]}",
        },
        "body": adf(para(text)),
        **extra,
    }


def page_routes() -> dict[str, Route]:
    return {
        f"{V2}/pages/100": PAGE,
        f"{V2}/pages/100/attachments": ATTACHMENTS,
        f"{V2}/spaces/9": {"id": "9", "key": "ENG", "name": "Engineering"},
        f"{V2}/pages/100/footer-comments": {
            "results": [comment("c1", "u1", "Looks good")]
        },
        f"{V2}/pages/100/inline-comments": {
            "results": [
                comment(
                    "c2",
                    "u3",
                    "Typo here",
                    resolutionStatus="resolved",
                    properties={"inlineOriginalSelection": "chekout"},
                )
            ]
        },
        f"{V2}/footer-comments/c1/children": {
            "results": [comment("c3", "u2", "Agreed")]
        },
        f"{V2}/footer-comments/c3/children": {"results": []},
        f"{V2}/inline-comments/c2/children": {"results": []},
        "user:u1": {"displayName": "Grace"},
        "user:u2": {"displayName": "Linus"},
        # u3 deliberately unresolvable.
    }


# -- references ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "content_id", "kind"),
    [
        ("123456", "123456", None),
        (123456, "123456", None),
        (f"{SITE}/wiki/spaces/ENG/pages/123/Title+Here", "123", "pages"),
        (f"{SITE}/wiki/spaces/ENG/pages/edit-v2/123", "123", "pages"),
        (f"{SITE}/wiki/pages/viewpage.action?pageId=777", "777", "pages"),
        (f"{SITE}/wiki/spaces/ENG/blog/2026/01/15/888/Release", "888", "blogposts"),
    ],
)
def test_parse_page_ref(raw: str | int, content_id: str, kind: str | None) -> None:
    ref = parse_page_ref(raw)
    assert (ref.content_id, ref.kind) == (content_id, kind)


def test_short_link_explains_itself() -> None:
    with pytest.raises(ValueError, match="short link"):
        parse_page_ref(f"{SITE}/wiki/x/AbCdEf")


@pytest.mark.parametrize("bad", ["", "PROJ-1", "https://example.com/foo"])
def test_parse_page_ref_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError, match="not a Confluence page reference"):
        parse_page_ref(bad)


@pytest.mark.parametrize(
    ("raw", "expected"), [("att123", "att123"), ("123", "att123"), (" ATT9 ", "att9")]
)
def test_normalize_attachment_id(raw: str, expected: str) -> None:
    assert normalize_attachment_id(raw) == expected


@pytest.mark.parametrize("bad", ["", "flow.png", "att", "100; x"])
def test_normalize_attachment_id_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError, match="attachment ID"):
        normalize_attachment_id(bad)


def test_to_cql_wraps_plain_words_and_escapes_quotes() -> None:
    assert (
        confluence.to_cql('retry "policy"')
        == 'type in (page, blogpost) AND text ~ "retry \\"policy\\""'
    )


def test_to_cql_passes_cql_through() -> None:
    cql = 'space = ENG AND title ~ "onboarding"'
    assert confluence.to_cql(cql) == cql


def test_confluence_token_falls_back_to_shared_token() -> None:
    assert settings().confluence_token == "jira-secret"
    assert settings("wiki-secret").confluence_token == "wiki-secret"


# -- client -------------------------------------------------------------------


async def test_scoped_token_uses_confluence_gateway() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{SITE}/wiki/rest/api/user/current":
            return httpx.Response(200, json={"type": "anonymous"})
        if url == f"{SITE}/_edge/tenant_info":
            return httpx.Response(200, json={"cloudId": "cid-123"})
        if url == f"{GATEWAY}/wiki/rest/api/user/current":
            return httpx.Response(200, json=ACCOUNT)
        if url.startswith(GATEWAY):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    client = make_client(handler)
    await client.get_json(f"{V2}/pages/1")
    assert client.base_url == GATEWAY
    assert client.used_cloud_id_fallback is True


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
async def test_confluence_writes_are_blocked(method: str) -> None:
    client = make_client(Router({}))
    with pytest.raises(ReadOnlyViolationError, match="read-only"):
        await client._request(method, f"{V2}/pages")


async def test_confluence_404_mentions_permissions() -> None:
    client = make_client(Router({}))
    with pytest.raises(AtlassianError, match="Confluence reports pages") as exc:
        await client.get_json(f"{V2}/pages/1")
    assert exc.value.status_code == 404


# -- pages --------------------------------------------------------------------


async def test_page_renders_body_metadata_attachments_and_comments() -> None:
    client = make_client(Router(page_routes()))
    output = await confluence.get_page_impl(client, "100", include_comments=True)

    assert output.startswith("# Checkout redesign")
    assert "- **Space:** Engineering (ENG)" in output
    assert "- **Created by:** Grace" in output
    assert "- **Last edited:** Linus, 2026-02-01T10:00:00.000Z" in output
    assert "- **Labels:** spec, checkout" in output
    assert f"- **URL:** {SITE}/wiki/spaces/ENG/pages/100/Checkout+redesign" in output
    assert "Intro text." in output
    # The embedded image is matched to its fetchable attachment.
    assert "[attachment: flow.png, id att555]" in output
    assert "[Jira issue: SHOP-42]" in output
    assert "- **flow.png** (id att555, image/png, 2 KB)" in output
    assert 'get_confluence_attachment(attachment_id="att555")' in output

    assert "## Comments (3)" in output
    assert "### Grace — 2026-03-01\n\nLooks good" in output
    assert "#### ↳ Linus — 2026-03-03\n\nAgreed" in output
    assert "### Unknown user — 2026-03-02 (inline, resolved)" in output
    assert '> on: "chekout"' in output


async def test_page_without_comments_skips_comment_requests() -> None:
    router = Router(page_routes())
    client = make_client(router)
    output = await confluence.get_page_impl(client, "100", include_comments=False)
    assert "## Comments" not in output
    assert not any("comments" in path for path in router.paths())


async def test_bare_id_falls_back_to_blog_post() -> None:
    blog = {**PAGE, "id": "200", "title": "Release notes"}
    routes: dict[str, Route] = {
        f"{V2}/blogposts/200": blog,
        f"{V2}/blogposts/200/attachments": {"results": []},
    }
    client = make_client(Router(routes))
    output = await confluence.get_page_impl(client, "200", include_comments=False)
    assert "- **Type:** Blog post" in output


async def test_missing_content_gives_one_clear_error() -> None:
    client = make_client(Router({}))
    with pytest.raises(AtlassianError, match="No Confluence page or blog post"):
        await confluence.get_page_impl(client, "999", include_comments=False)


async def test_url_ref_does_not_try_blog_posts() -> None:
    router = Router({})
    client = make_client(router)
    with pytest.raises(AtlassianError):
        await confluence.get_page_impl(
            client, f"{SITE}/wiki/spaces/ENG/pages/999/X", include_comments=False
        )
    assert not any("blogposts" in path for path in router.paths())


async def test_users_are_cached_across_calls() -> None:
    router = Router(page_routes())
    client = make_client(router)
    await confluence.get_page_impl(client, "100", include_comments=False)
    await confluence.get_page_impl(client, "100", include_comments=False)
    lookups = [p for p in router.paths() if p == "/wiki/rest/api/user"]
    assert len(lookups) == 2  # u1 and u2 once each, not per call


async def test_pagination_follows_next_links() -> None:
    routes: dict[str, Route] = {
        f"{V2}/pages/100/footer-comments": {
            "results": [comment("c1", "u1", "first")],
            "_links": {"next": f"{V2}/pages/100/footer-comments-page2?cursor=abc"},
        },
        f"{V2}/pages/100/footer-comments-page2": {
            "results": [comment("c2", "u1", "second")]
        },
    }
    client = make_client(Router(routes))
    results = await confluence._paginate(
        client, f"{V2}/pages/100/footer-comments", {}, 10
    )
    assert [r["id"] for r in results] == ["c1", "c2"]


async def test_comments_tool_output() -> None:
    client = make_client(Router(page_routes()))
    output = await confluence.get_comments_impl(client, "100", limit=50)
    assert output.startswith("# Comments on **Checkout redesign** (3)")


# -- page tree ----------------------------------------------------------------


async def test_page_context_lists_ancestors_and_children() -> None:
    routes: dict[str, Route] = {
        f"{V2}/pages/100": PAGE,
        f"{V2}/spaces/9": {"key": "ENG", "name": "Engineering"},
        f"{V2}/pages/100/ancestors": {
            "results": [{"id": "1", "type": "page"}, {"id": "2", "type": "folder"}]
        },
        f"{V2}/pages/1": {"title": "Engineering Home"},
        f"{V2}/folders/2": {"title": "Specs"},
        f"{V2}/pages/100/direct-children": {
            "results": [{"id": "101", "type": "page", "title": "API contract"}]
        },
    }
    client = make_client(Router(routes))
    output = await confluence.get_page_context_impl(client, "100")
    assert "- **Engineering Home** [page, id 1]" in output
    assert "  - **Specs** [folder, id 2]" in output
    assert "- **API contract** [page, id 101]" in output


async def test_blog_post_context_has_no_tree() -> None:
    routes: dict[str, Route] = {f"{V2}/blogposts/200": {**PAGE, "id": "200"}}
    client = make_client(Router(routes))
    output = await confluence.get_page_context_impl(
        client, f"{SITE}/wiki/spaces/ENG/blog/2026/01/15/200/X"
    )
    assert "no page tree" in output


# -- search -------------------------------------------------------------------


async def test_search_sends_wrapped_cql_and_cleans_excerpts() -> None:
    routes: dict[str, Route] = {
        "/wiki/rest/api/search": {
            "results": [
                {
                    "content": {"id": "100", "type": "page"},
                    "title": "@@@hl@@@Checkout@@@endhl@@@ redesign &gt; v2",
                    "excerpt": "The @@@hl@@@checkout@@@endhl@@@\nflow   changes&#39;",
                    "resultGlobalContainer": {"title": "Engineering"},
                    "lastModified": "2026-02-01T10:00:00.000Z",
                }
            ],
            "totalSize": 7,
        }
    }
    router = Router(routes)
    client = make_client(router)
    output = await confluence.search_impl(client, "checkout", limit=5)

    search = next(r for r in router.requests if r.url.path == "/wiki/rest/api/search")
    assert search.url.params["cql"] == 'type in (page, blogpost) AND text ~ "checkout"'
    assert search.url.params["limit"] == "5"
    assert "- **Checkout redesign > v2** [page, Engineering, id 100]" in output
    assert "  The checkout flow changes'" in output
    assert "7 matches in total" in output


# -- attachments --------------------------------------------------------------


def attachment_meta(**overrides: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "id": "att555",
        "title": "flow.png",
        "mediaType": "image/png",
        "fileSize": len(PNG_BYTES),
        "pageId": "100",
        "downloadLink": "/download/attachments/100/flow.png?api=v2",
    }
    meta.update(overrides)
    return meta


REST_DOWNLOAD = "/wiki/rest/api/content/100/child/attachment/att555/download"


async def test_image_attachment_is_downloaded_via_rest() -> None:
    routes: dict[str, Route] = {
        f"{V2}/attachments/att555": attachment_meta(),
        REST_DOWNLOAD: httpx.Response(200, content=PNG_BYTES),
    }
    client = make_client(Router(routes))
    image = await confluence.get_attachment_impl(client, "555")
    assert isinstance(image, Image)
    assert image.data == PNG_BYTES
    assert image._mime_type == "image/png"


async def test_download_falls_back_to_download_link() -> None:
    routes: dict[str, Route] = {
        f"{V2}/attachments/att555": attachment_meta(),
        "/wiki/download/attachments/100/flow.png": httpx.Response(
            200, content=PNG_BYTES
        ),
    }
    client = make_client(Router(routes))
    image = await confluence.get_attachment_impl(client, "att555")
    assert isinstance(image, Image)
    assert image.data == PNG_BYTES


async def test_non_image_attachment_is_rejected_without_download() -> None:
    routes: dict[str, Route] = {
        f"{V2}/attachments/att555": attachment_meta(
            title="spec.pdf", mediaType="application/pdf"
        )
    }
    router = Router(routes)
    client = make_client(router)
    with pytest.raises(ValueError, match="cannot be read"):
        await confluence.get_attachment_impl(client, "att555")
    assert not any("download" in path for path in router.paths())


def media_para(file_id: str) -> dict[str, Any]:
    return {
        "type": "mediaSingle",
        "content": [{"type": "media", "attrs": {"id": file_id, "type": "file"}}],
    }


def comment_image_routes(attachment_status: int = 200) -> dict[str, Route]:
    with_image = comment("c1", "u1", "see screenshot")
    with_image["body"] = adf(para("see screenshot"), media_para("file-in-comment"))
    reply = comment("c2", "u1", "and this one")
    reply["body"] = adf(media_para("file-in-reply"))
    attachments: Route = (
        httpx.Response(attachment_status)
        if attachment_status != 200
        else {
            "results": [
                {
                    "id": "att901",
                    "title": "bug.png",
                    "extensions": {"fileId": "file-in-comment"},
                }
            ]
        }
    )
    return {
        f"{V2}/pages/100": PAGE,
        f"{V2}/pages/100/footer-comments": {"results": [with_image]},
        f"{V2}/pages/100/inline-comments": {"results": []},
        f"{V2}/footer-comments/c1/children": {"results": [reply]},
        f"{V2}/footer-comments/c2/children": {"results": []},
        "/wiki/rest/api/content/c1/child/attachment": attachments,
        "/wiki/rest/api/content/c2/child/attachment": {
            "results": [
                {
                    "id": "att902",
                    "title": "reply.png",
                    "extensions": {"fileId": "file-in-reply"},
                }
            ]
        },
    }


async def test_comment_images_are_labelled_with_attachment_ids() -> None:
    client = make_client(Router(comment_image_routes()))
    output = await confluence.get_comments_impl(client, "100", limit=50)
    assert "[attachment: bug.png, id att901]" in output
    assert "[attachment: reply.png, id att902]" in output


async def test_comments_without_images_skip_attachment_lookups() -> None:
    router = Router(page_routes())
    client = make_client(router)
    await confluence.get_comments_impl(client, "100", limit=50)
    assert not any(path.endswith("/child/attachment") for path in router.paths())


async def test_comment_attachment_failure_still_renders_comment() -> None:
    client = make_client(Router(comment_image_routes(attachment_status=403)))
    output = await confluence.get_comments_impl(client, "100", limit=50)
    assert "see screenshot" in output
    assert "[attachment: file-in-comment]" in output


async def test_text_attachment_is_returned_as_text() -> None:
    content = b'{"retries": 3}'
    routes: dict[str, Route] = {
        f"{V2}/attachments/att555": attachment_meta(
            title="config.json", mediaType="application/json", fileSize=len(content)
        ),
        REST_DOWNLOAD: httpx.Response(200, content=content),
    }
    client = make_client(Router(routes))
    result = await confluence.get_attachment_impl(client, "att555")
    assert result == (
        'Attachment: config.json (application/json, 14 B)\n\n{"retries": 3}'
    )


async def test_page_hint_covers_text_attachments() -> None:
    routes = page_routes()
    routes[f"{V2}/pages/100/attachments"] = {
        "results": [
            {"id": "att7", "title": "spec.pdf", "mediaType": "application/pdf"},
            {"id": "att8", "title": "app.log", "mediaType": "text/plain"},
        ]
    }
    client = make_client(Router(routes))
    output = await confluence.get_page_impl(client, "100", include_comments=False)
    assert 'get_confluence_attachment(attachment_id="att8")' in output


def page_with_attachments_routes() -> dict[str, Route]:
    return {
        f"{V2}/pages/100": PAGE,
        f"{V2}/pages/100/attachments": {
            "results": [
                {"id": "att555", "title": "flow.png", "mediaType": "image/png"},
                {"id": "att556", "title": "spec.pdf", "mediaType": "application/pdf"},
                {"id": "att557", "title": "app.log", "mediaType": "text/plain"},
            ]
        },
    }


async def test_page_id_as_attachment_id_lists_readable_attachments() -> None:
    client = make_client(Router(page_with_attachments_routes()))
    with pytest.raises(ValueError, match="not an attachment ID") as exc:
        await confluence.get_attachment_impl(client, "100")
    message = str(exc.value)
    assert "flow.png = id att555" in message
    assert "app.log = id att557" in message
    assert "spec.pdf" not in message  # PDFs cannot be read, so not offered


async def test_page_url_as_attachment_id_lists_readable_attachments() -> None:
    client = make_client(Router(page_with_attachments_routes()))
    with pytest.raises(ValueError, match="flow.png = id att555"):
        await confluence.get_attachment_impl(
            client, f"{SITE}/wiki/spaces/ENG/pages/100/Checkout"
        )


async def test_unknown_numeric_id_keeps_the_404() -> None:
    client = make_client(Router({}))
    with pytest.raises(AtlassianError, match="404") as exc:
        await confluence.get_attachment_impl(client, "999")
    assert "/attachments/att999" in str(exc.value)


async def test_newest_first_sorts_top_level_comments_descending() -> None:
    router = Router(page_routes())
    client = make_client(router)
    await confluence.get_comments_impl(client, "100", limit=50, newest_first=True)
    sorts = {
        r.url.path: r.url.params.get("sort")
        for r in router.requests
        if "comments" in r.url.path
    }
    assert sorts[f"{V2}/pages/100/footer-comments"] == "-created-date"
    assert sorts[f"{V2}/pages/100/inline-comments"] == "-created-date"
    assert sorts[f"{V2}/footer-comments/c1/children"] == "created-date"
