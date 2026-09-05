"""When a description is really several tasks written as one.

The hint is advisory and appears in every task-creating tool result, so the
thing worth testing is what it stays QUIET about. A hint that fires on every
long description trains the agent to write short ones, which is the opposite of
what this project wants from a description.
"""

from memory_mcp.utils.decomposition import MIN_CHARS, decomposition_hint

DIVIDED = (
    "The endpoint, the screen and the docs all have to change.\n\n"
    "1. Add POST /api/thing in web/routes.py, taking the same body the CLI does.\n"
    "2. Call it from the Things tab, with the empty and error states.\n"
    "3. Write the README section, including the curl example.\n"
    "4. Backfill the rows written before the column existed.\n\n"
) + "Context that explains why all of this is needed at once. " * 20

PROSE = (
    "One change, argued at length. " * 60
)


class TestItFires:
    def test_a_long_divided_description_is_flagged(self):
        hint = decomposition_hint(DIVIDED)
        assert hint is not None
        assert "SUB-TASK" in hint and "parent_id" in hint

    def test_the_hint_counts_what_it_saw(self):
        hint = decomposition_hint(DIVIDED)
        assert "4 numbered items" in hint
        assert str(len(DIVIDED.strip())) in hint

    def test_headings_count_as_divisions(self):
        text = "## One\n\n" + "a " * 700 + "\n\n## Two\n\nb\n\n## Three\n\nc\n"
        assert decomposition_hint(text) is not None

    def test_checkboxes_count_as_divisions(self):
        text = "x " * 600 + "\n\n- [ ] one\n- [ ] two\n- [x] three\n"
        assert decomposition_hint(text) is not None


class TestItStaysQuiet:
    def test_a_long_single_explanation_is_not_nagged_at(self):
        """A thorough description is the goal, not a smell."""
        assert len(PROSE) > MIN_CHARS
        assert decomposition_hint(PROSE) is None

    def test_a_short_divided_description_is_left_alone(self):
        """Three headings over four lines is formatting, not a hidden plan."""
        assert decomposition_hint("## a\n\n## b\n\n## c\n") is None

    def test_a_sub_task_is_never_hinted_at(self):
        """One level is the limit - there is nothing to decompose into."""
        assert decomposition_hint(DIVIDED, has_parent=True) is None

    def test_a_task_that_already_has_sub_tasks_is_left_alone(self):
        """Then the long description is the parent's overview, which is right."""
        assert decomposition_hint(DIVIDED, child_count=3) is None

    def test_empty_and_missing_descriptions(self):
        assert decomposition_hint(None) is None
        assert decomposition_hint("") is None
        assert decomposition_hint("   ") is None
