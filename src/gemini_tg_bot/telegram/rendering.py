"""Render Gemini Markdown for Telegram and split long source messages.

The renderer converts a deliberately small, lossless subset of LaTeX to
readable Unicode.  Every other ``$...$`` or ``$$...$$`` expression remains
intact in ``<code>`` so unsupported syntax is never only partly transformed.

Only the Python standard library is used.  Every function in this module is
pure: no Telegram objects, network access, or process-wide state are involved.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import re
import unicodedata
from urllib.parse import urlsplit

from gemini_tg_bot.i18n import DEFAULT_LANGUAGE, translate


MAX_MESSAGE_LENGTH = 4000
TABLE_TARGET_WIDTH = 40
TABLE_MIN_COLUMN_WIDTH = 8
UNORDERED_LIST_BULLETS = ("•", "◦", "▪")
LIST_INDENT_CHARACTER = "\u2007"
LIST_INDENT_CHARACTERS_PER_LEVEL = 2

GOOGLEUSERCONTENT_ARTIFACT_RE = re.compile(
    r"https?://googleusercontent\.com/(?:\w+/)+\d+(?:_\d+)*\n*"
)
ORPHAN_ARTIFACT_SUFFIX_RE = re.compile(r"(?m)^[ \t]*_\d+[ \t]*$\n?")
# Agent tags arrive self-closing (``<Tag/>``) and as pairs (``<Tag ...>`` with a
# matching ``</Tag>``).  Only the tags are matched, never the text between an
# opening and a closing tag, because upstream sometimes wraps real content in
# them.  Requiring an upper-case initial keeps ordinary HTML such as ``<b>`` and
# arithmetic comparisons such as ``a < b > c`` untouched.
AGENT_TAG_RE = re.compile(
    r"</[A-Z][A-Za-z0-9_]*\s*>"
    r'|<[A-Z][A-Za-z0-9_]*(?:\s+[A-Za-z_][\w.-]*\s*=\s*"[^"]*")*\s*/?>'
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
_TABLE_SEPARATOR_CELL_RE = re.compile(r":?[ \t]*-+[ \t]*:?")
_SAFE_LINK_SCHEMES = frozenset({"http", "https", "mailto", "tg"})
_TABLE_LINE_START_FORBIDDEN = frozenset(
    "。，、；：？！）」』】》〉・ー％‧.,;:?!)]}"
)
_TABLE_LINE_END_FORBIDDEN = frozenset("（「『【《〈([{")
_LATEX_MACROS = {
    "approx": "≈",
    "cdot": "·",
    "cos": "cos",
    "ge": "≥",
    "infty": "∞",
    "lceil": "⌈",
    "le": "≤",
    "lfloor": "⌊",
    "ln": "ln",
    "log": "log",
    "max": "max",
    "min": "min",
    "ne": "≠",
    "pm": "±",
    "rceil": "⌉",
    "rfloor": "⌋",
    "sin": "sin",
    "tan": "tan",
    "times": "×",
    "to": "→",
    # Degrees, ellipses, and primes appear constantly in trigonometry answers.
    "cdots": "⋯",
    "circ": "°",
    "degree": "°",
    "ldots": "…",
    "prime": "′",
    # Greek letters with an exact Unicode counterpart.  Anything without one
    # (``\varepsilon`` and friends) stays outside the safe set on purpose.
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "zeta": "ζ",
    "eta": "η",
    "theta": "θ",
    "iota": "ι",
    "kappa": "κ",
    "lambda": "λ",
    "mu": "μ",
    "nu": "ν",
    "xi": "ξ",
    "pi": "π",
    "rho": "ρ",
    "sigma": "σ",
    "tau": "τ",
    "upsilon": "υ",
    "phi": "φ",
    "chi": "χ",
    "psi": "ψ",
    "omega": "ω",
    "Gamma": "Γ",
    "Delta": "Δ",
    "Theta": "Θ",
    "Lambda": "Λ",
    "Xi": "Ξ",
    "Pi": "Π",
    "Sigma": "Σ",
    "Upsilon": "Υ",
    "Phi": "Φ",
    "Psi": "Ψ",
    "Omega": "Ω",
}
# Glyphs that already sit on the superscript line, so ``90^\circ`` needs the
# glyph itself rather than a lookup in _SUPERSCRIPTS.
_LATEX_RAISED_MACROS = {
    "circ": "°",
    "degree": "°",
    "prime": "′",
}
_LATEX_FRACTION_OPERATORS = frozenset("+-*/=<>±×·≈≤≥≠→")
_SUPERSCRIPTS = {
    "0": "⁰",
    "1": "¹",
    "2": "²",
    "3": "³",
    "4": "⁴",
    "5": "⁵",
    "6": "⁶",
    "7": "⁷",
    "8": "⁸",
    "9": "⁹",
    "+": "⁺",
    "-": "⁻",
    "=": "⁼",
    "(": "⁽",
    ")": "⁾",
    "a": "ᵃ",
    "b": "ᵇ",
    "c": "ᶜ",
    "d": "ᵈ",
    "e": "ᵉ",
    "f": "ᶠ",
    "g": "ᵍ",
    "h": "ʰ",
    "i": "ⁱ",
    "j": "ʲ",
    "k": "ᵏ",
    "l": "ˡ",
    "m": "ᵐ",
    "n": "ⁿ",
    "o": "ᵒ",
    "p": "ᵖ",
    "r": "ʳ",
    "s": "ˢ",
    "t": "ᵗ",
    "u": "ᵘ",
    "v": "ᵛ",
    "w": "ʷ",
    "x": "ˣ",
    "y": "ʸ",
    "z": "ᶻ",
    "A": "ᴬ",
    "B": "ᴮ",
    "D": "ᴰ",
    "E": "ᴱ",
    "G": "ᴳ",
    "H": "ᴴ",
    "I": "ᴵ",
    "J": "ᴶ",
    "K": "ᴷ",
    "L": "ᴸ",
    "M": "ᴹ",
    "N": "ᴺ",
    "O": "ᴼ",
    "P": "ᴾ",
    "R": "ᴿ",
    "T": "ᵀ",
    "U": "ᵁ",
    "V": "ⱽ",
    "W": "ᵂ",
}
_SUBSCRIPTS = {
    "0": "₀",
    "1": "₁",
    "2": "₂",
    "3": "₃",
    "4": "₄",
    "5": "₅",
    "6": "₆",
    "7": "₇",
    "8": "₈",
    "9": "₉",
    "+": "₊",
    "-": "₋",
    "=": "₌",
    "(": "₍",
    ")": "₎",
    "a": "ₐ",
    "e": "ₑ",
    "h": "ₕ",
    "i": "ᵢ",
    "j": "ⱼ",
    "k": "ₖ",
    "l": "ₗ",
    "m": "ₘ",
    "n": "ₙ",
    "o": "ₒ",
    "p": "ₚ",
    "r": "ᵣ",
    "s": "ₛ",
    "t": "ₜ",
    "x": "ₓ",
}
_LATEX_PLAIN_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 "
    "\t\r\n()[]+-*/,.=<>"
)


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


@dataclass(frozen=True, slots=True)
class _TableBlock:
    raw: str
    rows: tuple[tuple[str, ...], ...]


def display_width(text: str) -> int:
    """Return the number of monospace cells occupied by ``text``.

    Telegram's monospace font uses two cells for East Asian wide and fullwidth
    characters.  Counting code points with ``len`` would therefore misalign
    tables containing Chinese, Japanese, or Korean text.
    """

    return sum(
        2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        for character in text
    )


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


def _split_table_row(line: str) -> tuple[str, ...] | None:
    body, _ = _without_line_ending(line)
    body = body.strip()
    if "|" not in body:
        return None

    cells: list[str] = []
    current: list[str] = []
    found_separator = False
    in_code = False
    position = 0
    while position < len(body):
        character = body[position]
        if character == "\\" and position + 1 < len(body):
            current.extend(body[position : position + 2])
            position += 2
            continue
        if character == "`":
            in_code = not in_code
            current.append(character)
        elif character == "|" and not in_code:
            cells.append("".join(current).strip())
            current.clear()
            found_separator = True
        else:
            current.append(character)
        position += 1
    cells.append("".join(current).strip())

    if body.startswith("|"):
        cells.pop(0)
    if body.endswith("|") and not _is_escaped(body, len(body) - 1):
        cells.pop()
    if not found_separator or len(cells) < 2:
        return None
    return tuple(cells)


def _is_table_separator(cells: tuple[str, ...], columns: int) -> bool:
    return len(cells) == columns and all(
        _TABLE_SEPARATOR_CELL_RE.fullmatch(cell) is not None for cell in cells
    )


def _split_text_tables(block: str) -> list[_TextBlock | _TableBlock]:
    lines = block.splitlines(keepends=True)
    blocks: list[_TextBlock | _TableBlock] = []
    plain: list[str] = []
    index = 0

    while index < len(lines):
        header = _split_table_row(lines[index])
        separator = (
            _split_table_row(lines[index + 1]) if index + 1 < len(lines) else None
        )
        if (
            header is None
            or separator is None
            or not _is_table_separator(separator, len(header))
        ):
            plain.append(lines[index])
            index += 1
            continue

        if plain:
            blocks.append(_TextBlock("".join(plain)))
            plain.clear()

        table_lines = [lines[index], lines[index + 1]]
        rows = [header]
        index += 2
        while index < len(lines):
            row = _split_table_row(lines[index])
            if row is None or len(row) != len(header):
                break
            table_lines.append(lines[index])
            rows.append(row)
            index += 1
        blocks.append(_TableBlock("".join(table_lines), tuple(rows)))

    if plain:
        blocks.append(_TextBlock("".join(plain)))
    return blocks


def _parse_document_blocks(
    markdown: str,
) -> list[_TextBlock | _CodeBlock | _TableBlock]:
    blocks: list[_TextBlock | _CodeBlock | _TableBlock] = []
    for block in _parse_blocks(markdown):
        if isinstance(block, _TextBlock):
            blocks.extend(_split_text_tables(block.raw))
        else:
            blocks.append(block)
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


def _latex_span(text: str, position: int) -> tuple[str, int] | None:
    """Return one complete math expression and its end, without guessing currency."""

    if text.startswith("$$", position):
        closing = _find_unescaped(text, "$$", position + 2)
        if closing >= 0 and text[position + 2 : closing].strip():
            return text[position + 2 : closing], closing + 2
        return None

    if text[position] != "$":
        return None
    closing = _find_unescaped(text, "$", position + 1)
    while closing >= 0 and closing + 1 < len(text) and text[closing + 1] == "$":
        closing = _find_unescaped(text, "$", closing + 2)
    if closing < 0:
        return None
    formula = text[position + 1 : closing]
    if not formula or formula[0].isspace() or formula[-1].isspace():
        return None
    return formula, closing + 1


def _matching_brace(formula: str, start: int) -> int:
    """Return the index of the ``}`` closing the group opened before ``start``."""

    depth = 1
    position = start
    while position < len(formula):
        character = formula[position]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return position
        position += 1
    return -1


def _latex_group(formula: str, position: int) -> tuple[str, int] | None:
    """Return the contents of the brace group at ``position`` and the index after it."""

    if position >= len(formula) or formula[position] != "{":
        return None
    end = _matching_brace(formula, position + 1)
    if end < 0:
        return None
    return formula[position + 1 : end], end + 1


def _convert_fraction(formula: str, position: int) -> tuple[str, int] | None:
    """Convert ``\frac{X}{Y}`` to ``X/Y`` when both sides are themselves safe.

    A nested fraction has no unambiguous single-line form, so it stays outside
    the safe set and rejects the whole expression.  A side carrying an operator
    or a space gains parentheses because ``a+b/c`` would otherwise read as a
    different expression than the source.
    """

    sides: list[str] = []
    for _ in range(2):
        group = _latex_group(formula, position)
        if group is None:
            return None
        raw, position = group
        if "\\frac" in raw:
            return None
        side = _convert_simple_latex(raw)
        if not side:
            return None
        sides.append(side)

    numerator, denominator = (
        f"({side})"
        if any(
            character.isspace() or character in _LATEX_FRACTION_OPERATORS
            for character in side
        )
        else side
        for side in sides
    )
    return f"{numerator}/{denominator}", position


def _convert_simple_latex(formula: str) -> str | None:
    """Convert a safe expression, or reject the whole expression on any unknown syntax."""

    converted: list[str] = []
    position = 0
    while position < len(formula):
        character = formula[position]
        if character == "\\":
            match = re.match(r"\\([A-Za-z]+)", formula[position:])
            if match is None:
                return None
            name = match.group(1)
            after_macro = position + len(match.group(0))
            if name == "text":
                # ``\text`` carries prose, including CJK, that the plain
                # character set deliberately excludes.  Only its braces are
                # dropped, and only when it holds no further markup.
                group = _latex_group(formula, after_macro)
                if group is None or any(brace in group[0] for brace in "\\{}"):
                    return None
                converted.append(group[0])
                position = group[1]
                continue
            if name == "frac":
                fraction = _convert_fraction(formula, after_macro)
                if fraction is None:
                    return None
                converted.append(fraction[0])
                position = fraction[1]
                continue
            if name not in _LATEX_MACROS:
                return None
            converted.append(_LATEX_MACROS[name])
            position = after_macro
            continue
        if character in {"^", "_"}:
            if character == "^":
                raised = re.match(r"\\([A-Za-z]+)", formula[position + 1 :])
                if raised is not None and raised.group(1) in _LATEX_RAISED_MACROS:
                    converted.append(_LATEX_RAISED_MACROS[raised.group(1)])
                    position += 1 + len(raised.group(0))
                    continue
            replacements = _SUPERSCRIPTS if character == "^" else _SUBSCRIPTS
            if position + 1 >= len(formula) or formula[position + 1] not in replacements:
                return None
            converted.append(replacements[formula[position + 1]])
            position += 2
            continue
        if character not in _LATEX_PLAIN_CHARACTERS:
            return None
        converted.append(character)
        position += 1
    return "".join(converted)


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

        if text[position] == "$":
            span = _latex_span(text, position)
            if span is not None:
                formula, end = span
                converted = _convert_simple_latex(formula)
                if converted is None:
                    rendered.append(f"<code>{escape(text[position:end], quote=False)}</code>")
                else:
                    rendered.append(escape(converted, quote=False))
                position = end
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


def _table_column_widths(rows: tuple[tuple[str, ...], ...]) -> tuple[int, ...]:
    return tuple(
        max(display_width(row[column]) for row in rows)
        for column in range(len(rows[0]))
    )


def _normalise_table_cell(cell: str) -> str:
    """Apply safe math conversion without inspecting inline code span contents."""

    normalised: list[str] = []
    position = 0
    while position < len(cell):
        if cell[position] == "\\" and position + 1 < len(cell):
            normalised.append(cell[position : position + 2])
            position += 2
            continue
        if cell[position] == "`":
            closing = _find_unescaped(cell, "`", position + 1)
            if closing >= 0:
                normalised.append(cell[position : closing + 1])
                position = closing + 1
                continue
        if cell[position] == "$":
            span = _latex_span(cell, position)
            if span is not None:
                formula, end = span
                converted = _convert_simple_latex(formula)
                normalised.append(cell[position:end] if converted is None else converted)
                position = end
                continue
        normalised.append(cell[position])
        position += 1
    return "".join(normalised)


def _strip_table_cell_markdown(cell: str) -> str:
    """Remove inline presentation syntax without producing HTML inside ``pre``."""

    plain: list[str] = []
    position = 0
    while position < len(cell):
        if cell[position] == "\\" and position + 1 < len(cell):
            plain.append(cell[position : position + 2])
            position += 2
            continue

        if cell[position] == "[":
            label_end = _matching_bracket(cell, position + 1, "[", "]")
            if (
                label_end >= 0
                and label_end + 1 < len(cell)
                and cell[label_end + 1] == "("
            ):
                target_end = _matching_bracket(cell, label_end + 2, "(", ")")
                if target_end >= 0:
                    label = _strip_table_cell_markdown(
                        cell[position + 1 : label_end]
                    )
                    url = _link_destination(cell[label_end + 2 : target_end])
                    plain.append(f"{label}（{url}）" if url else label)
                    position = target_end + 1
                    continue

        if cell[position] == "`":
            closing = _find_unescaped(cell, "`", position + 1)
            if closing >= 0:
                plain.append(cell[position + 1 : closing])
                position = closing + 1
                continue

        if cell.startswith("~~", position):
            closing = _find_unescaped(cell, "~~", position + 2)
            if closing > position + 2:
                plain.append(
                    _strip_table_cell_markdown(cell[position + 2 : closing])
                )
                position = closing + 2
                continue

        if cell.startswith("**", position):
            closing = _find_emphasis_close(cell, position + 2, "**")
            if closing > position + 2:
                plain.append(
                    _strip_table_cell_markdown(cell[position + 2 : closing])
                )
                position = closing + 2
                continue

        if cell[position] == "*":
            closing = _find_emphasis_close(cell, position + 1, "*")
            if closing > position + 1:
                plain.append(
                    _strip_table_cell_markdown(cell[position + 1 : closing])
                )
                position = closing + 1
                continue

        plain.append(cell[position])
        position += 1
    return "".join(plain)


def _fit_table_column_widths(
    natural_widths: tuple[int, ...],
) -> tuple[int, ...] | None:
    gaps_width = 2 * (len(natural_widths) - 1)
    if sum(natural_widths) + gaps_width <= TABLE_TARGET_WIDTH:
        return natural_widths

    available = TABLE_TARGET_WIDTH - gaps_width
    if available < len(natural_widths) * TABLE_MIN_COLUMN_WIDTH:
        # Only tables with so many columns that the target cannot give every
        # column its minimum readable width use the legacy list fallback.
        return None

    allocated: list[int | None] = [None] * len(natural_widths)
    free = list(range(len(natural_widths)))
    remaining = available
    while True:
        total_weight = sum(natural_widths[index] for index in free)
        constrained = [
            index
            for index in free
            if natural_widths[index] * remaining
            < TABLE_MIN_COLUMN_WIDTH * total_weight
        ]
        if not constrained:
            break
        for index in constrained:
            allocated[index] = TABLE_MIN_COLUMN_WIDTH
            remaining -= TABLE_MIN_COLUMN_WIDTH
            free.remove(index)

    total_weight = sum(natural_widths[index] for index in free)
    for index in free:
        allocated[index] = remaining * natural_widths[index] // total_weight
    leftover = available - sum(width for width in allocated if width is not None)
    by_remainder = sorted(
        free,
        key=lambda index: (remaining * natural_widths[index] % total_weight, -index),
        reverse=True,
    )
    for index in by_remainder[:leftover]:
        assert allocated[index] is not None
        allocated[index] += 1

    return tuple(width for width in allocated if width is not None)


def _wrap_table_cell(cell: str, width: int) -> tuple[str, ...]:
    """Wrap by display cells, preferring word boundaries when one is available."""

    if not cell:
        return ("",)

    lines: list[str] = []
    remaining = cell
    while display_width(remaining) > width:
        used = 0
        cut = 0
        for cut, character in enumerate(remaining, start=1):
            character_width = display_width(character)
            if used + character_width > width:
                cut -= 1
                break
            used += character_width
        if cut <= 0:
            cut = 1

        whitespace = next(
            (
                index
                for index in range(cut - 1, 0, -1)
                if remaining[index].isspace()
            ),
            None,
        )
        cut = whitespace if whitespace is not None else cut

        before = cut - 1
        while before >= 0 and remaining[before].isspace():
            before -= 1
        after = cut
        while after < len(remaining) and remaining[after].isspace():
            after += 1
        violates_punctuation_rule = (
            before >= 0 and remaining[before] in _TABLE_LINE_END_FORBIDDEN
        ) or (
            after < len(remaining)
            and remaining[after] in _TABLE_LINE_START_FORBIDDEN
        )
        if violates_punctuation_rule and remaining[: cut - 1].rstrip():
            cut -= 1

        lines.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        lines.append(remaining)
    return tuple(lines)


def _render_pre_table(
    rows: tuple[tuple[str, ...], ...],
    widths: tuple[int, ...],
) -> str:
    rendered_rows: list[str] = []
    for index, row in enumerate(rows):
        wrapped = [
            _wrap_table_cell(cell, width)
            for cell, width in zip(row, widths, strict=True)
        ]
        for line_index in range(max(len(lines) for lines in wrapped)):
            cells = [
                lines[line_index] if line_index < len(lines) else ""
                for lines in wrapped
            ]
            padded = [
                cell + " " * (width - display_width(cell))
                for cell, width in zip(cells, widths, strict=True)
            ]
            rendered_rows.append("  ".join(padded).rstrip())
        if index == 0:
            rendered_rows.append("  ".join("─" * width for width in widths))
    content = "\n".join(rendered_rows)
    return f"<pre>{escape(content, quote=False)}</pre>"


def _render_list_table(rows: tuple[tuple[str, ...], ...]) -> str:
    header, *data_rows = rows
    if not data_rows:
        return _render_inline(" | ".join(header))

    indent = LIST_INDENT_CHARACTER * LIST_INDENT_CHARACTERS_PER_LEVEL
    marker = UNORDERED_LIST_BULLETS[1]
    rendered_rows: list[str] = []
    for row in data_rows:
        lines = [_render_inline(row[0])]
        lines.extend(
            f"{indent}{marker} {_render_inline(label)}：{_render_inline(value)}"
            for label, value in zip(header[1:], row[1:], strict=True)
        )
        rendered_rows.append("\n".join(lines))
    return "\n\n".join(rendered_rows)


def _render_table_block(block: _TableBlock) -> str:
    rows = tuple(
        tuple(
            _strip_table_cell_markdown(_normalise_table_cell(cell))
            for cell in row
        )
        for row in block.rows
    )
    widths = _fit_table_column_widths(_table_column_widths(rows))
    rendered = (
        _render_pre_table(rows, widths)
        if widths is not None
        else _render_list_table(block.rows)
    )
    return rendered + ("\n" if block.raw.endswith(("\n", "\r")) else "")


def strip_googleusercontent_artifacts(text: str) -> str:
    """Remove Gemini image placeholders without touching inline underscore text."""

    without_urls = GOOGLEUSERCONTENT_ARTIFACT_RE.sub("", text)
    return ORPHAN_ARTIFACT_SUFFIX_RE.sub("", without_urls)


def markdown_to_telegram_html(markdown: str) -> str:
    """Convert a practical Markdown subset to Telegram-safe HTML.

    Unsupported Markdown and raw HTML are retained as escaped plain text.  A
    fenced block is always closed in the output, including when Gemini emits
    an unfinished fence.  Simple LaTeX is converted only when the complete
    expression belongs to a lossless allowlist; all other math stays copyable
    as its original source in ``<code>``.
    """

    rendered: list[str] = []
    for block in _parse_document_blocks(_clean_markdown(markdown)):
        if isinstance(block, _TextBlock):
            rendered.append(_render_text_block(block.raw))
            continue
        if isinstance(block, _TableBlock):
            rendered.append(_render_table_block(block))
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
    """Split Markdown without leaving a chunk inside a protected block.

    Paragraph boundaries are preferred over line boundaries, followed by a
    hard cut.  A code block that cannot fit as a whole is closed at the end of
    every chunk and reopened (with its language marker) in the next chunk.  A
    Markdown table is kept whole so no chunk can be mistaken for plain pipe-
    separated text.  Returned chunks never exceed ``limit`` characters.
    """

    if limit < 32:
        raise ValueError("limit must leave room for Telegram HTML wrappers")
    if not markdown:
        return []

    chunks: list[str] = []
    current = ""
    for block in _parse_document_blocks(markdown):
        if isinstance(block, _TableBlock):
            if len(block.raw) > limit:
                raise ValueError("limit is too small for a Markdown table")
            if current and len(current) + len(block.raw) > limit:
                chunks.append(current)
                current = ""
            current += block.raw
            continue

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
    "TABLE_MIN_COLUMN_WIDTH",
    "TABLE_TARGET_WIDTH",
    "UNORDERED_LIST_BULLETS",
    "display_width",
    "markdown_to_telegram_html",
    "render_markdown",
    "render_markdown_chunks",
    "split_message",
    "strip_googleusercontent_artifacts",
]


THOUGHTS_TRUNCATION_NOTE = translate("thoughts.truncated", DEFAULT_LANGUAGE)
THOUGHTS_TITLE = translate("thoughts.title", DEFAULT_LANGUAGE)


def _render_thoughts_inline(text: str) -> str:
    rendered: list[str] = []
    for line in text.splitlines(keepends=True):
        body, ending = _without_line_ending(line)
        heading = _HEADING_RE.fullmatch(body)
        if heading is not None:
            rendered.append(f"<b>{_render_inline(heading.group('body'))}</b>{ending}")
        elif _opening_fence(line) is not None:
            rendered.append(escape(body, quote=False) + ending)
        else:
            rendered.append(_render_inline(body) + ending)
    return "".join(rendered)


def render_thoughts_blockquote(
    thoughts: str,
    *,
    budget: int,
    seconds: float | None = None,
    language: str = DEFAULT_LANGUAGE,
) -> str:
    """Wrap reasoning in a collapsed blockquote that fits within ``budget``.

    Telegram renders ``<blockquote expandable>`` collapsed until tapped, which
    suits reasoning that is usually longer than the answer it precedes.  The
    result shares one message with the answer so the two cannot be separated by
    other traffic, which is why the caller has to pass a character budget: the
    reasoning is supplementary, so it is truncated rather than allowed to push
    the answer into another message.  When ``seconds`` is supplied, the first
    line identifies the elapsed thinking time even while the quote is collapsed.
    """

    text = thoughts.strip()
    if not text or budget <= 0:
        return ""

    opening, closing = "<blockquote expandable>", "</blockquote>"
    title = (
        translate(
            (
                "thoughts.title_elapsed.one"
                if int(max(0.0, seconds)) == 1
                else "thoughts.title_elapsed.many"
            ),
            language,
            seconds=int(max(0.0, seconds)),
        )
        if seconds is not None
        else ""
    )
    overhead = len(opening) + len(title) + len(closing)
    if budget <= overhead:
        return ""

    body = _render_thoughts_inline(text)
    if len(body) + overhead <= budget:
        return f"{opening}{title}{body}{closing}"

    note = escape(translate("thoughts.truncated", language), quote=False)
    room = budget - overhead - len(note)
    if room <= 0:
        return ""
    # Inline tags and escaping expand the source, so trim the source and render
    # it again.  Cutting rendered HTML directly could leave a tag or entity
    # incomplete and make Telegram reject the entire message.
    trimmed = text[:room]
    rendered_trimmed = _render_thoughts_inline(trimmed)
    while trimmed and len(rendered_trimmed) > room:
        trimmed = trimmed[:-1]
        rendered_trimmed = _render_thoughts_inline(trimmed)
    if not trimmed:
        return ""
    return f"{opening}{title}{rendered_trimmed}{note}{closing}"
