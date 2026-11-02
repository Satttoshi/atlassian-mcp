"""Tests for get_attachment: images, text files, mime allowlist, 5 MB cap, ID checks."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.utilities.types import Image

from jira_mcp import attachments, server
from jira_mcp.attachments import (
    MAX_ATTACHMENT_BYTES,
    MAX_INLINE_TEXT_CHARS,
    PREVIEW_LINES,
    PREVIEW_MAX_CHARS,
    SNIFF_BYTES,
)
from jira_mcp.client import JiraClient
from jira_mcp.config import Settings
from jira_mcp.server import _get_attachment_impl, mcp

SITE = "https://example.atlassian.net"
ACCOUNT = {"accountId": "abc", "displayName": "Ada"}
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"x" * 64


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> JiraClient:
    settings = Settings(
        email="ada@x.io",
        api_token="secret",
        site_url=SITE,
        cloud_id=None,
        env_file_found=True,
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JiraClient(settings, http=http)


class AttachmentHandler:
    """Serves auth, attachment metadata, and attachment content."""

    def __init__(self, meta: dict[str, Any], content: bytes = PNG_BYTES) -> None:
        self.meta = meta
        self.content = content
        self.content_requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{SITE}/rest/api/3/myself":
            return httpx.Response(200, json=ACCOUNT)
        if url == f"{SITE}/rest/api/3/attachment/{self.meta['id']}":
            return httpx.Response(200, json=self.meta)
        if url == f"{SITE}/rest/api/3/attachment/content/{self.meta['id']}":
            self.content_requests += 1
            return httpx.Response(
                200,
                content=self.content,
                headers={"Content-Type": str(self.meta.get("mimeType", ""))},
            )
        return httpx.Response(404)


def png_meta(**overrides: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "id": "10023",
        "filename": "screenshot.png",
        "mimeType": "image/png",
        "size": len(PNG_BYTES),
    }
    meta.update(overrides)
    return meta


async def test_image_attachment_is_returned() -> None:
    client = make_client(AttachmentHandler(png_meta()))
    image = await _get_attachment_impl(client, "10023")
    assert isinstance(image, Image)
    assert image.data == PNG_BYTES
    assert image._mime_type == "image/png"


async def test_attachment_id_is_trimmed() -> None:
    client = make_client(AttachmentHandler(png_meta()))
    image = await _get_attachment_impl(client, " 10023 ")
    assert isinstance(image, Image)
    assert image.data == PNG_BYTES


async def test_jpeg_mime_maps_to_image_jpeg() -> None:
    meta = png_meta(filename="shot.jpg", mimeType="image/jpeg")
    client = make_client(AttachmentHandler(meta))
    image = await _get_attachment_impl(client, "10023")
    assert isinstance(image, Image)
    assert image._mime_type == "image/jpeg"


async def test_integer_id_is_accepted() -> None:
    # Models often send the ID as a JSON number; that must not cost a retry.
    client = make_client(AttachmentHandler(png_meta()))
    image = await _get_attachment_impl(client, 10023)
    assert isinstance(image, Image)
    assert image.data == PNG_BYTES


@pytest.mark.parametrize("bad_id", ["", "abc def", "10023; DROP"])
async def test_garbage_id_is_rejected(bad_id: str) -> None:
    client = make_client(AttachmentHandler(png_meta()))
    with pytest.raises(ValueError, match="attachment ID"):
        await _get_attachment_impl(client, bad_id)


async def test_issue_key_error_lists_the_valid_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{SITE}/rest/api/3/myself":
            return httpx.Response(200, json=ACCOUNT)
        if url.startswith(f"{SITE}/rest/api/3/issue/PROJ-1"):
            return httpx.Response(
                200, json={"fields": {"attachment": [png_meta(), png_meta(id="99")]}}
            )
        return httpx.Response(404)

    client = make_client(handler)
    with pytest.raises(ValueError, match="issue key, not an attachment ID") as exc:
        await _get_attachment_impl(client, "proj-1")
    assert "screenshot.png = id 10023" in str(exc.value)
    assert "id 99" in str(exc.value)


async def test_issue_key_without_attachments_says_so() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{SITE}/rest/api/3/myself":
            return httpx.Response(200, json=ACCOUNT)
        if url.startswith(f"{SITE}/rest/api/3/issue/PROJ-1"):
            return httpx.Response(200, json={"fields": {"attachment": []}})
        return httpx.Response(404)

    client = make_client(handler)
    with pytest.raises(ValueError, match="no attachments"):
        await _get_attachment_impl(client, "PROJ-1")


@pytest.mark.parametrize(
    ("filename", "mime"),
    [
        ("demo.mp4", "video/mp4"),
        ("demo.mov", "video/quicktime"),
        ("spec.pdf", "application/pdf"),
        ("logs.zip", "application/zip"),
        ("unknown.bin", ""),
        ("crash.dmp", "application/octet-stream"),
        ("release.apk", "binary/octet-stream"),
    ],
)
async def test_unsupported_types_are_rejected_without_download(
    filename: str, mime: str
) -> None:
    handler = AttachmentHandler(png_meta(filename=filename, mimeType=mime))
    client = make_client(handler)
    with pytest.raises(ValueError, match="cannot be read"):
        await _get_attachment_impl(client, "10023")
    assert handler.content_requests == 0


async def test_oversized_metadata_is_rejected_without_download() -> None:
    handler = AttachmentHandler(png_meta(size=MAX_ATTACHMENT_BYTES + 1))
    client = make_client(handler)
    with pytest.raises(ValueError, match="5.0 MB limit"):
        await _get_attachment_impl(client, "10023")
    assert handler.content_requests == 0


async def test_oversized_actual_content_is_rejected() -> None:
    # Metadata claims a small size but the real payload is over the cap.
    handler = AttachmentHandler(
        png_meta(size=100), content=b"x" * (MAX_ATTACHMENT_BYTES + 1)
    )
    client = make_client(handler)
    with pytest.raises(ValueError, match="5.0 MB limit"):
        await _get_attachment_impl(client, "10023")


async def test_empty_content_is_rejected() -> None:
    handler = AttachmentHandler(png_meta(size=0), content=b"")
    client = make_client(handler)
    with pytest.raises(ValueError, match="no content"):
        await _get_attachment_impl(client, "10023")


def text_meta(content: bytes, **overrides: Any) -> dict[str, Any]:
    return png_meta(
        filename="app.log", mimeType="text/plain", size=len(content), **overrides
    )


@pytest.mark.parametrize(
    ("filename", "mime"),
    [
        ("app.log", "text/plain"),
        ("data.csv", "text/csv"),
        ("payload.json", "application/json"),
        ("session.har", "application/json"),
        ("config.yaml", "application/x-yaml"),
        ("feed.xml", "application/atom+xml"),
        ("server.log", "application/octet-stream"),
        ("trace.txt", ""),
    ],
)
async def test_text_attachment_is_returned_as_text(filename: str, mime: str) -> None:
    content = b"line one\nline two\n"
    meta = text_meta(content) | {"filename": filename, "mimeType": mime}
    client = make_client(AttachmentHandler(meta, content=content))
    result = await _get_attachment_impl(client, "10023")
    assert isinstance(result, str)
    assert result.startswith(f"Attachment: {filename} (")
    assert result.endswith("line one\nline two\n")


async def test_utf8_bom_is_stripped() -> None:
    content = "\ufeffGrüße".encode()
    client = make_client(AttachmentHandler(text_meta(content), content=content))
    result = await _get_attachment_impl(client, "10023")
    assert isinstance(result, str)
    assert result.endswith("\n\nGrüße")


async def test_utf16_with_bom_is_decoded() -> None:
    content = "Windows log ü".encode("utf-16")
    client = make_client(AttachmentHandler(text_meta(content), content=content))
    result = await _get_attachment_impl(client, "10023")
    assert isinstance(result, str)
    assert result.endswith("Windows log ü")


async def test_non_utf8_text_falls_back_to_cp1252() -> None:
    content = "Größe".encode("cp1252")
    client = make_client(AttachmentHandler(text_meta(content), content=content))
    result = await _get_attachment_impl(client, "10023")
    assert isinstance(result, str)
    assert result.endswith("Größe")


async def test_binary_content_behind_text_name_is_rejected() -> None:
    content = b"\x7fELF\x00\x00\x01"
    client = make_client(AttachmentHandler(text_meta(content), content=content))
    with pytest.raises(ValueError, match="looks like a binary file"):
        await _get_attachment_impl(client, "10023")


@pytest.mark.parametrize(
    ("filename", "mime"),
    [
        ("Dockerfile", "application/octet-stream"),
        ("app.log.1", ""),
        ("Makefile", "binary/octet-stream"),
        ("output.weird", "application/unknown"),
    ],
)
async def test_untyped_text_is_detected_by_content(filename: str, mime: str) -> None:
    content = "FROM node:22\nRUN echo grüß\n".encode()
    meta = text_meta(content) | {"filename": filename, "mimeType": mime}
    handler = AttachmentHandler(meta, content=content)
    result = await _get_attachment_impl(make_client(handler), "10023")
    assert handler.content_requests == 1
    assert isinstance(result, str)
    assert result.endswith("RUN echo grüß\n")


@pytest.mark.parametrize(
    "content",
    [
        b"\x7fELF\x02\x01\x01\x00\x00",
        "Größe".encode("cp1252"),
    ],
    ids=["nul-bytes", "invalid-utf8"],
)
async def test_untyped_binary_is_rejected_after_sniffing(content: bytes) -> None:
    meta = text_meta(content) | {"filename": "blob", "mimeType": ""}
    client = make_client(AttachmentHandler(meta, content=content))
    with pytest.raises(ValueError, match="content is not text"):
        await _get_attachment_impl(client, "10023")


async def test_untyped_utf16_with_bom_is_detected_by_content() -> None:
    content = "Windows log ü".encode("utf-16")
    meta = text_meta(content) | {"filename": "blob", "mimeType": ""}
    client = make_client(AttachmentHandler(meta, content=content))
    result = await _get_attachment_impl(client, "10023")
    assert isinstance(result, str)
    assert result.endswith("Windows log ü")


async def test_sniffing_tolerates_character_split_at_sample_end() -> None:
    content = b"a" * (SNIFF_BYTES - 1) + "ü".encode()
    meta = text_meta(content) | {"filename": "blob", "mimeType": ""}
    client = make_client(AttachmentHandler(meta, content=content))
    result = await _get_attachment_impl(client, "10023")
    assert isinstance(result, str)
    assert result.endswith("aü")


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "cache"
    monkeypatch.setattr(attachments, "ATTACHMENT_CACHE_DIR", path)
    return path


async def test_text_at_inline_limit_is_returned_inline(cache_dir: Path) -> None:
    content = b"a" * MAX_INLINE_TEXT_CHARS
    client = make_client(AttachmentHandler(text_meta(content), content=content))
    result = await _get_attachment_impl(client, "10023")
    assert isinstance(result, str)
    assert result.endswith(content.decode())
    assert not cache_dir.exists()


def long_log(line_count: int) -> str:
    return "".join(f"line {i:05d}\n" for i in range(line_count))


async def test_long_text_is_saved_in_full_with_preview(cache_dir: Path) -> None:
    text = long_log(5_000)
    assert len(text) > MAX_INLINE_TEXT_CHARS
    content = text.encode()
    meta = text_meta(content) | {"filename": "debug logs VON 9.18.txt"}
    client = make_client(AttachmentHandler(meta, content=content))
    result = await _get_attachment_impl(client, "10023")

    assert isinstance(result, str)
    saved = cache_dir / "10023-debug_logs_VON_9.18.txt"
    assert saved.read_text(encoding="utf-8") == text
    assert result.startswith("Attachment: debug logs VON 9.18.txt (")
    assert str(saved) in result
    assert "5,000 lines" in result
    assert "line 00000" in result
    assert f"line {PREVIEW_LINES - 1:05d}" in result
    assert f"line {PREVIEW_LINES:05d}" not in result
    assert "line 04999" in result
    assert "line 02500" not in result
    assert len(result) < MAX_INLINE_TEXT_CHARS


async def test_saved_text_is_utf8_whatever_the_source_encoding(
    cache_dir: Path,
) -> None:
    text = "Größe ü\n" * 5_000
    content = text.encode("utf-16")
    client = make_client(AttachmentHandler(text_meta(content), content=content))
    await _get_attachment_impl(client, "10023")
    assert (cache_dir / "10023-app.log").read_text(encoding="utf-8") == text


async def test_preview_of_huge_single_line_is_capped(cache_dir: Path) -> None:
    content = b'{"minified": "' + b"x" * 200_000 + b'"}'
    client = make_client(AttachmentHandler(text_meta(content), content=content))
    result = await _get_attachment_impl(client, "10023")
    assert isinstance(result, str)
    assert len(result) < 2 * PREVIEW_MAX_CHARS + 1_000
    assert (cache_dir / "10023-app.log").read_bytes() == content


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("../../etc/passwd", "10023-passwd"),
        ("..", "10023-file"),
        ("a b/c?.log", "10023-c_.log"),
    ],
)
async def test_saved_filename_is_sanitised(
    cache_dir: Path, filename: str, expected: str
) -> None:
    content = long_log(5_000).encode()
    meta = text_meta(content) | {"filename": filename}
    client = make_client(AttachmentHandler(meta, content=content))
    await _get_attachment_impl(client, "10023")
    assert [p.name for p in cache_dir.iterdir()] == [expected]


async def test_unwritable_cache_dir_gives_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(attachments, "ATTACHMENT_CACHE_DIR", blocker / "cache")
    content = long_log(5_000).encode()
    client = make_client(AttachmentHandler(text_meta(content), content=content))
    with pytest.raises(ValueError, match="could not be saved"):
        await _get_attachment_impl(client, "10023")


async def test_tool_returns_text_and_image_content_over_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"stack trace here"
    handlers = {
        "1": AttachmentHandler(text_meta(content, id="1"), content=content),
        "2": AttachmentHandler(png_meta(id="2")),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        att_id = str(request.url).rstrip("/").rpartition("/")[2]
        return handlers.get(att_id, handlers["1"])(request)

    monkeypatch.setattr(server, "_client", make_client(handler))
    async with Client(mcp) as mcp_client:
        text_result = await mcp_client.call_tool(
            "get_attachment", {"attachment_id": "1"}
        )
        image_result = await mcp_client.call_tool(
            "get_attachment", {"attachment_id": "2"}
        )
    assert [c.type for c in text_result.content] == ["text"]
    assert "stack trace here" in text_result.content[0].text
    assert [c.type for c in image_result.content] == ["image"]
