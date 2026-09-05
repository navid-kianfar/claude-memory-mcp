"""The markdown/HTML boundary between the store and the board.

Every assertion here is about one of four promises the converter makes, and
each of them has a failure the board would show a human:

1. The HTML is the subset TipTap holds - anything else is dropped the moment
   someone edits the card.
2. Text is escaped before any tag is added - asoode does not sanitize and its
   frontend injects the string into the DOM.
3. A round trip is stable, so a description edited on the board and synced back
   is not slowly mangled by the trip.
4. Plain text stays plain, because the store is already full of it.
"""

import pytest

from memory_mcp.utils.richtext import (
    html_to_markdown,
    looks_like_html,
    markdown_to_html,
)


class TestMarkdownToHtml:
    def test_plain_text_becomes_one_paragraph(self):
        assert markdown_to_html("just a sentence") == "<p>just a sentence</p>"

    def test_empty_input_produces_nothing(self):
        """Not "<p></p>": asoode would show that as a description that exists."""
        assert markdown_to_html("") == ""
        assert markdown_to_html(None) == ""
        assert markdown_to_html("   \n  ") == ""

    def test_headings_clamp_to_three(self):
        """TipTap here is configured with levels [1, 2, 3]; an h4 is unrepresentable."""
        assert markdown_to_html("# One") == "<h1>One</h1>"
        assert markdown_to_html("### Three") == "<h3>Three</h3>"
        assert markdown_to_html("##### Five") == "<h3>Five</h3>"

    def test_headings_become_bold_paragraphs_for_comments(self):
        """asoode's comment editor runs compact, which turns the heading node off."""
        assert (
            markdown_to_html("## Findings", allow_headings=False)
            == "<p><strong>Findings</strong></p>"
        )

    def test_bullet_list(self):
        assert markdown_to_html("- one\n- two") == "<ul><li>one</li><li>two</li></ul>"

    def test_ordered_list(self):
        assert markdown_to_html("1. one\n2. two") == "<ol><li>one</li><li>two</li></ol>"

    def test_nested_list_sits_inside_its_parent_item(self):
        """TipTap requires the nested ul INSIDE the li, not beside it."""
        html = markdown_to_html("- outer\n  - inner\n- after")
        assert html == "<ul><li>outer<ul><li>inner</li></ul></li><li>after</li></ul>"

    def test_a_list_after_a_paragraph_is_its_own_block(self):
        html = markdown_to_html("intro:\n- one")
        assert html == "<p>intro:</p><ul><li>one</li></ul>"

    def test_inline_marks(self):
        assert markdown_to_html("**b** *i* ~~s~~ `c`") == (
            "<p><strong>b</strong> <em>i</em> <s>s</s> <code>c</code></p>"
        )

    def test_underscores_do_not_italicise_inside_a_word(self):
        """snake_case_names are the common case and must survive."""
        assert markdown_to_html("task_repository_py") == "<p>task_repository_py</p>"

    def test_markers_inside_a_code_span_stay_literal(self):
        assert markdown_to_html("`**not bold**`") == "<p><code>**not bold**</code></p>"

    def test_fenced_code_block_keeps_its_language(self):
        html = markdown_to_html("```python\nx = 1\n```")
        assert html == '<pre><code class="language-python">x = 1</code></pre>'

    def test_blockquote(self):
        assert markdown_to_html("> quoted") == "<blockquote><p>quoted</p></blockquote>"

    def test_horizontal_rule(self):
        assert markdown_to_html("a\n\n---\n\nb") == "<p>a</p><hr><p>b</p>"

    def test_single_newline_is_a_hard_break(self):
        """Two deliberately separate lines must not become one run-on sentence."""
        assert markdown_to_html("one\ntwo") == "<p>one<br>two</p>"

    def test_links_stay_literal(self):
        """StarterKit has no Link extension: an <a> loses its URL on the first edit."""
        html = markdown_to_html("see [the docs](https://example.com)")
        assert html == "<p>see [the docs](https://example.com)</p>"

    def test_a_table_degrades_to_a_code_block(self):
        html = markdown_to_html("| a | b |\n|---|---|\n| 1 | 2 |")
        assert html.startswith("<pre><code>| a | b |")
        assert "<table" not in html

    def test_an_unterminated_fence_runs_to_the_end(self):
        """A half-written description is still a description."""
        assert markdown_to_html("```\nx = 1") == "<pre><code>x = 1</code></pre>"


class TestHtmlInput:
    """"Must support md or basic html" - a body that is already HTML."""

    def test_html_is_normalised_rather_than_escaped(self):
        html = markdown_to_html("<h2>Pasted</h2><ul><li>one</li><li>two</li></ul>")
        assert html == "<h2>Pasted</h2><ul><li>one</li><li>two</li></ul>"

    def test_tags_outside_the_subset_are_dropped_not_shown(self):
        """A pasted table would vanish in TipTap anyway; keep the text."""
        html = markdown_to_html("<p>a</p><table><tr><td>x</td></tr></table>")
        assert "<table" not in html and "x" in html

    def test_a_script_is_not_html_input(self):
        """It opens with a tag, but not one of ours - so it is escaped text."""
        html = markdown_to_html("<script>alert(1)</script>")
        assert "<script>" not in html and "&lt;script&gt;" in html

    def test_markdown_mentioning_a_tag_stays_markdown(self):
        """The strict opener check is what protects an example in backticks."""
        html = markdown_to_html("Use `<p>` for a paragraph.")
        assert html == "<p>Use <code>&lt;p&gt;</code> for a paragraph.</p>"


class TestEscaping:
    """asoode does not sanitize and renders with dangerouslySetInnerHTML."""

    def test_script_tags_are_escaped(self):
        html = markdown_to_html("<script>alert(1)</script>")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_escaping_happens_inside_every_construct(self):
        for source in ("# <b>x</b>", "- <b>x</b>", "> <b>x</b>", "**<b>x</b>**"):
            assert "<b>" not in markdown_to_html(source), source

    def test_code_blocks_escape_their_body(self):
        html = markdown_to_html("```\n<img src=x onerror=alert(1)>\n```")
        assert "<img" not in html
        assert "&lt;img" in html

    def test_a_code_language_cannot_break_out_of_the_attribute(self):
        html = markdown_to_html('```py"><script>x</script>\nbody\n```')
        assert "<script>" not in html


class TestHtmlToMarkdown:
    def test_paragraphs(self):
        assert html_to_markdown("<p>one</p><p>two</p>") == "one\n\ntwo"

    def test_headings(self):
        assert html_to_markdown("<h2>Title</h2>") == "## Title"

    def test_lists(self):
        assert html_to_markdown("<ul><li>a</li><li>b</li></ul>") == "- a\n- b"

    def test_ordered_lists_number_themselves(self):
        assert html_to_markdown("<ol><li>a</li><li>b</li></ol>") == "1. a\n2. b"

    def test_marks(self):
        assert html_to_markdown("<p><strong>b</strong> <em>i</em></p>") == "**b** *i*"

    def test_code_block(self):
        assert html_to_markdown("<pre><code>x = 1</code></pre>") == "```\nx = 1\n```"

    def test_entities_are_decoded(self):
        assert html_to_markdown("<p>a &amp; b &lt;c&gt;</p>") == "a & b <c>"

    def test_unknown_tags_keep_their_text(self):
        """A description pasted from elsewhere degrades to readable text."""
        assert html_to_markdown("<p><span class=x>kept</span></p>") == "kept"

    def test_tagless_text_is_returned_untouched(self):
        """The inbound path calls this on everything; plain rows must not change."""
        original = "asoode has no optimistic concurrency (no version/etag)."
        assert html_to_markdown(original) == original

    def test_empty(self):
        assert html_to_markdown("") == ""
        assert html_to_markdown(None) == ""


class TestRoundTrip:
    """A description edited on the board and synced back must not drift."""

    @pytest.mark.parametrize(
        "source",
        [
            "just a sentence",
            "# Heading\n\nA paragraph.",
            "- one\n- two",
            "1. one\n2. two",
            "**bold** and *italic* and `code`",
            "```\nx = 1\n```",
            "> quoted",
            "A paragraph.\n\n## Section\n\n- a\n- b",
            "- outer\n  - nested\n- after",
        ],
    )
    def test_markdown_survives_the_trip(self, source):
        assert html_to_markdown(markdown_to_html(source)) == source

    def test_a_second_trip_changes_nothing(self):
        """Whatever the first pass normalises, the second must leave alone."""
        source = "# T\n\nintro\n\n- a\n  - nested\n\n> q\n\n| a | b |\n|---|---|\n| 1 | 2 |"
        once = html_to_markdown(markdown_to_html(source))
        twice = html_to_markdown(markdown_to_html(once))
        assert once == twice


class TestHelpers:
    def test_looks_like_html(self):
        assert looks_like_html("<p>hi</p>")
        assert looks_like_html("<ul><li>x</li></ul>")
        assert not looks_like_html("plain text")
        assert not looks_like_html("2 < 3 and 4 > 1")
        assert not looks_like_html("")
