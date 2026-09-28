"""Tests for the Mustache-SUBSET renderer (Phase 2 chunk 1).

Covers exactly the grammar the templating spec §1 promises:

  * `{{SCALAR}}` — happy path + unknown-key raises + no silent stub.
  * `{{#if KEY}}...{{/if}}` — true / false / missing-key-falsey /
    `{{else}}` branch selection.
  * `{{#each LIST}}...{{/each}}` — list of dicts + list of scalars
    (`{{.}}`) + missing/empty list.
  * Nested sections (`{{#each}}` inside `{{#if}}`).
  * Malformed / unbalanced tags raise `ValueError`.
  * `find_unrendered_markers` returns leftover markers in order.
"""

from __future__ import annotations

import pytest

from mineru_cli.install.renderer import (
    find_unrendered_markers,
    render_template,
)


# --- Scalar substitution --------------------------------------------------


def test_scalar_substitution_happy_path() -> None:
    """A simple `{{KEY}}` swaps in `str(context[key])`."""
    out = render_template(
        "hello, {{USER_NAME}}! from {{PERSONA_NAME}}",
        {"USER_NAME": "Sam", "PERSONA_NAME": "Mineru"},
    )
    assert out == "hello, Sam! from Mineru"


def test_scalar_coerces_non_string_values_via_str() -> None:
    """Numbers, bools, and Path-likes all render via `str(value)`."""
    out = render_template(
        "n={{N}} b={{B}} tz={{TZ}}",
        {"N": 42, "B": True, "TZ": "America/Los_Angeles"},
    )
    assert out == "n=42 b=True tz=America/Los_Angeles"


def test_scalar_unknown_key_raises_naming_it() -> None:
    """Drift must fail loud — an unknown key raises with the key named."""
    with pytest.raises(ValueError) as exc:
        render_template("hi {{MISSING_KEY}}", {"USER_NAME": "Sam"})
    assert "MISSING_KEY" in str(exc.value)


def test_scalar_no_silent_stub_when_missing() -> None:
    """No path leaves `{{KEY}}` in the output as a silent stub."""
    with pytest.raises(ValueError):
        render_template("{{PERSONA_EMOJI}}", {})


# --- {{#if}} / {{else}} ---------------------------------------------------


def test_if_true_includes_then_block() -> None:
    out = render_template(
        "prefix {{#if FLAG}}yes{{/if}} suffix",
        {"FLAG": True},
    )
    assert out == "prefix yes suffix"


def test_if_false_omits_then_block() -> None:
    out = render_template(
        "prefix {{#if FLAG}}yes{{/if}} suffix",
        {"FLAG": False},
    )
    assert out == "prefix  suffix"


def test_if_missing_key_is_falsey() -> None:
    """`{{#if MISSING}}` cleanly omits (missing key = falsey by spec)."""
    out = render_template(
        "prefix {{#if OPTIONAL}}yes{{/if}} suffix",
        {},
    )
    assert out == "prefix  suffix"


def test_if_empty_list_is_falsey() -> None:
    """An empty list is falsey (matches natural expectation)."""
    out = render_template(
        "{{#if L}}yes{{/if}}",
        {"L": []},
    )
    assert out == ""


def test_if_else_true_picks_then_branch() -> None:
    out = render_template(
        "{{#if F}}Y{{else}}N{{/if}}",
        {"F": True},
    )
    assert out == "Y"


def test_if_else_false_picks_else_branch() -> None:
    out = render_template(
        "{{#if F}}Y{{else}}N{{/if}}",
        {"F": False},
    )
    assert out == "N"


def test_if_else_missing_key_picks_else_branch() -> None:
    out = render_template(
        "{{#if OPTIONAL}}yes{{else}}nope{{/if}}",
        {},
    )
    assert out == "nope"


# --- {{#each}} ------------------------------------------------------------


def test_each_over_list_of_dicts_resolves_item_fields() -> None:
    """Inside `{{#each}}`, plain keys resolve against the current item."""
    out = render_template(
        "{{#each PEOPLE}}[{{name}}={{relation}}]{{/each}}",
        {
            "PEOPLE": [
                {"name": "Robin", "relation": "partner"},
                {"name": "River", "relation": "son"},
            ],
        },
    )
    assert out == "[Robin=partner][River=son]"


def test_each_over_list_of_scalars_uses_dot() -> None:
    """`{{.}}` inside `{{#each}}` resolves to the current scalar item."""
    out = render_template(
        "{{#each COLORS}}<{{.}}>{{/each}}",
        {"COLORS": ["red", "green", "blue"]},
    )
    assert out == "<red><green><blue>"


def test_each_over_missing_key_renders_zero_iterations() -> None:
    """A missing list key is treated as an empty list (zero body runs)."""
    out = render_template(
        "before-{{#each MISSING}}<{{.}}>{{/each}}-after",
        {},
    )
    assert out == "before--after"


def test_each_over_empty_list_renders_zero_iterations() -> None:
    out = render_template(
        "start{{#each L}}x{{/each}}end",
        {"L": []},
    )
    assert out == "startend"


def test_each_falls_through_to_outer_scope_for_unknown_key() -> None:
    """Inner scope wins; missing keys fall back to outer scope."""
    out = render_template(
        "{{#each ITEMS}}{{USER_NAME}}: {{name}}\n{{/each}}",
        {"USER_NAME": "Sam", "ITEMS": [{"name": "a"}, {"name": "b"}]},
    )
    assert out == "Sam: a\nSam: b\n"


def test_each_raises_on_non_list_operand() -> None:
    """`{{#each}}` over a dict / str / int is a template defect."""
    with pytest.raises(ValueError) as exc:
        render_template("{{#each L}}x{{/each}}", {"L": "not a list"})
    assert "each" in str(exc.value).lower()


# --- Nesting --------------------------------------------------------------


def test_each_nested_inside_if_renders_correctly() -> None:
    """The most useful nesting: iterate a list only when a flag is set."""
    out = render_template(
        "{{#if SHOW}}"
        "{{#each PEOPLE}}({{name}}){{/each}}"
        "{{/if}}",
        {
            "SHOW": True,
            "PEOPLE": [{"name": "A"}, {"name": "B"}, {"name": "C"}],
        },
    )
    assert out == "(A)(B)(C)"


def test_if_nested_inside_each_renders_correctly() -> None:
    """A conditional inside an iteration works too."""
    out = render_template(
        "{{#each ROWS}}"
        "{{name}}"
        "{{#if starred}}*{{/if}}"
        ";"
        "{{/each}}",
        {
            "ROWS": [
                {"name": "a", "starred": True},
                {"name": "b", "starred": False},
                {"name": "c", "starred": True},
            ],
        },
    )
    assert out == "a*;b;c*;"


# --- Malformed / unbalanced tags -----------------------------------------


def test_unclosed_if_raises() -> None:
    with pytest.raises(ValueError) as exc:
        render_template("{{#if X}}yes and nothing else", {"X": True})
    assert "unclosed" in str(exc.value).lower()
    assert "if" in str(exc.value)


def test_unclosed_each_raises() -> None:
    with pytest.raises(ValueError) as exc:
        render_template("{{#each L}}x", {"L": []})
    assert "unclosed" in str(exc.value).lower()


def test_dangling_close_tag_raises() -> None:
    with pytest.raises(ValueError) as exc:
        render_template("hi {{/if}}", {})
    assert "no open section" in str(exc.value)


def test_mismatched_close_raises() -> None:
    """`{{#if X}}...{{/each}}` is a malformed nest."""
    with pytest.raises(ValueError) as exc:
        render_template("{{#if X}}body{{/each}}", {"X": True})
    assert "mismatched" in str(exc.value).lower()


def test_unknown_section_verb_raises() -> None:
    with pytest.raises(ValueError) as exc:
        render_template("{{#foo BAR}}x{{/foo}}", {})
    assert "foo" in str(exc.value)


def test_missing_key_after_section_open_raises() -> None:
    with pytest.raises(ValueError) as exc:
        render_template("{{#if}}x{{/if}}", {})
    assert "missing key" in str(exc.value).lower()


def test_empty_tag_raises() -> None:
    with pytest.raises(ValueError) as exc:
        render_template("hi {{}}", {})
    assert "empty" in str(exc.value).lower()


def test_scalar_with_whitespace_raises() -> None:
    """`{{ FOO }}` is not accepted — spec §1 pins the no-whitespace form."""
    with pytest.raises(ValueError) as exc:
        render_template("{{ FOO }}", {"FOO": "x"})
    assert "whitespace" in str(exc.value).lower()


def test_else_outside_if_raises() -> None:
    with pytest.raises(ValueError):
        render_template("hi {{else}} bye", {})


# --- find_unrendered_markers ---------------------------------------------


def test_find_unrendered_markers_returns_leftovers_in_order() -> None:
    """Drift-detection helper preserves order + duplicates."""
    text = "before {{FOO}} middle {{BAR}} and again {{FOO}} end"
    assert find_unrendered_markers(text) == ["{{FOO}}", "{{BAR}}", "{{FOO}}"]


def test_find_unrendered_markers_empty_when_none_present() -> None:
    assert find_unrendered_markers("all clean, no markers") == []


def test_render_output_has_zero_unrendered_markers_on_clean_render() -> None:
    """Positive confirmation used by drift tests upstream."""
    out = render_template(
        "{{X}} {{#if Y}}{{Z}}{{/if}}",
        {"X": "1", "Y": True, "Z": "2"},
    )
    assert find_unrendered_markers(out) == []
