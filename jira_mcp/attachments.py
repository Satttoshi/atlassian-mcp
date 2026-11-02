"""Reading attachments for Claude, shared by Jira and Confluence.

Images come back as vision input; text files (logs, JSON, CSV, source code,
...) come back as text. Metadata is checked before download, so known
binaries and oversized files never cost a transfer, and the payload is
re-checked afterwards because metadata can be stale.
"""

from __future__ import annotations

import codecs
import re
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from fastmcp.utilities.types import Image

from .render import format_size, is_text_attachment, needs_content_sniff

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

# The only mime types Claude can analyse visually, mapped to the format name
# FastMCP's Image helper expects. Besides these, only text files are served;
# known binaries (videos, PDFs, archives) are rejected before download.
IMAGE_FORMATS = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
}

# Text up to this size is returned inline (well under Claude Code's ~25k token
# MCP output limit). Longer text is saved in full to ATTACHMENT_CACHE_DIR and
# only a head/tail preview plus the file path is returned, so the model can
# grep or page through it instead of loading it all into context.
MAX_INLINE_TEXT_CHARS = 20_000
PREVIEW_LINES = 50
PREVIEW_MAX_CHARS = 4_000
ATTACHMENT_CACHE_DIR = Path(tempfile.gettempdir()) / "jira-mcp" / "attachments"

# How much of an untyped file is inspected to decide whether it is text.
SNIFF_BYTES = 8192


@dataclass(frozen=True)
class AttachmentMeta:
    id: str
    filename: str
    mime_type: str
    size: int


async def read_attachment(
    meta: AttachmentMeta, download: Callable[[], Awaitable[bytes]]
) -> Image | str:
    filename, mime_type = meta.filename, meta.mime_type.lower()
    image_format = IMAGE_FORMATS.get(mime_type)
    is_text = image_format is None and is_text_attachment(mime_type, filename)
    sniff = (
        image_format is None
        and not is_text
        and needs_content_sniff(mime_type, filename)
    )
    if image_format is None and not is_text and not sniff:
        supported = ", ".join(sorted(set(IMAGE_FORMATS)))
        raise ValueError(
            f"Attachment '{filename}' has type '{mime_type or 'unknown'}', which "
            f"cannot be read. Supported are images ({supported}) and text files "
            "(logs, JSON, XML, CSV, source code, ...). Videos, PDFs, archives "
            "and other binary files cannot be fetched."
        )
    if meta.size > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"Attachment '{filename}' is {format_size(meta.size)}, above the "
            f"{format_size(MAX_ATTACHMENT_BYTES)} limit for inline analysis."
        )

    data = await download()
    # Metadata can be stale or wrong; never trust it over the actual payload.
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"Attachment '{filename}' downloaded as {format_size(len(data))}, above "
            f"the {format_size(MAX_ATTACHMENT_BYTES)} limit for inline analysis."
        )
    if not data:
        raise ValueError(f"Attachment '{filename}' has no content.")
    if image_format is not None:
        return Image(data=data, format=image_format)
    if sniff and not _looks_like_text(data):
        raise ValueError(
            f"Attachment '{filename}' has type '{mime_type or 'unknown'}' and its "
            "content is not text, so it cannot be read."
        )
    text = _decode_text(data, filename)
    header = f"Attachment: {filename} ({mime_type or 'unknown type'}, {format_size(len(data))})"
    if len(text) <= MAX_INLINE_TEXT_CHARS:
        return f"{header}\n\n{text}"
    try:
        path = _save_text(meta.id, filename, text)
    except OSError as exc:
        raise ValueError(
            f"Attachment '{filename}' is too long to return inline and could "
            f"not be saved to {ATTACHMENT_CACHE_DIR}: {exc}"
        ) from exc
    return _render_saved_text(header, path, text)


def _decode_text(data: bytes, filename: str) -> str:
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    if b"\x00" in data:
        raise ValueError(
            f"Attachment '{filename}' looks like a binary file despite its "
            "name or type, so it cannot be read as text."
        )
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _looks_like_text(data: bytes) -> bool:
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return True
    sample = data[:SNIFF_BYTES]
    if b"\x00" in sample:
        return False
    try:
        # Incremental, so a multi-byte character cut off at the sample end is fine.
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
    except UnicodeDecodeError:
        return False
    return True


def _save_text(att_id: str, filename: str, text: str) -> Path:
    safe_name = re.sub(r"[^\w.-]+", "_", Path(filename).name).strip("._") or "file"
    ATTACHMENT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = ATTACHMENT_CACHE_DIR / f"{att_id}-{safe_name}"
    # Always UTF-8 on disk, whatever the original encoding, so grep just works.
    path.write_text(text, encoding="utf-8", newline="")
    return path


def _preview(lines: list[str], from_end: bool = False) -> str:
    text = "\n".join(lines)
    if len(text) <= PREVIEW_MAX_CHARS:
        return text
    if from_end:
        return "[...]" + text[-PREVIEW_MAX_CHARS:]
    return text[:PREVIEW_MAX_CHARS] + "[...]"


def _render_saved_text(header: str, path: Path, text: str) -> str:
    lines = text.splitlines()
    head = _preview(lines[:PREVIEW_LINES])
    tail = _preview(lines[-PREVIEW_LINES:], from_end=True)
    return (
        f"{header}\n\n"
        f"Too long to return inline ({len(lines):,} lines, {len(text):,} "
        f"characters). The full text is saved at:\n{path}\n"
        "Search it with grep or read it in ranges (Read with offset/limit) "
        "instead of loading it all.\n\n"
        f"--- first {min(PREVIEW_LINES, len(lines))} lines ---\n{head}\n\n"
        f"--- last {min(PREVIEW_LINES, len(lines))} lines ---\n{tail}"
    )
