import json
from html import unescape
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

import pytest

from gemini_tg_bot.i18n import (
    LANGUAGE_CHINESE,
    LANGUAGE_ENGLISH,
    translate,
)


# The task's fixed DoD spells the coverage source as this slash-separated
# module key (without the file's .py suffix).  Loading the target under that
# exact key lets pytest-cov measure the intended file without changing the DoD.
_COVERAGE_KEY = "src/gemini_tg_bot/telegram/rendering"
_RENDERING_PATH = (
    Path(__file__).parents[1] / "src" / "gemini_tg_bot" / "telegram" / "rendering.py"
)
_SPEC = spec_from_file_location(_COVERAGE_KEY, _RENDERING_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_RENDERING = module_from_spec(_SPEC)
sys.modules[_COVERAGE_KEY] = _RENDERING
_SPEC.loader.exec_module(_RENDERING)

MAX_MESSAGE_LENGTH = _RENDERING.MAX_MESSAGE_LENGTH
LIST_INDENT_CHARACTER = _RENDERING.LIST_INDENT_CHARACTER
TABLE_MIN_COLUMN_WIDTH = _RENDERING.TABLE_MIN_COLUMN_WIDTH
TABLE_TARGET_WIDTH = _RENDERING.TABLE_TARGET_WIDTH
display_width = _RENDERING.display_width
markdown_to_telegram_html = _RENDERING.markdown_to_telegram_html
render_markdown = _RENDERING.render_markdown
render_markdown_chunks = _RENDERING.render_markdown_chunks
split_message = _RENDERING.split_message
strip_googleusercontent_artifacts = _RENDERING.strip_googleusercontent_artifacts


def test_nested_inline_formatting_code_and_escapes() -> None:
    source = r"**bold and *italic***, *italic with **bold***, `x < y` and \*literal\*"

    assert markdown_to_telegram_html(source) == (
        "<b>bold and <i>italic</i></b>, "
        "<i>italic with <b>bold</b></i>, "
        "<code>x &lt; y</code> and *literal*"
    )
    assert render_markdown(source) == markdown_to_telegram_html(source)


def test_links_images_and_unsupported_html_degrade_safely() -> None:
    source = (
        "[**Docs**](https://example.test/a?q=1&x=2) "
        "![plot](https://example.test/p.png) "
        "[unsafe](javascript:alert(1)) <table><tr><td>x</td></tr></table>"
    )

    rendered = markdown_to_telegram_html(source)

    assert '<a href="https://example.test/a?q=1&amp;x=2"><b>Docs</b></a>' in rendered
    assert '<a href="https://example.test/p.png">plot</a>' in rendered
    assert "unsafe (javascript:alert(1))" in rendered
    assert "&lt;table&gt;&lt;tr&gt;&lt;td&gt;x&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;" in rendered
    assert "<table>" not in rendered


def test_links_with_titles_angle_destinations_and_malformed_markup() -> None:
    source = (
        '[title](https://example.test "ignored") '
        "[angle](<tg://user?id=42>) [missing]( "
        "[no-target] and ![broken](javascript:bad)"
    )

    rendered = markdown_to_telegram_html(source)

    assert '<a href="https://example.test">title</a>' in rendered
    assert '<a href="tg://user?id=42">angle</a>' in rendered
    assert "[missing](" in rendered
    assert "[no-target]" in rendered
    assert "broken (javascript:bad)" in rendered


def test_lists_headings_and_blockquotes_use_supported_telegram_html() -> None:
    source = "# Heading\n- **one**\n  * two\n1. three\n> quoted *text*\n"

    assert markdown_to_telegram_html(source) == (
        "<b>Heading</b>\n"
        "• <b>one</b>\n"
        f"{LIST_INDENT_CHARACTER * 2}◦ two\n"
        "1. three\n"
        "<blockquote>quoted <i>text</i></blockquote>\n"
    )


def test_unordered_lists_use_three_visual_levels_and_cap_deeper_items() -> None:
    source = (
        "- first\n"
        "  - second\n"
        "    - third\n"
        "      - fourth\n"
    )

    rendered = markdown_to_telegram_html(source)

    assert rendered == (
        "• first\n"
        f"{LIST_INDENT_CHARACTER * 2}◦ second\n"
        f"{LIST_INDENT_CHARACTER * 4}▪ third\n"
        f"{LIST_INDENT_CHARACTER * 4}▪ fourth\n"
    )
    assert "".join(render_markdown_chunks(source, limit=32)) == rendered


def test_list_marker_and_bold_html_are_rendered_together() -> None:
    assert markdown_to_telegram_html("- **important** item\n") == (
        "• <b>important</b> item\n"
    )


def test_list_adjacent_to_blockquote_keeps_both_structures() -> None:
    source = "- before\n> quoted **text**\n  - after\n"

    assert markdown_to_telegram_html(source) == (
        "• before\n"
        "<blockquote>quoted <b>text</b></blockquote>\n"
        f"{LIST_INDENT_CHARACTER * 2}◦ after\n"
    )


def test_fenced_code_preserves_language_and_escapes_content() -> None:
    source = "before\n```python\nprint('<tag> & value')\n```\nafter"

    assert markdown_to_telegram_html(source) == (
        "before\n"
        '<pre><code class="language-python">'
        "print('&lt;tag&gt; &amp; value')\n"
        "</code></pre>after"
    )


def test_invalid_language_marker_is_omitted() -> None:
    rendered = markdown_to_telegram_html("```<bad>\nx\n```\n")

    assert rendered == "<pre><code>x\n</code></pre>"


def test_unclosed_code_fence_is_closed_in_html() -> None:
    rendered = markdown_to_telegram_html("text\n```js\nconst x = '<x>';\n")

    assert rendered == (
        "text\n"
        '<pre><code class="language-js">const x = \'&lt;x&gt;\';\n</code></pre>'
    )


def test_latex_superscript_example_becomes_readable_text() -> None:
    """A ubiquitous complexity exponent should read naturally without code styling."""

    assert markdown_to_telegram_html(r"$O(N^2)$") == "O(N²)"


def test_latex_function_name_example_becomes_readable_text() -> None:
    """Removing a safe function-name backslash eliminates common visual noise."""

    assert markdown_to_telegram_html(r"$O(N \log N)$") == "O(N log N)"


def test_latex_operator_example_becomes_readable_text() -> None:
    """A lossless Unicode operator is clearer than Telegram-displayed LaTeX source."""

    assert markdown_to_telegram_html(r"$a \le b$") == "a ≤ b"


def test_latex_brackets_and_subscript_example_becomes_readable_text() -> None:
    """Combining only allowlisted pieces must still produce one coherent expression."""

    assert markdown_to_telegram_html(r"$\lceil \log_2 n \rceil$") == "⌈ log₂ n ⌉"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (r"$a \le b$", "a ≤ b"),
        (r"$a \ge b$", "a ≥ b"),
        (r"$a \ne b$", "a ≠ b"),
        (r"$a \times b$", "a × b"),
        (r"$a \cdot b$", "a · b"),
        (r"$a \approx b$", "a ≈ b"),
        (r"$n \to \infty$", "n → ∞"),
        (r"$a \pm b$", "a ± b"),
        (r"$\lfloor x \rfloor$", "⌊ x ⌋"),
        (r"$\lceil x \rceil$", "⌈ x ⌉"),
        (r"$\log x + \ln x$", "log x + ln x"),
        (r"$\max x + \min x$", "max x + min x"),
        (r"$\sin x + \cos x + \tan x$", "sin x + cos x + tan x"),
    ],
)
def test_every_allowlisted_latex_macro_has_a_lossless_conversion(
    source: str,
    expected: str,
) -> None:
    """An explicit allowlist needs coverage so later edits cannot silently widen or shrink it."""

    assert markdown_to_telegram_html(source) == expected


def test_latex_scripts_require_one_unicode_supported_character() -> None:
    """Unsupported or grouped scripts must reject the whole formula instead of approximating."""

    assert markdown_to_telegram_html(r"$x^3 + y^n + z_n$") == "x³ + yⁿ + zₙ"
    assert markdown_to_telegram_html(r"$x^q$") == r"<code>$x^q$</code>"
    assert markdown_to_telegram_html(r"$x^{12}$") == r"<code>$x^{12}$</code>"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (r"$\theta$", "\u03b8"),
        (r"$\sin\theta$", "sin\u03b8"),
        (r"$90^\circ$", "90\u00b0"),
        (r"$2\pi$", "2\u03c0"),
        (r"$\frac{\text{對邊}}{\text{斜邊}}$", "對邊/斜邊"),
        (
            r"$\sin^2\theta + \cos^2\theta = 1$",
            "sin\u00b2\u03b8 + cos\u00b2\u03b8 = 1",
        ),
    ],
)
def test_production_trigonometry_expressions_convert_to_unicode(
    source: str,
    expected: str,
) -> None:
    """These exact expressions reached real users as raw LaTeX because the safe set was too narrow."""

    assert markdown_to_telegram_html(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (r"$\alpha \beta \gamma \delta \epsilon$", "\u03b1 \u03b2 \u03b3 \u03b4 \u03b5"),
        (r"$\zeta \eta \iota \kappa \lambda$", "\u03b6 \u03b7 \u03b9 \u03ba \u03bb"),
        (r"$\mu \nu \xi \rho \sigma$", "\u03bc \u03bd \u03be \u03c1 \u03c3"),
        (r"$\tau \upsilon \phi \chi \psi \omega$", "\u03c4 \u03c5 \u03c6 \u03c7 \u03c8 \u03c9"),
        (r"$\Gamma \Delta \Theta \Lambda \Xi$", "\u0393 \u0394 \u0398 \u039b \u039e"),
        (r"$\Pi \Sigma \Upsilon \Phi \Psi \Omega$", "\u03a0 \u03a3 \u03a5 \u03a6 \u03a8 \u03a9"),
        (r"$\degree$", "\u00b0"),
        (r"$x\prime$", "x\u2032"),
        (r"$1, 2, \cdots, n$", "1, 2, \u22ef, n"),
        (r"$1, 2, \ldots, n$", "1, 2, \u2026, n"),
        (r"$\text{speed}$", "speed"),
    ],
)
def test_every_newly_allowlisted_latex_macro_has_a_lossless_conversion(
    source: str,
    expected: str,
) -> None:
    """Widening the safe set only helps if each addition stays pinned to one exact glyph."""

    assert markdown_to_telegram_html(source) == expected


def test_latex_fraction_becomes_a_single_line_quotient() -> None:
    """A slash reads as the same quantity, so keeping simple fractions as code was pure noise."""

    assert markdown_to_telegram_html(r"$\frac{n(n-1)}{2}$") == "(n(n-1))/2"
    assert markdown_to_telegram_html(r"$\frac{x}{y}$") == "x/y"


def test_latex_fraction_parenthesises_any_side_holding_an_operator() -> None:
    """Without the parentheses ``\frac{a+b}{c}`` would flatten into the different value ``a+b/c``."""

    assert markdown_to_telegram_html(r"$\frac{a+b}{c}$") == "(a+b)/c"
    assert markdown_to_telegram_html(r"$\frac{a}{b - c}$") == "a/(b - c)"


def test_nested_latex_fraction_remains_original_code() -> None:
    """Stacked fractions have no unambiguous one-line form, so the source must stay copyable."""

    source = r"$\frac{1}{\frac{a}{b}}$"

    assert markdown_to_telegram_html(source) == rf"<code>{source}</code>"


def test_latex_text_macro_rejects_further_markup() -> None:
    """``\text`` may carry prose only; nested groups would need rules the safe set does not have."""

    source = r"$\text{a \theta b}$"

    assert markdown_to_telegram_html(source) == rf"<code>{source}</code>"


def test_latex_sum_example_remains_original_code() -> None:
    """Unsupported large operators and grouped scripts must stay exact and copyable."""

    source = r"$\sum_{i=1}^{n} i$"

    assert markdown_to_telegram_html(source) == rf"<code>{source}</code>"


def test_latex_conversion_is_all_or_nothing() -> None:
    """One unsafe command must prevent safe neighbors from being partially rewritten."""

    source = r"$a \le \sqrt n$"

    assert markdown_to_telegram_html(source) == rf"<code>{source}</code>"
    assert "≤" not in markdown_to_telegram_html(source)


def test_dollars_inside_inline_code_are_not_latex() -> None:
    """Code examples need literal dollar signs and source syntax preserved byte for byte."""

    assert markdown_to_telegram_html(r"`$O(N^2)$`") == r"<code>$O(N^2)$</code>"


def test_safe_display_math_converts_but_unsafe_display_math_stays_code() -> None:
    """The safety decision should apply consistently to both inline and display delimiters."""

    source = "$$\nx + y\n$$ and " + r"$$\int_0^1 x^2\,dx$$"

    assert markdown_to_telegram_html(source) == (
        "\nx + y\n and " + r"<code>$$\int_0^1 x^2\,dx$$</code>"
    )


def test_currency_like_dollars_are_not_mistaken_for_math() -> None:
    """Conservative delimiter detection prevents ordinary prices from being rewritten."""

    assert markdown_to_telegram_html("price $5 and $10.") == "price $5 and $10."


def test_display_width_counts_ascii_and_east_asian_characters() -> None:
    """Cell widths must reflect Telegram monospace glyphs instead of code-point count."""

    assert display_width("ASCII") == 5
    assert display_width("中文") == 4
    assert display_width("Ａ，") == 4


def test_display_width_counts_mixed_text_correctly() -> None:
    """Mixed Latin and CJK cells are common in Gemini tables and must stay aligned."""

    assert display_width("A中Ｂ!") == 6


def test_ascii_table_renders_as_an_aligned_preformatted_block() -> None:
    """A compact table should remain scannable after Telegram strips Markdown pipes."""

    source = (
        "| Name | Score |\n"
        "| --- | --- |\n"
        "| Ada | 10 |\n"
        "| Grace | 9 |\n"
    )

    assert markdown_to_telegram_html(source) == (
        "<pre>Name   Score\n"
        "─────  ─────\n"
        "Ada    10\n"
        "Grace  9</pre>\n"
    )


def test_chinese_table_uses_display_width_for_alignment() -> None:
    """Double-width Chinese glyphs must not shift later columns out of alignment."""

    source = "| 名稱 | 值 |\n| --- | --- |\n| A | 中文 |\n"

    assert markdown_to_telegram_html(source) == (
        "<pre>名稱  值\n"
        "────  ────\n"
        "A     中文</pre>\n"
    )


def test_table_separator_is_replaced_by_a_solid_rule() -> None:
    """The established three-hyphen table output must remain byte-for-byte stable."""

    source = "| Left | Right |\n| :--- | ---: |\n| one | two |"

    assert render_markdown_chunks(source) == [
        "<pre>Left  Right\n"
        "────  ─────\n"
        "one   two</pre>"
    ]


@pytest.mark.parametrize(
    "separator",
    [
        "| :- | :- |",
        "| - | - |",
        "|---|---|",
        "| :-- | --: |",
        "| :-: | --- |",
    ],
)
def test_gfm_table_separator_variants_are_recognised(separator: str) -> None:
    """Accepting every GFM alignment form keeps valid compact tables readable."""

    source = f"| 名稱 | 值 |\n{separator}\n| 甲 | 乙 |"

    assert render_markdown_chunks(source) == [
        "<pre>名稱  值\n"
        "────  ──\n"
        "甲    乙</pre>"
    ]


def test_table_separator_column_count_must_match_header() -> None:
    """Keeping the column-count guard prevents ambiguous pipe text becoming a table."""

    source = "| 名稱 | 值 |\n| - | - | - |\n| 甲 | 乙 |"

    assert render_markdown_chunks(source) == [source]


def test_table_separator_cells_reject_non_syntax_characters() -> None:
    """Rejecting mixed-content cells prevents hyphenated prose from acting as syntax."""

    source = "| 名稱 | 值 |\n| -a- | --- |\n| 甲 | 乙 |"

    assert render_markdown_chunks(source) == [source]


@pytest.mark.parametrize(
    ("marked", "plain"),
    [
        ("**粗體**", "粗體"),
        ("*斜體*", "斜體"),
        ("`程式碼`", "程式碼"),
        ("~~刪除線~~", "刪除線"),
        ("[文字](https://example.test)", "文字（https://example.test）"),
    ],
)
def test_pre_table_cells_show_inline_markdown_as_plain_text(
    marked: str,
    plain: str,
) -> None:
    """Removing unusable delimiters keeps preformatted table values readable."""

    source = (
        "| 類型 | 值 |\n"
        "| --- | --- |\n"
        f"| {marked} | 一般 |\n"
    )

    rendered = markdown_to_telegram_html(source)
    pre_content = rendered.removeprefix("<pre>").removesuffix("</pre>\n")

    assert rendered.startswith("<pre>")
    assert plain in unescape(pre_content)
    assert marked not in pre_content
    assert "<" not in pre_content


def test_pre_table_width_uses_text_after_markdown_is_removed() -> None:
    """Measuring only visible glyphs prevents hidden delimiters from shifting columns."""

    source = (
        "| 名稱 | 值 |\n"
        "| --- | --- |\n"
        "| **粗體** | 一般 |\n"
    )

    assert markdown_to_telegram_html(source) == (
        "<pre>名稱  值\n"
        "────  ────\n"
        "粗體  一般</pre>\n"
    )


def test_list_fallback_still_renders_inline_markdown_as_html() -> None:
    """The mobile fallback needs emphasis and links because it is not monospace text."""

    source = (
        "| First | Second | Third | Fourth | Fifth |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| **粗體** | *斜體* | `code` | [site](https://example.test) | plain |\n"
    )

    rendered = markdown_to_telegram_html(source)

    assert rendered.startswith("<b>粗體</b>\n")
    assert f"{LIST_INDENT_CHARACTER * 2}◦ Second：<i>斜體</i>" in rendered
    assert f"{LIST_INDENT_CHARACTER * 2}◦ Third：<code>code</code>" in rendered
    assert (
        f'{LIST_INDENT_CHARACTER * 2}◦ Fourth：'
        '<a href="https://example.test">site</a>'
    ) in rendered
    assert "<pre>" not in rendered


def test_wide_table_wraps_cells_and_keeps_display_column_starts_aligned() -> None:
    """Wrapped CJK rows must retain exact visual column starts in Telegram monospace."""

    source = (
        "| 演算法特性 | 時間複雜度 | 核心行為模式 |\n"
        "| --- | --- | --- |\n"
        "| 基礎 Quick Sort | $O(N^2)$ | 發生最壞情況，產生極度不平衡的分割。 |\n"
        "| 優化 Quick Sort | $O(N \\log N)$ | 避開極值，維持高效分割。 |\n"
    )

    rendered = markdown_to_telegram_html(source)
    content = unescape(rendered.removeprefix("<pre>").removesuffix("</pre>\n"))
    lines = content.splitlines()

    assert rendered.startswith("<pre>")
    assert "<code>" not in rendered
    assert "O(N²)" in content
    assert "O(N log" in content
    assert all(display_width(line) <= TABLE_TARGET_WIDTH for line in lines)

    def text_at_display_column(line: str, column: int) -> str:
        position = 0
        for index, character in enumerate(line):
            if position == column:
                return line[index:]
            position += display_width(character)
        return "" if position <= column else pytest.fail("column splits a wide glyph")

    second_column_fragments = ("時間複雜", "度", "O(N²)", "O(N log", "N)")
    third_column_fragments = (
        "核心行為模式",
        "發生最壞情況，產生極",
        "度不平衡的分割。",
        "避開極值，維持高效分",
        "割。",
    )
    for fragment in second_column_fragments:
        assert any(text_at_display_column(line, 10).startswith(fragment) for line in lines)
    for fragment in third_column_fragments:
        assert any(text_at_display_column(line, 20).startswith(fragment) for line in lines)


def test_chinese_closing_punctuation_does_not_start_a_wrapped_line() -> None:
    """Keeping a Chinese full stop with preceding text avoids a visually orphaned mark."""

    lines = _RENDERING._wrap_table_cell("平衡的分割。仍然繼續", 10)

    assert lines == ("平衡的分", "割。仍然繼", "續")
    assert all(not line.startswith("。") for line in lines)


def test_chinese_opening_punctuation_does_not_end_a_wrapped_line() -> None:
    """Moving an opening bracket forward keeps its enclosed Chinese phrase connected."""

    lines = _RENDERING._wrap_table_cell("排序方法（最差情況）", 10)

    assert lines == ("排序方法", "（最差情", "況）")
    assert all(not line.endswith("（") for line in lines)


@pytest.mark.parametrize("cell", ["（甲", "。甲"])
def test_extremely_narrow_table_wrap_terminates_without_empty_lines(cell: str) -> None:
    """An impossible one-cell width must favor progress over punctuation aesthetics."""

    lines = _RENDERING._wrap_table_cell(cell, 1)

    assert "".join(lines) == cell
    assert all(lines)


def test_ascii_table_cell_wrapping_remains_unchanged() -> None:
    """Kinsoku rules must not disturb established wrapping for ordinary ASCII text."""

    assert _RENDERING._wrap_table_cell("alpha beta gamma", 10) == (
        "alpha",
        "beta gamma",
    )
    assert _RENDERING._wrap_table_cell("abcdefghijkl", 5) == (
        "abcde",
        "fghij",
        "kl",
    )


def test_chinese_table_cell_wraps_between_characters() -> None:
    """CJK has no required spaces, so character-boundary wrapping avoids overflow."""

    chinese = "天地玄黃宇宙洪荒日月盈昴辰宿列張寒來暑往"
    source = (
        "| 類型 | 說明 |\n"
        "| --- | --- |\n"
        f"| 中文 | {chinese} |\n"
    )

    content = unescape(
        markdown_to_telegram_html(source)
        .removeprefix("<pre>")
        .removesuffix("</pre>\n")
    )
    lines = content.splitlines()

    for fragment in (chinese[:15], chinese[15:]):
        line = next(line for line in lines if fragment in line)
        assert display_width(line[: line.index(fragment)]) == 10


def test_latin_table_cell_prefers_whitespace_before_hard_wrap() -> None:
    """Keeping Latin words whole makes wrapped prose substantially easier to scan."""

    prose = "alpha beta gamma delta epsilons zeta"
    source = (
        "| Label | Description |\n"
        "| --- | --- |\n"
        f"| X | {prose} |\n"
    )

    content = unescape(
        markdown_to_telegram_html(source)
        .removeprefix("<pre>")
        .removesuffix("</pre>\n")
    )

    assert "X         alpha beta gamma delta\n          epsilons zeta" in content
    assert "epsilon\n" not in content


def test_too_many_wide_columns_still_fall_back_to_list_items() -> None:
    """Only an impossible minimum-width allocation should sacrifice table comparison."""

    headings = [f"Column {index} heading" for index in range(1, 6)]
    source = (
        f"| {' | '.join(headings)} |\n"
        f"| {' | '.join(['---'] * len(headings))} |\n"
        "| first | second | third | fourth | fifth |\n"
    )

    rendered = markdown_to_telegram_html(source)

    assert len(headings) * TABLE_MIN_COLUMN_WIDTH + 2 * (len(headings) - 1) > TABLE_TARGET_WIDTH
    assert rendered.startswith("first\n")
    assert f"{LIST_INDENT_CHARACTER * 2}◦ Column 2 heading：second" in rendered
    assert "<pre>" not in rendered


def test_isolated_pipe_is_not_mistaken_for_a_table() -> None:
    """Ordinary prose containing a pipe must retain its existing rendering."""

    assert markdown_to_telegram_html("Use | as a separator.") == "Use | as a separator."


def test_table_like_row_without_separator_is_not_converted() -> None:
    """Requiring a separator row prevents one-off pipe-delimited prose from changing."""

    source = "| this looks | table-like |\nbut it has no separator"

    assert markdown_to_telegram_html(source) == source


def test_split_message_keeps_a_table_in_one_chunk() -> None:
    """Moving a whole table preserves the header context for every data row."""

    table = "| A | B |\n| --- | --- |\n| 1 | 2 |\n"
    source = f"{'x' * 40}\n{table}"

    chunks = split_message(source, limit=64)

    assert chunks == ["x" * 40 + "\n", table]


def test_split_prefers_paragraph_then_newline_then_hard_cut() -> None:
    paragraph_text = "a" * 20 + "\n\n" + "b" * 20 + "\n" + "c" * 20
    line_text = "a" * 20 + "\n" + "b" * 30
    hard_text = "x" * 70

    assert split_message(paragraph_text, 50)[0] == "a" * 20 + "\n\n"
    assert split_message(line_text, 40)[0] == "a" * 20 + "\n"
    assert split_message(hard_text, 40) == ["x" * 40, "x" * 30]


def test_split_rejects_tiny_limit_and_empty_input() -> None:
    with pytest.raises(ValueError, match="leave room"):
        split_message("text", 31)
    assert split_message("") == []


def test_long_code_block_is_closed_and_reopened_with_language() -> None:
    code = "".join(f"line {index:04d} = {'x' * 48}\n" for index in range(150))
    source = f"intro\n\n```python\n{code}```\nend"

    chunks = split_message(source)

    assert len(chunks) >= 3
    assert all(len(chunk) <= MAX_MESSAGE_LENGTH for chunk in chunks)
    code_chunks = [chunk for chunk in chunks if chunk.startswith("```python\n")]
    assert len(code_chunks) >= 2
    assert all(chunk.endswith("```\n") for chunk in code_chunks[:-1])
    assert all(markdown_to_telegram_html(chunk).count("<pre>") == 1 for chunk in code_chunks)
    rendered_code = "".join(
        markdown_to_telegram_html(chunk)
        .replace('<pre><code class="language-python">', "")
        .replace("</code></pre>", "")
        for chunk in code_chunks
    )
    assert code in rendered_code


def test_long_unclosed_fence_and_empty_fence_split_safely() -> None:
    unclosed = "```text\n" + "z" * 500
    chunks = split_message(unclosed, 100)

    assert len(chunks) > 1
    assert all(chunk.startswith("```text\n") and chunk.endswith("```\n") for chunk in chunks)
    assert all(len(chunk) <= 100 for chunk in chunks)

    # Exercise the defensive empty-content path with an oversized opener.
    empty = "```" + "a" * 80
    assert split_message(empty, 40) == ["```\n```\n"]


def test_code_block_too_small_for_requested_limit() -> None:
    source = "`" * 30 + "lang\ncontent that does not fit\n" + "`" * 30

    with pytest.raises(ValueError, match="too small for a fenced code block"):
        split_message(source, 32)


def test_rendered_chunks_account_for_html_expansion() -> None:
    source = ("<&> " * 1300) + "\n\n" + ("**bold** " * 600)

    chunks = render_markdown_chunks(source)

    assert len(chunks) > 2
    assert all(len(chunk) <= MAX_MESSAGE_LENGTH for chunk in chunks)
    assert "&lt;&amp;&gt;" in "".join(chunks)


def test_rendered_chunks_reject_impossibly_small_html_budget() -> None:
    with pytest.raises(ValueError, match="too small for the rendered HTML"):
        render_markdown_chunks("&" * 40, 32)


def test_googleusercontent_artifact_urls_and_orphan_suffixes_are_removed() -> None:
    source = (
        "before\n"
        "http://googleusercontent.com/image_generation_content/0_551\n"
        "middle\n"
        "https://googleusercontent.com/image_generation_content/551\n"
        "_0\n"
        "after"
    )

    assert strip_googleusercontent_artifacts(source) == "before\nmiddle\nafter"
    assert render_markdown_chunks(source) == ["before\nmiddle\nafter"]


def test_orphan_cleanup_does_not_remove_normal_inline_underscore_text() -> None:
    source = "變數 _1 是有效正文。\nprefix _551\n_2 suffix"

    assert strip_googleusercontent_artifacts(source) == source


def test_artifact_only_text_renders_as_no_chunks() -> None:
    assert render_markdown_chunks("_551\n") == []
    assert render_markdown_chunks(
        "http://googleusercontent.com/image_generation_content/1_0\n"
    ) == []


def test_agent_image_and_follow_up_tags_are_removed_completely() -> None:
    source = (
        "before\n"
        '<Image alt="view" src="image_agent_tag_123"/>\n'
        '<FollowUp label="useful suggestion" query="unused query"/>\n'
        "after"
    )

    assert markdown_to_telegram_html(source) == "before\n\nafter"
    assert markdown_to_telegram_html('<Image src="first"/> text') == "text"
    assert markdown_to_telegram_html('text <Image src="last"/>') == "text"


def test_agent_tag_inside_code_fence_is_preserved_verbatim() -> None:
    source = (
        "說明：\n"
        "```html\n"
        '<Image src="x"/>\n'
        "---\n"
        "```\n"
        "結束\n"
        '<Image src="outside"/>\n'
    )

    assert markdown_to_telegram_html(source) == (
        "說明：\n"
        '<pre><code class="language-html">'
        '&lt;Image src="x"/&gt;\n'
        "---\n"
        "</code></pre>結束"
    )


def test_unknown_agent_tag_is_removed_but_lowercase_html_is_untouched() -> None:
    source = '<Suggestion foo="bar"/>\n<widget foo="bar"/>'

    assert markdown_to_telegram_html(source) == '&lt;widget foo="bar"/&gt;'


def test_paired_agent_tags_from_production_are_removed_completely() -> None:
    """This exact pair reached real users as escaped markup at the end of an answer."""

    source = (
        "最後一段內容。\n"
        "\n"
        '<ElicitationsGroup message="想要進一步了解哪一部分？">\n'
        "\n"
        "</ElicitationsGroup>\n"
    )

    assert markdown_to_telegram_html(source) == "最後一段內容。"


def test_self_closing_agent_tag_is_still_removed() -> None:
    """The paired form was added alongside the self-closing form, not in place of it."""

    source = 'before\n<FollowUp label="suggestion" query="unused"/>\nafter'

    assert markdown_to_telegram_html(source) == "before\n\nafter"


def test_text_between_paired_agent_tags_is_preserved() -> None:
    """Upstream sometimes wraps real content in a tag, so dropping the span would lose the answer."""

    source = '<Notice level="info">這段文字有意義</Notice>'

    assert markdown_to_telegram_html(source) == "這段文字有意義"


def test_lowercase_html_tags_are_untouched_by_the_paired_tag_pattern() -> None:
    """Matching lower-case names would delete the user's own literal HTML examples."""

    source = "<b>bold</b> and <code>snippet</code>"

    assert markdown_to_telegram_html(source) == (
        "&lt;b&gt;bold&lt;/b&gt; and &lt;code&gt;snippet&lt;/code&gt;"
    )


def test_paired_agent_tag_inside_code_fence_is_preserved_verbatim() -> None:
    """A fence is quoted source; silently editing it would corrupt what the user asked about."""

    source = (
        "```html\n"
        '<ElicitationsGroup message="x">\n'
        "body\n"
        "</ElicitationsGroup>\n"
        "```\n"
    )

    assert markdown_to_telegram_html(source) == (
        '<pre><code class="language-html">'
        '&lt;ElicitationsGroup message="x"&gt;\n'
        "body\n"
        "&lt;/ElicitationsGroup&gt;\n"
        "</code></pre>"
    )


def test_arithmetic_comparison_is_not_mistaken_for_an_agent_tag() -> None:
    """Chained comparisons are ordinary prose; eating them would silently change the maths."""

    assert markdown_to_telegram_html("a < b > c") == "a &lt; b &gt; c"


def test_headings_are_bold_and_horizontal_rules_are_removed() -> None:
    source = "# One\n## Two\n### Three\n\n---\n\nafter"

    assert markdown_to_telegram_html(source) == (
        "<b>One</b>\n<b>Two</b>\n<b>Three</b>\n\nafter"
    )


def test_agent_tag_only_text_renders_as_no_chunks() -> None:
    assert render_markdown_chunks('<Image src="image_agent_tag_123"/>\n') == []


def test_real_web_image_fixture_removes_agent_tag_rule_and_bolds_heading() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "image-web.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    source = fixture["candidates"][0]["text"]

    rendered = markdown_to_telegram_html(source)

    assert "Image alt=" not in rendered
    assert "---" not in rendered
    assert "<b>外觀核心設計特點</b>" in rendered
    assert f"{LIST_INDENT_CHARACTER * 2}◦ <b>古錢幣符號：</b>" in rendered


def test_thoughts_are_wrapped_in_an_expandable_blockquote() -> None:
    """Expandable markup keeps supplementary reasoning compact in Telegram."""

    assert _RENDERING.render_thoughts_blockquote("Reasoning", budget=100) == (
        "<blockquote expandable>Reasoning</blockquote>"
    )


@pytest.mark.parametrize(
    ("language", "title"),
    [
        (LANGUAGE_ENGLISH, "Thought process (18 seconds)\n"),
        (LANGUAGE_CHINESE, "思考過程（18 秒）\n"),
    ],
)
def test_thoughts_seconds_title_is_the_first_expandable_line(
    language: str,
    title: str,
) -> None:
    """The collapsed reasoning heading must follow the active chat language."""

    rendered = _RENDERING.render_thoughts_blockquote(
        "Reasoning",
        budget=100,
        seconds=18.9,
        language=language,
    )

    assert rendered == f"<blockquote expandable>{title}Reasoning</blockquote>"


def test_thoughts_none_seconds_preserves_existing_output() -> None:
    assert _RENDERING.render_thoughts_blockquote(
        "Reasoning",
        budget=100,
        seconds=None,
    ) == "<blockquote expandable>Reasoning</blockquote>"


def test_thoughts_escape_html_special_characters() -> None:
    """Escaping prevents reasoning text from corrupting Telegram's HTML markup."""

    assert _RENDERING.render_thoughts_blockquote("a < b > c & d", budget=100) == (
        "<blockquote expandable>a &lt; b &gt; c &amp; d</blockquote>"
    )


def test_thoughts_render_inline_markdown() -> None:
    """Reasoning emphasis and code should not appear with literal Markdown delimiters."""

    assert _RENDERING.render_thoughts_blockquote(
        "**Defining the Comparison** with *care* and `values`",
        budget=150,
    ) == (
        "<blockquote expandable><b>Defining the Comparison</b> with "
        "<i>care</i> and <code>values</code></blockquote>"
    )


def test_thoughts_keep_block_syntax_flat_and_render_headings_as_bold() -> None:
    """Flat quote contents avoid Telegram's unreliable nested block rendering."""

    source = (
        "# Heading\n"
        "- **item**\n"
        "```python\n"
        "code\n"
        "```\n"
        "| A | B |\n"
        "| --- | --- |"
    )

    rendered = _RENDERING.render_thoughts_blockquote(source, budget=500)

    assert "<b>Heading</b>" in rendered
    assert "- <b>item</b>" in rendered
    assert "```python" in rendered
    assert "| --- | --- |" in rendered
    assert "<pre>" not in rendered
    assert "• item" not in rendered
    assert rendered.count("<blockquote") == 1


def test_thoughts_truncation_accounts_for_inline_tag_expansion() -> None:
    """Added inline tags must never make a truncated Telegram message exceed budget."""

    opening, closing = "<blockquote expandable>", "</blockquote>"
    note = _RENDERING.THOUGHTS_TRUNCATION_NOTE
    budget = len(opening) + len(closing) + len(note) + 20

    rendered = _RENDERING.render_thoughts_blockquote(
        "**bold** & " * 20,
        budget=budget,
    )

    assert "<b>bold</b>" in rendered
    assert rendered.endswith(f"{note}{closing}")
    assert len(rendered) <= budget


@pytest.mark.parametrize("thoughts", ["", " \t\n "])
def test_empty_or_whitespace_only_thoughts_render_nothing(thoughts: str) -> None:
    """Omitting empty reasoning avoids sending a meaningless Telegram element."""

    assert _RENDERING.render_thoughts_blockquote(thoughts, budget=100) == ""


@pytest.mark.parametrize("budget_delta", [-1, 0])
def test_thoughts_render_nothing_when_budget_cannot_fit_tags(
    budget_delta: int,
) -> None:
    """A complete HTML wrapper is required so a tight budget never emits invalid markup."""

    tags_length = len("<blockquote expandable>") + len("</blockquote>")

    assert (
        _RENDERING.render_thoughts_blockquote(
            "Reasoning",
            budget=tags_length + budget_delta,
        )
        == ""
    )


def test_thoughts_are_truncated_with_note_inside_budget() -> None:
    """Reasoning must yield limited message space to the answer while explaining data loss."""

    opening, closing = "<blockquote expandable>", "</blockquote>"
    note = _RENDERING.THOUGHTS_TRUNCATION_NOTE
    budget = len(opening) + len(closing) + len(note) + 12

    rendered = _RENDERING.render_thoughts_blockquote("x" * 100, budget=budget)

    assert rendered.endswith(f"{note}{closing}")
    assert len(rendered) <= budget


def test_thoughts_with_seconds_are_truncated_inside_budget() -> None:
    """A localized heading must still leave the reasoning inside its budget."""

    opening, closing = "<blockquote expandable>", "</blockquote>"
    title = translate(
        "thoughts.title_elapsed.many",
        LANGUAGE_ENGLISH,
        seconds=18,
    )
    note = _RENDERING.THOUGHTS_TRUNCATION_NOTE
    budget = len(opening) + len(title) + len(closing) + len(note) + 12

    rendered = _RENDERING.render_thoughts_blockquote(
        "x" * 100,
        budget=budget,
        seconds=18.9,
    )

    assert rendered.startswith(f"{opening}{title}")
    assert rendered.endswith(f"{note}{closing}")
    assert len(rendered) <= budget


@pytest.mark.parametrize(("character", "entity"), [("<", "&lt;"), ("&", "&amp;")])
def test_thoughts_truncation_preserves_complete_html_entities(
    character: str,
    entity: str,
) -> None:
    """Whole entities keep truncated reasoning valid for Telegram's strict HTML parser."""

    opening, closing = "<blockquote expandable>", "</blockquote>"
    note = _RENDERING.THOUGHTS_TRUNCATION_NOTE
    budget = len(opening) + len(closing) + len(note) + 23

    rendered = _RENDERING.render_thoughts_blockquote(character * 100, budget=budget)
    escaped_prefix = rendered.removeprefix(opening).removesuffix(f"{note}{closing}")

    assert escaped_prefix
    assert escaped_prefix == entity * (len(escaped_prefix) // len(entity))
    assert len(rendered) <= budget


def test_thoughts_strip_surrounding_whitespace() -> None:
    """Trimming presentation-only whitespace preserves room for useful reasoning."""

    assert _RENDERING.render_thoughts_blockquote(" \n  Reasoning \t ", budget=100) == (
        "<blockquote expandable>Reasoning</blockquote>"
    )
