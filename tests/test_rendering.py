from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

import pytest


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


def test_lists_headings_and_blockquotes_use_supported_plain_forms() -> None:
    source = "# Heading\n- **one**\n  * two\n1. three\n> quoted *text*\n"

    assert markdown_to_telegram_html(source) == (
        "Heading\n"
        "• <b>one</b>\n"
        "  • two\n"
        "1. three\n"
        "<blockquote>quoted <i>text</i></blockquote>\n"
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


def test_latex_fragments_remain_copyable_code() -> None:
    source = (
        r"Inline $E = mc^2$ and display $$\int_0^1 x^2\,dx$$; price $5 and $10."
        "\n$$\nx + y\n$$"
    )

    assert markdown_to_telegram_html(source) == (
        "Inline <code>$E = mc^2$</code> and display "
        r"<code>$$\int_0^1 x^2\,dx$$</code>; price $5 and $10."
        "\n<code>$$\nx + y\n$$</code>"
    )


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
