"""Mustache-SUBSET renderer for hydration templates.

Hand-rolled, zero-dependency (`pystache` is not installed here). Covers
the exact grammar templating spec §1 promises:

  * `{{SCALAR}}` — `str(context[key])`; unknown key raises `ValueError`
    (fail-loud drift guard).
  * `{{#if KEY}} ... {{else}} ... {{/if}}` — block/else branches;
    missing key is falsey.
  * `{{#each LIST}} ... {{/each}}` — iterate a list. Inside the block,
    keys resolve against the current item first, then fall through to
    the outer context. `{{.}}` names the current scalar item.

Sections nest freely. Malformed / unbalanced tags raise `ValueError`
with a message naming the offending tag.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple


# Match `{{...}}` non-greedy; interior text may not contain `{` or `}`.
# This deliberately excludes triple-braces and mid-tag braces; those
# would signal a malformed template and we prefer to surface them as
# unrendered markers rather than accidentally chomping something ugly.
_TAG_RE = re.compile(r"\{\{([^{}]*?)\}\}")

# The two section verbs we support. Anything else after `#` is a
# malformed section open.
_OPEN_IF = "if"
_OPEN_EACH = "each"


def find_unrendered_markers(text: str) -> List[str]:
    """Return every leftover `{{...}}` marker (for drift detection).

    Callers use this after a render pass to assert zero markers
    remained. The returned list preserves order and duplicates.
    """
    return [match.group(0) for match in _TAG_RE.finditer(text)]


def render_template(template_text: str, context: Dict[str, Any]) -> str:
    """Render `template_text` against `context`.

    Args:
        template_text: raw template with `{{VAR}}` / `{{#if}}` / `{{#each}}`.
        context: top-level scope. Missing scalar keys raise `ValueError`
            naming the offending variable.

    Raises:
        ValueError: unknown scalar key, malformed/unbalanced section tag,
            or non-list operand for `{{#each}}`.
    """
    tokens = _tokenize(template_text)
    nodes, next_index, hit_else = _parse_block(tokens, 0, end_key=None)
    if hit_else:
        # Never reachable: `end_key=None` never returns hit_else True.
        raise ValueError("internal: stray {{else}} at top level")
    if next_index != len(tokens):
        raise ValueError(
            f"template parse error: unexpected trailing tokens at index {next_index}"
        )
    return _render_nodes(nodes, [context])


# --- Tokenization --------------------------------------------------------


def _tokenize(text: str) -> List[Tuple[str, Any]]:
    """Split `text` into a flat list of (kind, payload) tokens.

    Kinds:
      ('text', str)                 — literal chunk between tags
      ('scalar', key)               — `{{key}}`
      ('open', ('if'|'each', key))  — `{{#if key}}` / `{{#each key}}`
      ('close', key)                — `{{/key}}`
      ('else', None)                — `{{else}}`
    """
    tokens: List[Tuple[str, Any]] = []
    pos = 0
    for match in _TAG_RE.finditer(text):
        if match.start() > pos:
            tokens.append(("text", text[pos:match.start()]))
        raw_unstripped = match.group(1)
        raw_inner = raw_unstripped.strip()
        raw_full = match.group(0)
        if not raw_inner:
            raise ValueError(f"malformed tag {raw_full!r}: empty content")
        if raw_inner.startswith("#"):
            body = raw_inner[1:].strip()
            parts = body.split(None, 1)
            verb = parts[0] if parts else ""
            if verb not in (_OPEN_IF, _OPEN_EACH):
                raise ValueError(
                    f"malformed section tag {raw_full!r}: unknown verb "
                    f"{verb!r} (supported: '#if', '#each')"
                )
            if len(parts) < 2 or not parts[1].strip():
                raise ValueError(
                    f"malformed section tag {raw_full!r}: missing key "
                    f"after '#{verb}'"
                )
            tokens.append(("open", (verb, parts[1].strip())))
        elif raw_inner.startswith("/"):
            key = raw_inner[1:].strip()
            if not key:
                raise ValueError(
                    f"malformed close tag {raw_full!r}: missing key"
                )
            tokens.append(("close", key))
        elif raw_inner == "else":
            tokens.append(("else", None))
        else:
            # Scalar. Whitespace inside the tag is a defect (e.g. a
            # stray `{{ FOO }}` — templating spec §1 pins the strict
            # no-interior-whitespace form, contrasting explicitly with
            # the Jinja2 `{{ VAR }}` shape). Check the RAW (pre-strip)
            # tag content so a leading/trailing space also raises.
            if any(ch.isspace() for ch in raw_unstripped):
                raise ValueError(
                    f"malformed scalar tag {raw_full!r}: unexpected whitespace"
                )
            tokens.append(("scalar", raw_inner))
        pos = match.end()
    if pos < len(text):
        tokens.append(("text", text[pos:]))
    return tokens


# --- Parsing (block-oriented recursive descent) ---------------------------


def _parse_block(
    tokens: List[Tuple[str, Any]],
    start: int,
    end_key: Optional[str],
) -> Tuple[List[Any], int, bool]:
    """Parse tokens until we hit a matching close tag (or `{{else}}`).

    Node shapes emitted:
      ('text', str)
      ('scalar', key)
      ('if', key, then_nodes, else_nodes)
      ('each', key, body_nodes)

    Returns `(nodes, next_index, stopped_on_else)`. `stopped_on_else`
    is True iff we returned because we hit `{{else}}` (the caller then
    parses the else branch with the same `end_key`).
    """
    nodes: List[Any] = []
    i = start
    while i < len(tokens):
        kind, payload = tokens[i]
        if kind == "text":
            nodes.append(("text", payload))
            i += 1
        elif kind == "scalar":
            nodes.append(("scalar", payload))
            i += 1
        elif kind == "open":
            verb, key = payload
            # Close tag matches the VERB name (`{{/if}}` / `{{/each}}`),
            # not the section key — matches the syntax in templating
            # spec §1 (`{{#if KEY}}...{{/if}}`).
            if verb == _OPEN_IF:
                then_nodes, next_i, hit_else = _parse_block(
                    tokens, i + 1, end_key=verb
                )
                if hit_else:
                    else_nodes, next_i, hit_else2 = _parse_block(
                        tokens, next_i, end_key=verb
                    )
                    if hit_else2:
                        raise ValueError(
                            f"multiple {{{{else}}}} inside "
                            f"{{{{#if {key}}}}}"
                        )
                else:
                    else_nodes = []
                nodes.append(("if", key, then_nodes, else_nodes))
                i = next_i
            elif verb == _OPEN_EACH:
                body_nodes, next_i, hit_else = _parse_block(
                    tokens, i + 1, end_key=verb
                )
                if hit_else:
                    raise ValueError(
                        f"{{{{else}}}} is only valid inside {{{{#if}}}}, "
                        f"not inside {{{{#each {key}}}}}"
                    )
                nodes.append(("each", key, body_nodes))
                i = next_i
            else:  # pragma: no cover — _tokenize already rejects
                raise ValueError(
                    f"internal: unknown section verb {verb!r}"
                )
        elif kind == "close":
            if end_key is None:
                raise ValueError(
                    f"unexpected close tag '{{{{/{payload}}}}}' with no "
                    "open section"
                )
            if payload != end_key:
                raise ValueError(
                    f"mismatched section close: expected "
                    f"'{{{{/{end_key}}}}}', got '{{{{/{payload}}}}}'"
                )
            return nodes, i + 1, False
        elif kind == "else":
            if end_key is None:
                raise ValueError(
                    "unexpected {{else}} outside of {{#if}}"
                )
            return nodes, i + 1, True
        else:  # pragma: no cover — _tokenize never yields other kinds
            raise ValueError(f"internal: unknown token kind {kind!r}")
    if end_key is not None:
        raise ValueError(
            f"unclosed section: missing '{{{{/{end_key}}}}}'"
        )
    return nodes, i, False


# --- Rendering -----------------------------------------------------------


def _render_nodes(nodes: List[Any], scope_stack: List[Dict[str, Any]]) -> str:
    """Render a parsed node list against a stack of scopes (innermost first).

    `scope_stack[0]` is checked first for every name lookup; missing
    names fall through to the outer scopes, then raise on scalar
    resolution (raising is the fail-loud drift guard).
    """
    out: List[str] = []
    for node in nodes:
        kind = node[0]
        if kind == "text":
            out.append(node[1])
        elif kind == "scalar":
            out.append(_resolve_scalar(node[1], scope_stack))
        elif kind == "if":
            _, key, then_nodes, else_nodes = node
            value = _lookup_or_falsey(key, scope_stack)
            branch = then_nodes if _is_truthy(value) else else_nodes
            out.append(_render_nodes(branch, scope_stack))
        elif kind == "each":
            _, key, body_nodes = node
            items = _lookup_or_falsey(key, scope_stack)
            if items is None or items is False or items == []:
                continue
            if not isinstance(items, list):
                raise ValueError(
                    f"{{{{#each {key}}}}} expected a list, got "
                    f"{type(items).__name__}"
                )
            for item in items:
                if isinstance(item, dict):
                    child_scope = item
                else:
                    # Scalar item: {{.}} resolves to the item itself.
                    child_scope = {".": item}
                out.append(
                    _render_nodes(body_nodes, [child_scope] + scope_stack)
                )
        else:  # pragma: no cover
            raise ValueError(f"internal: unknown node kind {kind!r}")
    return "".join(out)


def _resolve_scalar(key: str, scope_stack: List[Dict[str, Any]]) -> str:
    """Look `key` up the scope stack. Fail loud on miss."""
    for scope in scope_stack:
        if key in scope:
            return str(scope[key])
    raise ValueError(
        f"unknown variable '{{{{{key}}}}}' — not in render context"
    )


def _lookup_or_falsey(key: str, scope_stack: List[Dict[str, Any]]) -> Any:
    """Look `key` up the scope stack; return `None` if absent.

    Used by section tags where "missing key = falsey = omit".
    """
    for scope in scope_stack:
        if key in scope:
            return scope[key]
    return None


def _is_truthy(value: Any) -> bool:
    """Mustache-shaped truthiness: empty containers / 0 / None / False = falsey."""
    return bool(value)
