"""Deterministic, dependency-light artifact text extraction."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
import re
import subprocess
from typing import Iterable


EXTRACTOR_VERSION = "1.2"


class ExtractionError(Exception):
    """Raised when an artifact cannot be extracted safely."""


@dataclass(frozen=True)
class Unit:
    kind: str
    number: int
    anchor: str | None
    title: str
    text: str


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    parent: "_Node | None" = None
    children: list["_Node"] = field(default_factory=list)
    data: str = ""


_IGNORED_TAGS = {"script", "style", "template", "nav"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCK_TAGS = {"address", "article", "aside", "blockquote", "br", "div", "dl", "dt", "dd", "footer", "header", "hr", "li", "main", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul"} | _HEADING_TAGS


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document")
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag.lower(), {key.lower(): value or "" for key, value in attrs})
        node.parent = self._stack[-1]
        node.parent.children.append(node)
        self._stack.append(node)
        if tag.lower() in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self._stack.pop()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self._stack[-1].tag == tag.lower():
            self._stack.pop()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        parent = self._stack[-1]
        text_node = _Node("#text", parent=parent, data=data)
        parent.children.append(text_node)


def extract(data: bytes, suffix: str, filename: str) -> list[Unit]:
    """Extract addressable text units from one artifact snapshot."""
    del filename  # Kept in the public contract for callers and diagnostics.
    normalized_suffix = suffix.lower()
    if not normalized_suffix.startswith("."):
        normalized_suffix = "." + normalized_suffix
    if normalized_suffix in {".html", ".htm"}:
        try:
            return _extract_html(data)
        except RecursionError as exc:
            raise ExtractionError('HTML nesting exceeds prototype limit') from exc
    if normalized_suffix == ".pdf":
        return _extract_pdf(data)
    if normalized_suffix in {".md", ".markdown"}:
        return _extract_markdown(data)
    if normalized_suffix == ".txt":
        return _single_text_unit(data)
    raise ExtractionError("Unsupported artifact format")


def _is_ignored(node: _Node) -> bool:
    current: _Node | None = node
    while current is not None:
        if current.tag in _IGNORED_TAGS:
            return True
        current = current.parent
    return False


def _classes(node: _Node) -> set[str]:
    return set(node.attrs.get("class", "").split())


def _is_note(node: _Node) -> bool:
    classes = _classes(node)
    return (
        "notes" in classes
        or "speaker-notes" in classes
        or "speaker_notes" in classes
        or "notes" in node.attrs
        or "data-notes" in node.attrs
        or "data-speaker-notes" in node.attrs
    )


def _descendants(node: _Node) -> Iterable[_Node]:
    for child in node.children:
        yield child
        yield from _descendants(child)


def _all_nodes(root: _Node) -> list[_Node]:
    return list(_descendants(root))


def _text(node: _Node, *, skip_notes: bool = False, skip_nested_slides: bool = False) -> str:
    """Render text while keeping punctuation and separating adjacent words."""
    chunks: list[str] = []

    def visit(current: _Node) -> None:
        if skip_nested_slides and current is not node and _nested_in_slide(current, node):
            return
        if _is_ignored(current) or (skip_notes and _is_note(current)):
            return
        if current.tag in _BLOCK_TAGS:
            chunks.append(" ")
        if current.data:
            chunks.append(current.data)
        for child in current.children:
            visit(child)
        if current.tag in _BLOCK_TAGS:
            chunks.append(" ")

    visit(node)
    return " ".join("".join(chunks).split())


def _notes(node: _Node) -> list[str]:
    return [_text(candidate) for candidate in _all_nodes(node) if _is_note(candidate) and _text(candidate)]


def _title(node: _Node) -> str:
    for candidate in _all_nodes(node):
        if (
            candidate.tag in _HEADING_TAGS
            and not _is_ignored(candidate)
            and not _is_note(candidate)
            and not _nested_in_slide(candidate, node)
        ):
            value = _text(candidate)
            if value:
                return value
    if node.tag == "#document":
        for candidate in _all_nodes(node):
            if candidate.tag == "title" and not _is_ignored(candidate):
                value = _text(candidate)
                if value:
                    return value
    return ""


def _unit_text(node: _Node) -> tuple[str, str]:
    visible = _text(node, skip_notes=True, skip_nested_slides=True)
    notes = [
        _text(candidate)
        for candidate in _all_nodes(node)
        if _is_note(candidate)
        and not _nested_in_slide(candidate, node)
        and _text(candidate)
    ]
    return _title(node), "\n\n".join(part for part in [visible, *notes] if part)


def _is_slide(node: _Node) -> bool:
    return "slide" in _classes(node) or "data-slide" in node.attrs


def _has_ancestor(node: _Node, predicate) -> bool:
    current = node.parent
    while current is not None:
        if predicate(current):
            return True
        current = current.parent
    return False


def _nested_in_slide(node: _Node, owner: _Node) -> bool:
    current = node.parent
    while current is not None and current is not owner:
        if _is_slide(current):
            return True
        current = current.parent
    return False


def _slide_nodes(root: _Node) -> list[_Node]:
    nodes = _all_nodes(root)
    explicit = [node for node in nodes if _is_slide(node) and not _is_ignored(node)]
    outermost = [node for node in explicit if not _has_ancestor(node, _is_slide)]
    if outermost:
        return outermost

    reveal_containers = [node for node in nodes if "slides" in _classes(node) and not _is_ignored(node)]
    reveal_sections: list[_Node] = []
    seen: set[int] = set()
    for container in reveal_containers:
        for node in _descendants(container):
            if node.tag != "section" or _is_ignored(node) or id(node) in seen:
                continue
            if any(child.tag == "section" for child in _descendants(node)):
                continue
            reveal_sections.append(node)
            seen.add(id(node))
    return reveal_sections


def _html_units(root: _Node, nodes: list[_Node], kind: str) -> list[Unit]:
    ids = Counter(node.attrs.get("id", "") for node in _all_nodes(root) if node.attrs.get("id", ""))
    units: list[Unit] = []
    for number, node in enumerate(nodes, 1):
        title, text = _unit_text(node)
        raw_id = node.attrs.get("id", "")
        anchor = raw_id if raw_id and ids[raw_id] == 1 else None
        units.append(Unit(kind, number, anchor, title, text))
    return units


def _extract_html(data: bytes) -> list[Unit]:
    parser = _DocumentParser()
    try:
        parser.feed(data.decode("utf-8", errors="replace"))
        parser.close()
    except Exception as exc:  # HTMLParser should be permissive, but keep the API stable.
        raise ExtractionError("Unable to parse HTML document") from exc
    root = parser.root
    slides = _slide_nodes(root)
    if slides:
        return _html_units(root, slides, "slide")

    sections = [node for node in _all_nodes(root) if node.tag == "section" and not _is_ignored(node)]
    sections = [node for node in sections if not _has_ancestor(node, lambda ancestor: ancestor.tag == "section")]
    if sections:
        return _html_units(root, sections, "section")
    title, text = _unit_text(root)
    return [Unit("section", 1, None, title, text)]


def _extract_pdf(data: bytes) -> list[Unit]:
    try:
        result = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", "-layout", "-", "-"],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExtractionError("Unable to extract PDF document") from exc
    if result.returncode != 0:
        raise ExtractionError("Unable to extract PDF document")
    try:
        rendered = result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExtractionError("Unable to extract PDF document") from exc
    pages = rendered.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    units: list[Unit] = []
    for number, page in enumerate(pages, 1):
        text = " ".join(page.split())
        if text:
            units.append(Unit("page", number, None, "", text))
    return units


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _single_text_unit(data: bytes) -> list[Unit]:
    text = " ".join(_decode(data).split())
    return [Unit("section", 1, None, "", text)]


def _extract_markdown(data: bytes) -> list[Unit]:
    source = _decode(data)
    heading = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
    matches = list(heading.finditer(source, re.MULTILINE))
    if not matches:
        return _single_text_unit(data)
    units: list[Unit] = []
    for number, match in enumerate(matches, 1):
        end = matches[number].start() if number < len(matches) else len(source)
        title = " ".join(match.group(2).split())
        body = source[match.end() : end]
        text = " ".join((title + " " + body).split())
        units.append(Unit("section", number, None, title, text))
    return units
