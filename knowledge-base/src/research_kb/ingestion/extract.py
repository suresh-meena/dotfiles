from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from research_kb.domain.canonical import hash_bytes, hash_text

TEXT_EXTENSIONS = {".md", ".markdown", ".txt", ".rst", ".tex", ".py", ".sh", ".c", ".h", ".cpp", ".json", ".yaml", ".yml", ".toml", ".csv", ".tsv"}
NOTEBOOK_EXTENSIONS = {".ipynb"}
HTML_EXTENSIONS = {".html", ".htm", ".xhtml"}
PDF_EXTENSIONS = {".pdf"}

CHUNK_TARGET_TOKENS = 650
CHUNK_MAX_TOKENS = 900
CHUNK_MIN_TOKENS = 80


@dataclass
class Extraction:
    status: str
    text: str = ""
    parser_name: str = "builtin"
    parser_version: str = "1"
    omissions: list[str] = field(default_factory=list)
    sections: list[dict[str, Any]] = field(default_factory=list)


def _approx_tokens(text: str) -> int:
    return max(1, len(text.split()))


def _chunk_section(title: str, body: str, heading_path: list[str]) -> list[dict[str, Any]]:
    if not body.strip():
        return []
    paragraphs = re.split(r"\n\s*\n", body)
    chunks: list[dict[str, Any]] = []
    current: list[str] = []
    current_tokens = 0
    index = 0
    for paragraph in paragraphs:
        tokens = _approx_tokens(paragraph)
        if current and (
            current_tokens + tokens > CHUNK_MAX_TOKENS or current_tokens >= CHUNK_TARGET_TOKENS
        ):
            chunks.append(
                {
                    "section_key": f"{'|'.join(heading_path)}#{index}",
                    "title": title,
                    "body": "\n\n".join(current),
                    "heading_path": list(heading_path),
                }
            )
            index += 1
            current = []
            current_tokens = 0
        current.append(paragraph)
        current_tokens += tokens
    if current and (current_tokens >= CHUNK_MIN_TOKENS or not chunks):
        chunks.append(
            {
                "section_key": f"{'|'.join(heading_path)}#{index}",
                "title": title,
                "body": "\n\n".join(current),
                "heading_path": list(heading_path),
            }
        )
    return chunks


def chunk_markdown(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    sections: list[dict[str, Any]] = []
    heading_path: list[str] = []
    current_title = "document"
    buffer: list[str] = []
    for line in lines:
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            if buffer:
                sections.extend(_chunk_section(current_title, "\n".join(buffer), heading_path))
            level = len(match.group(1))
            heading_path = heading_path[: level - 1]
            heading_path.append(match.group(2).strip())
            current_title = match.group(2).strip()
            buffer = []
        else:
            buffer.append(line)
    if buffer:
        sections.extend(_chunk_section(current_title, "\n".join(buffer), heading_path))
    if not sections:
        sections = _chunk_section("document", text, ["document"])
    return sections


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def extract(path: Path) -> Extraction:
    suffix = path.suffix.lower()
    if suffix in PDF_EXTENSIONS:
        return Extraction(
            status="needs_visual_check",
            text="",
            parser_name="none",
            parser_version="0",
            omissions=["pdf_extraction_unsupported", "preserve_original_page_region"],
        )
    if suffix in NOTEBOOK_EXTENSIONS:
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except ValueError:
            return Extraction(status="unsupported", omissions=["notebook_json_unreadable"])
        cells = payload.get("cells", [])
        parts = []
        for index, cell in enumerate(cells):
            source = cell.get("source", [])
            text = "".join(source) if isinstance(source, list) else str(source)
            parts.append(f"## cell {index}\n{text}")
        body = "\n\n".join(parts)
        return Extraction(status="extracted", text=body, parser_name="notebook_json", parser_version="1")
    if suffix in HTML_EXTENSIONS:
        extractor = _HtmlTextExtractor()
        extractor.feed(path.read_text(encoding="utf-8", errors="replace"))
        body = html.unescape("\n".join(part.strip() for part in extractor.parts if part.strip()))
        return Extraction(
            status="extracted",
            text=body,
            parser_name="html_parser",
            parser_version="1",
            omissions=["formatting_lost"],
        )
    if suffix in TEXT_EXTENSIONS or suffix == "":
        try:
            body = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            body = path.read_text(encoding="utf-8", errors="replace")
            return Extraction(
                status="ocr_uncertain",
                text=body,
                parser_name="utf8_replace",
                parser_version="1",
                omissions=["non_utf8_bytes_replaced"],
            )
        return Extraction(status="extracted", text=body, parser_name="utf8_text", parser_version="1")
    return Extraction(status="unsupported", omissions=[f"unsupported_extension:{suffix or 'none'}"])


def sections_for_extraction(extraction: Extraction) -> list[dict[str, Any]]:
    if extraction.status not in ("extracted", "ocr_uncertain") or not extraction.text:
        return []
    if extraction.parser_name in ("notebook_json",):
        sections = _chunk_section("notebook", extraction.text, ["notebook"])
    else:
        sections = chunk_markdown(extraction.text)
    return attach_line_ranges(extraction.text, sections)


def attach_line_ranges(text: str, sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cursor = 0
    for section in sections:
        body = section.get("body", "")
        index = text.find(body, cursor)
        if index < 0:
            index = text.find(body)
        if index < 0:
            section["line_start"] = None
            section["line_end"] = None
            continue
        section["line_start"] = text[:index].count("\n") + 1
        section["line_end"] = section["line_start"] + body.count("\n")
        cursor = index + len(body)
    return sections


def line_anchor(text: str, line_start: int, line_end: int) -> dict[str, Any]:
    lines = text.splitlines()
    excerpt = "\n".join(lines[max(0, line_start - 1) : line_end])
    return {
        "anchor_kind": "markdown_text",
        "coordinate_system": "line_range_1based",
        "locator": {"line_start": line_start, "line_end": line_end},
        "excerpt": excerpt,
        "excerpt_hash": hash_text(excerpt),
    }


def find_excerpt_anchor(text: str, excerpt: str) -> dict[str, Any]:
    needle = excerpt.strip()
    if not needle:
        raise ValueError("Excerpt is empty.")
    index = text.find(needle)
    if index < 0:
        return {
            "anchor_kind": "markdown_text",
            "coordinate_system": "excerpt_hash_only",
            "locator": {"byte_offset": None},
            "excerpt": excerpt,
            "excerpt_hash": hash_text(excerpt),
            "status": "needs_visual_check",
        }
    start_line = text[:index].count("\n") + 1
    end_line = start_line + needle.count("\n")
    anchor = line_anchor(text, start_line, end_line)
    anchor["locator"]["byte_offset"] = index
    return anchor


def extraction_blob_bytes(extraction: Extraction) -> bytes:
    return extraction.text.encode("utf-8")


def extraction_pipeline_hash(parser_name: str, parser_version: str) -> str:
    return hash_bytes(f"{parser_name}:{parser_version}".encode("utf-8"))
