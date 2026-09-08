"""Render Gemini Markdown for Telegram and split long source messages.

The renderer deliberately keeps LaTeX source intact and wraps ``$...$`` and
``$$...$$`` expressions in ``<code>``.  Telegram cannot typeset LaTeX, and a
Unicode approximation would be lossy (especially for matrices and custom
commands); showing the original expression is deterministic and copyable.

Only the Python standard library is used.  Every function in this module is
pure: no Telegram objects, network access, or process-wide state are involved.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import re
from urllib.parse import urlsplit


MAX_MESSAGE_LENGTH = 4000
UNORDERED_LIST_BULLETS = ("•", "◦", "▪")
LIST_INDENT_CHARACTER = "\u2007"
LIST_INDENT_CHARACTERS_PER_LEVEL = 2

GOOGLEUSERCONTENT_ARTIFACT_RE = re.compile(
    r"https?://googleusercontent\.com/(?:\w+/)+\d+(?:_\d+)*\n*"
)
ORPHAN_ARTIFACT_SUFFIX_RE = re.compile(r"(?m)^[ \t]*_\d+[ \t]*$\n?")
AGENT_TAG_RE = re.compile(
    r'<[A-Z][A-Za-z0-9_]*(?:\s+[A-Za-z_][\w.-]*\s*=\s*"[^"]*")*\s*/>'
)

_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,})([^`]*)$")
_LIST_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-+*]|\d+[.)])\s+(?P<body>.*)$")
_HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s+(?P<body>.*?)(?:\s+#+)?$")
_HORIZONTAL_RULE_RE = re.compile(r"(?m)^ {0,3}-{3,}[ \t]*\r?$")
_EXCESS_BLANK_LINES_RE = re.compile(
    r"\r?\n[ \t]*\r?\n(?:[ \t]*\r?\n)+"
)
_BLANK_LINE_WHITESPACE_RE = re.compile(r"(?m)^[ \t]+(?=\r?$)")
_LEADING_BLANK_LINES_RE = re.compile(r"^(?:[ \t]*\r?\n)+")
_TRAILING_BLANK_LINES_RE = re.compile(r"(?:\r?\n[ \t]*)+$")
_LANGUAGE_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")
_SAFE_LINK_SCHEMES = frozenset({"http", "https", "mailto", "tg"})


@dataclass(frozen=True, slots=True)
class _TextBlock:
    raw: str


@dataclass(frozen=True, slots=True)
class _CodeBlock:
    raw: str
    fence: str
    info: str
    content: str
    closed: bool


def _without_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\n"
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1], "\n"
    return line, ""


def _opening_fence(line: str) -> tuple[str, str] | None:
    body, _ = _without_line_ending(line)
    match = _FENCE_OPEN_RE.fullmatch(body)
    if match is None:
        return None
    return match.group(1), match.group(2).strip()


def _is_closing_fence(line: str, minimum_length: int) -> bool:
    body, _ = _without_line_ending(line)
    return re.fullmatch(rf" {{0,3}}`{{{minimum_length},}}[ \t]*", body) is not None


def _parse_blocks(markdown: str) -> list[_TextBlock | _CodeBlock]:
    lines = markdown.splitlines(keepends=True)
    blocks: list[_TextBlock | _CodeBlock] = []
    plain: list[str] = []
    index = 0

    while index < len(lines):
        opening = _opening_fence(lines[index])
        if opening is None:
            plain.append(lines[index])
            index += 1
            continue

        if plain:
            blocks.append(_TextBlock("".join(plain)))
            plain.clear()

        fence, info = opening
        raw_lines = [lines[index]]
        content_lines: list[str] = []
        closed = False
        index += 1
        while index < len(lines):
            line = lines[index]
            raw_lines.append(line)
            index += 1
            if _is_closing_fence(line, len(fence)):
                closed = True
                break
            content_lines.append(line)

        blocks.append(
            _CodeBlock(
                raw="".join(raw_lines),
                fence=fence,
                info=info,
                content="".join(content_lines),
                closed=closed,
            )
        )

    if plain:
        blocks.append(_TextBlock("".join(plain)))
    return blocks


def _is_escaped(text: str, position: int) -> bool:
    backslashes = 0
    position -= 1
    while position >= 0 and text[position] == "\\":
        backslashes += 1
        position -= 1
    return backslashes % 2 == 1


def _find_unescaped(text: str, needle: str, start: int) -> int:
    position = text.find(needle, start)
    while position >= 0:
        if not _is_escaped(text, position):
            return position
        position = text.find(needle, position + len(needle))
    return -1


def _find_emphasis_close(text: str, start: int, delimiter: str) -> int:
    position = start
    while position < len(text):
        position = text.find("*", position)
        if position < 0:
            return -1
        if _is_escaped(text, position):
            position += 1
            continue

        run_end = position
        while run_end < len(text) and text[run_end] == "*":
            run_end += 1
        run_length = run_end - position
        if delimiter == "**" and run_length >= 2:
            return run_end - 2
        if delimiter == "*" and run_length % 2 == 1:
            return run_end - 1
        position = run_end
    return -1


def _matching_bracket(text: str, start: int, opening: str, closing: str) -> int:
    depth = 1
    position = start
    while position < len(text):
        character = text[position]
        if character == "\\":
            position += 2
            continue
        if character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return position
        position += 1
    return -1


def _link_destination(raw: str) -> str:
    destination = raw.strip()
    if destination.startswith("<") and ">" in destination:
        return destination[1 : destination.index(">")]

    # A Markdown title is not meaningful to Telegram.  Drop it while keeping
    # escaped spaces in a bare destination intact.
    match = re.match(r"(?:\\.|[^\s])+", destination)
    return match.group(0).replace("\\ ", " ") if match else ""


def _safe_link(url: str) -> bool:
    if not url or any(character in url for character in "\r\n\x00"):
        return False
    return urlsplit(url).scheme.lower() in _SAFE_LINK_SCHEMES


def _render_link(text: str, position: int, *, image: bool) -> tuple[str, int] | None:
    label_start = position + (2 if image else 1)
    label_end = _matching_bracket(text, label_start, "[", "]")
    if label_end < 0 or label_end + 1 >= len(text) or text[label_end + 1] != "(":
        return None
    target_end = _matching_bracket(text, label_end + 2, "(", ")")
    if target_end < 0:
        return None

    label = _render_inline(text[label_start:label_end])
    url = _link_destination(text[label_end + 2 : target_end])
    if not _safe_link(url):
        suffix = f" ({escape(url, quote=False)})" if url else ""
        return label + suffix, target_end + 1
    if image:
        # Telegram HTML has no image element.  Preserve useful information as
        # a normal link instead of emitting unsupported markup.
        return f'<a href="{escape(url, quote=True)}">{label or escape(url)}</a>', target_end + 1
    return f'<a href="{escape(url, quote=True)}">{label}</a>', target_end + 1


def _render_inline(text: str) -> str:
    rendered: list[str] = []
    position = 0
    while position < len(text):
        if text[position] == "\\" and position + 1 < len(text):
            rendered.append(escape(text[position + 1], quote=False))
            position += 2
            continue

        if text.startswith("![", position):
            link = _render_link(text, position, image=True)
            if link is not None:
                value, position = link
                rendered.append(value)
                continue

        if text[position] == "[":
            link = _render_link(text, position, image=False)
            if link is not None:
                value, position = link
                rendered.append(value)
                continue

        if text[position] == "`":
            closing = _find_unescaped(text, "`", position + 1)
            if closing >= 0:
                rendered.append(f"<code>{escape(text[position + 1 : closing], quote=False)}</code>")
                position = closing + 1
                continue

        if text.startswith("$$", position):
            closing = _find_unescaped(text, "$$", position + 2)
            if closing >= 0 and text[position + 2 : closing].strip():
                rendered.append(f"<code>{escape(text[position : closing + 2], quote=False)}</code>")
                position = closing + 2
                continue

        if text[position] == "$" and not text.startswith("$$", position):
            closing = _find_unescaped(text, "$", position + 1)
            while closing >= 0 and closing + 1 < len(text) and text[closing + 1] == "$":
                closing = _find_unescaped(text, "$", closing + 2)
            if closing >= 0:
                formula = text[position + 1 : closing]
                if formula and not formula[0].isspace() and not formula[-1].isspace():
                    rendered.append(f"<code>{escape(text[position : closing + 1], quote=False)}</code>")
                    position = closing + 1
                    continue

        if text.startswith("**", position):
            closing = _find_emphasis_close(text, position + 2, "**")
            if closing >= 0 and closing > position + 2:
                rendered.append(f"<b>{_render_inline(text[position + 2 : closing])}</b>")
                position = closing + 2
                continue

        if text[position] == "*":
            closing = _find_emphasis_close(text, position + 1, "*")
            if closing >= 0 and closing > position + 1:
                rendered.append(f"<i>{_render_inline(text[position + 1 : closing])}</i>")
                position = closing + 1
                continue

        rendered.append(escape(text[position], quote=False))
        position += 1
    return "".join(rendered)


def _normalise_language(info: str) -> str:
    language = info.split(maxsplit=1)[0] if info else ""
    return language if _LANGUAGE_RE.fullmatch(language) else ""


def _clean_text_block(block: str) -> tuple[str, bool]:
    agent_matches = list(AGENT_TAG_RE.finditer(block))
    cleaned, agent_tags = AGENT_TAG_RE.subn("", block)
    cleaned, horizontal_rules = _HORIZONTAL_RULE_RE.subn("", cleaned)
    changed = bool(agent_tags or horizontal_rules)
    if changed:
        if agent_matches and not block[: agent_matches[0].start()].strip():
            cleaned = cleaned.lstrip(" \t")
        if agent_matches and not block[agent_matches[-1].end() :].strip():
            cleaned = cleaned.rstrip(" \t")
        cleaned = _BLANK_LINE_WHITESPACE_RE.sub("", cleaned)
        cleaned = _EXCESS_BLANK_LINES_RE.sub("\n\n", cleaned)
    return cleaned, changed


def _clean_markdown(markdown: str) -> str:
    """Remove non-content placeholders only from outside fenced code blocks."""

    blocks = _parse_blocks(strip_googleusercontent_artifacts(markdown))
    cleaned: list[str] = []
    changed_text_blocks: list[bool] = []
    for block in blocks:
        if isinstance(block, _TextBlock):
            text, changed = _clean_text_block(block.raw)
            cleaned.append(text)
            changed_text_blocks.append(changed)
        else:
            cleaned.append(block.raw)
            changed_text_blocks.append(False)

    if cleaned and isinstance(blocks[0], _TextBlock) and changed_text_blocks[0]:
        cleaned[0] = _LEADING_BLANK_LINES_RE.sub("", cleaned[0])
    if cleaned and isinstance(blocks[-1], _TextBlock) and changed_text_blocks[-1]:
        cleaned[-1] = _TRAILING_BLANK_LINES_RE.sub("", cleaned[-1])
    return "".join(cleaned)


def _render_text_block(block: str) -> str:
    rendered: list[str] = []
    plain_lines: list[str] = []

    def flush_plain_lines() -> None:
        if plain_lines:
            rendered.append(_render_inline("".join(plain_lines)))
            plain_lines.clear()

    for line in block.splitlines(keepends=True):
        body, ending = _without_line_ending(line)
        list_item = _LIST_RE.fullmatch(body)
        heading = _HEADING_RE.fullmatch(body)
        if list_item is not None:
            flush_plain_lines()
            marker = list_item.group("marker")
            raw_indent = list_item.group("indent").replace("\t", "  ")
            depth = min(
                len(raw_indent) // LIST_INDENT_CHARACTERS_PER_LEVEL,
                len(UNORDERED_LIST_BULLETS) - 1,
            )
            if marker in {"-", "+", "*"}:
                marker = UNORDERED_LIST_BULLETS[depth]
            indent = LIST_INDENT_CHARACTER * (
                depth * LIST_INDENT_CHARACTERS_PER_LEVEL
            )
            rendered.append(f"{indent}{marker} {_render_inline(list_item.group('body'))}{ending}")
        elif body.startswith(">"):
            flush_plain_lines()
            quote = body[1:].lstrip(" ")
            rendered.append(f"<blockquote>{_render_inline(quote)}</blockquote>{ending}")
        elif heading is not None:
            flush_plain_lines()
            rendered.append(f"<b>{_render_inline(heading.group('body'))}</b>{ending}")
        else:
            plain_lines.append(body + ending)
    flush_plain_lines()
    return "".join(rendered)


def strip_googleusercontent_artifacts(text: str) -> str:
    """Remove Gemini image placeholders without touching inline underscore text."""

    without_urls = GOOGLEUSERCONTENT_ARTIFACT_RE.sub("", text)
    return ORPHAN_ARTIFACT_SUFFIX_RE.sub("", without_urls)


def markdown_to_telegram_html(markdown: str) -> str:
    """Convert a practical Markdown subset to Telegram-safe HTML.

    Unsupported Markdown and raw HTML are retained as escaped plain text.  A
    fenced block is always closed in the output, including when Gemini emits
    an unfinished fence.  LaTeX is wrapped in ``<code>`` rather than converted
    to Unicode so no mathematical meaning is silently discarded.
    """

    rendered: list[str] = []
    for block in _parse_blocks(_clean_markdown(markdown)):
        if isinstance(block, _TextBlock):
            rendered.append(_render_text_block(block.raw))
            continue
        language = _normalise_language(block.info)
        language_class = f' class="language-{escape(language, quote=True)}"' if language else ""
        rendered.append(
            f"<pre><code{language_class}>{escape(block.content, quote=False)}</code></pre>"
        )
    return "".join(rendered)


def _take_preferred(text: str, length: int) -> int:
    paragraph = text.rfind("\n\n", 0, length + 1)
    if paragraph >= 0:
        return paragraph + 2
    newline = text.rfind("\n", 0, length + 1)
    if newline >= 0:
        return newline + 1
    return length


def _split_code_block(block: _CodeBlock, limit: int) -> list[str]:
    language = _normalise_language(block.info)
    opener = f"{block.fence}{language}\n"
    # Reserve a possible newline, a closing fence, and its terminating newline.
    content_limit = limit - len(opener) - len(block.fence) - 2
    if content_limit < 1:
        raise ValueError("limit is too small for a fenced code block")

    pieces: list[str] = []
    remaining = block.content
    while remaining:
        take = _take_preferred(remaining, min(content_limit, len(remaining)))
        content, remaining = remaining[:take], remaining[take:]
        separator = "" if content.endswith(("\n", "\r")) else "\n"
        pieces.append(f"{opener}{content}{separator}{block.fence}\n")

    if not pieces:
        pieces.append(f"{opener}{block.fence}\n")
    return pieces


def split_message(markdown: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split Markdown without leaving a chunk inside a fenced code block.

    Paragraph boundaries are preferred over line boundaries, followed by a
    hard cut.  A code block that cannot fit as a whole is closed at the end of
    every chunk and reopened (with its language marker) in the next chunk.
    Returned chunks never exceed ``limit`` characters.
    """

    if limit < 32:
        raise ValueError("limit must leave room for Telegram HTML wrappers")
    if not markdown:
        return []

    chunks: list[str] = []
    current = ""
    for block in _parse_blocks(markdown):
        if isinstance(block, _CodeBlock):
            if len(current) + len(block.raw) <= limit:
                current += block.raw
                continue
            if current:
                chunks.append(current)
                current = ""
            if len(block.raw) <= limit:
                current = block.raw
                continue
            code_chunks = _split_code_block(block, limit)
            chunks.extend(code_chunks[:-1])
            current = code_chunks[-1]
            continue

        remaining = block.raw
        while remaining:
            capacity = limit - len(current)
            if len(remaining) <= capacity:
                current += remaining
                break
            if capacity == 0:
                chunks.append(current)
                current = ""
                continue
            take = _take_preferred(remaining, capacity)
            current += remaining[:take]
            remaining = remaining[take:]
            chunks.append(current)
            current = ""

    if current:
        chunks.append(current)
    return chunks


def render_markdown_chunks(markdown: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Render Markdown into independently sendable Telegram HTML chunks.

    Splitting is performed on Markdown first so fence locations are known.
    The raw split budget is reduced recursively when HTML escaping or tags make
    a rendered chunk longer than Telegram's safe limit.
    """

    cleaned = _clean_markdown(markdown)
    if not cleaned.strip():
        return []

    pending = split_message(cleaned, limit)
    rendered: list[str] = []
    while pending:
        raw = pending.pop(0)
        html = markdown_to_telegram_html(raw)
        if len(html) <= limit:
            rendered.append(html)
            continue

        ratio_budget = max(32, int(len(raw) * limit / len(html)) - 16)
        reduced_limit = min(len(raw) - 1, ratio_budget)
        if reduced_limit < 32:
            raise ValueError("limit is too small for the rendered HTML")
        smaller = split_message(raw, reduced_limit)
        if len(smaller) < 2:
            smaller = split_message(raw, max(32, len(raw) // 2))
        pending[0:0] = smaller
    return rendered


# Concise compatibility name for callers that already establish Telegram as
# their rendering context.
render_markdown = markdown_to_telegram_html


__all__ = [
    "AGENT_TAG_RE",
    "GOOGLEUSERCONTENT_ARTIFACT_RE",
    "LIST_INDENT_CHARACTER",
    "LIST_INDENT_CHARACTERS_PER_LEVEL",
    "MAX_MESSAGE_LENGTH",
    "ORPHAN_ARTIFACT_SUFFIX_RE",
    "UNORDERED_LIST_BULLETS",
    "markdown_to_telegram_html",
    "render_markdown",
    "render_markdown_chunks",
    "split_message",
    "strip_googleusercontent_artifacts",
]
