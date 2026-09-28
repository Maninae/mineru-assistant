"""Render-context assembly for hydration.

Builds the flat `{{VAR}}` context that `render_template` consumes,
sourced from a `Profile` + an optional connectors dict + the process
environment. Per templating spec §2 there are five variable groups
(persona identity, user identity, filesystem roots, system
namespacing, connectors); each is filled from the correct source and
optional entries are simply omitted so `{{#if}}` blocks that guard
them cleanly render empty.

Secret VALUES never enter the context. Secrets appear here by NAME
only (Keychain service names, env prefixes).
"""

from __future__ import annotations

from typing import Any, Dict, Optional


# Extras keys that map straight into the context under a different
# (uppercase / spec-shaped) name. Kept as tables so a spec update is a
# one-line diff rather than an if/elif ladder.
_PERSONA_EXTRAS_MAP = (
    ("persona_emoji", "PERSONA_EMOJI"),
    ("persona_origin", "PERSONA_ORIGIN"),
    ("persona_kind", "PERSONA_KIND"),
)

_USER_EXTRAS_MAP = (
    ("user_full_name", "USER_FULL_NAME"),
    ("user_pronouns", "USER_PRONOUNS"),
    ("user_location", "USER_LOCATION"),
)

# When the possessive slot of `user_pronouns` is a possessive-PRONOUN form
# ("theirs", "hers"), normalize to the possessive-DETERMINER form ("their",
# "her") — templates use the possessive before a noun ("their behalf"),
# which is the determiner. `his` is invariant.
_POSSESSIVE_PRONOUN_TO_DETERMINER = {
    "hers": "her",
    "theirs": "their",
    "yours": "your",
    "ours": "our",
}

# Subject pronouns that take PLURAL verb agreement ("they are", "they want").
# Anything else is treated as singular ("he is", "she wants"). Custom
# neopronouns default to singular, matching how "xe/ze" sets are documented.
_PLURAL_SUBJECT_PRONOUNS = frozenset({"they"})

# When a `user_pronouns` value omits the possessive-determiner slot (the natural
# short form "he/him", "she/her", "they/them"), derive it from the subject rather
# than repeating the subject word — otherwise `{{USER_POSSESSIVE}} behalf` renders
# as "he behalf". Unknown neopronoun subjects still fall back to the subject word.
_SUBJECT_TO_POSSESSIVE_DETERMINER = {
    "he": "his",
    "she": "her",
    "they": "their",
}

_FILESYSTEM_EXTRAS_MAP = (
    ("user_dev_root", "USER_DEV_ROOT"),
    ("user_claude_home", "USER_CLAUDE_HOME"),
    ("heavy_storage_root", "HEAVY_STORAGE_ROOT"),
)

_HOUSEHOLD_EXTRAS_MAP = (
    ("household", "USER_HOUSEHOLD"),
    ("key_people", "USER_KEY_PEOPLE"),
)


def build_render_context(
    profile: Any,
    connectors: Optional[Dict[str, Any]],
    env: Dict[str, str],
) -> Dict[str, Any]:
    """Assemble the `{{VAR}}` context for a hydration render.

    Draws from three sources per templating spec §2:

      * `Profile` fields — persona identity, user identity, filesystem
        roots, launchd namespacing, secrets seam knobs.
      * `connectors` dict (optional; may be `None` or an empty dict
        when the private overlay is not yet wired). Whatever keys are
        present flow through verbatim; missing keys become undefined
        and any `{{#if}}` guarding them cleanly omits its block.
      * `env` — process environment. Only `HOME` is consumed (surfaced
        as `USER_HOME`). Never leak secret VALUES.

    Also derives `PERSONA_NAME_LOWER` from `assistant_name` (spec §5).

    Args:
        profile: a `Profile` (from `mineru_cli.profile.schema`). Field
            access is duck-typed so tests can pass a stand-in shape.
        connectors: optional connectors-yaml payload. May be `None`.
        env: process environment view. `HOME` becomes `USER_HOME`
            when present; absent `HOME` leaves `USER_HOME` unset.

    Returns:
        A flat `dict` suitable for `render_template`.
    """
    context: Dict[str, Any] = {}
    extras: Dict[str, Any] = getattr(profile, "extras", {}) or {}

    _fill_persona(context, profile, extras)
    _fill_user_identity(context, profile, extras)
    _fill_filesystem(context, profile, extras, env)
    _fill_namespacing(context, profile, extras)
    _fill_household(context, extras)
    _fill_connectors(context, connectors)

    return context


def _fill_persona(
    context: Dict[str, Any], profile: Any, extras: Dict[str, Any]
) -> None:
    """Persona identity (spec §2.1) + derived `PERSONA_NAME_LOWER`."""
    context["PERSONA_NAME"] = profile.assistant_name
    context["PERSONA_NAME_LOWER"] = str(profile.assistant_name).lower()
    for extras_key, ctx_key in _PERSONA_EXTRAS_MAP:
        if extras_key in extras:
            context[ctx_key] = extras[extras_key]


def _fill_user_identity(
    context: Dict[str, Any], profile: Any, extras: Dict[str, Any]
) -> None:
    """User identity (spec §2.2).

    Also derives pronoun forms from `USER_PRONOUNS` so templates render
    correctly across he/him, she/her, they/them, and custom sets without
    hard-coding one set. Derived keys (lowercase + capitalized shapes):
      - `USER_PRONOUN_SUBJECT` / `USER_PRONOUN_SUBJECT_CAP`  (he/she/they)
      - `USER_PRONOUN_OBJECT`  / `USER_PRONOUN_OBJECT_CAP`   (him/her/them)
      - `USER_POSSESSIVE`      / `USER_POSSESSIVE_CAP`       (his/her/their)

    Input is `user_pronouns` in the standard "/"-separated form
    (subject/object/possessive). Missing slots fall back to the subject
    form; a possessive-pronoun form like "theirs" is normalized to the
    determiner form "their" per `_POSSESSIVE_PRONOUN_TO_DETERMINER`.
    """
    context["USER_NAME"] = profile.display_name
    context["USER_TIMEZONE"] = profile.timezone
    for extras_key, ctx_key in _USER_EXTRAS_MAP:
        if extras_key in extras:
            context[ctx_key] = extras[extras_key]

    pronouns_raw = extras.get("user_pronouns")
    if isinstance(pronouns_raw, str) and pronouns_raw.strip():
        derived = _derive_pronoun_forms(pronouns_raw)
        context.update(derived)


def _derive_pronoun_forms(pronouns_raw: str) -> Dict[str, str]:
    """Parse `user_pronouns` into subject/object/possessive-determiner forms.

    Emits:
      - Subject / object / possessive-determiner (lowercase + capitalized).
      - `USER_IS` / `USER_S`: verb-agreement helpers for the copula.
        `he` / `she` → `is` / `'s`, `they` → `are` / `'re`. Templates use
        `{{USER_PRONOUN_SUBJECT}} {{USER_IS}}` for expanded form and
        `{{USER_PRONOUN_SUBJECT_CAP}}{{USER_S}}` for the contraction.
      - `USER_VERB_S`: third-person-singular verb suffix ("s" for he/she,
        "" for they). Lets `{{USER_PRONOUN_SUBJECT}} give{{USER_VERB_S}}`
        render as "he gives" / "they give".

    Also normalizes a possessive-pronoun input like "theirs" to the
    determiner "their" so `{{USER_POSSESSIVE}} behalf` reads cleanly
    regardless of which form the operator wrote.
    """
    parts = [p.strip() for p in pronouns_raw.split("/") if p.strip()]
    if not parts:
        return {}
    subject = parts[0]
    obj = parts[1] if len(parts) > 1 else subject
    poss_raw = (
        parts[2] if len(parts) > 2
        else _SUBJECT_TO_POSSESSIVE_DETERMINER.get(subject.lower(), subject)
    )
    poss = _POSSESSIVE_PRONOUN_TO_DETERMINER.get(poss_raw.lower(), poss_raw)
    is_plural = subject.lower() in _PLURAL_SUBJECT_PRONOUNS
    return {
        "USER_PRONOUN_SUBJECT": subject,
        "USER_PRONOUN_SUBJECT_CAP": subject[:1].upper() + subject[1:],
        "USER_PRONOUN_OBJECT": obj,
        "USER_PRONOUN_OBJECT_CAP": obj[:1].upper() + obj[1:],
        "USER_POSSESSIVE": poss,
        "USER_POSSESSIVE_CAP": poss[:1].upper() + poss[1:],
        "USER_IS": "are" if is_plural else "is",
        "USER_S": "'re" if is_plural else "'s",
        "USER_VERB_S": "" if is_plural else "s",
    }


def _fill_filesystem(
    context: Dict[str, Any],
    profile: Any,
    extras: Dict[str, Any],
    env: Dict[str, str],
) -> None:
    """Filesystem roots (spec §2.3).

    `Path` fields render as strings so templates can concatenate them
    (`{{MINERU_HOME}}/bin/gog-firewall`) without an str() call.

    Also emits `MINERU_INJECT_QUEUE_DIR` — the daemon-inject queue path
    — as a first-class context key, derived from `workspace_absolute`.
    Step-5 audit, Findings 7 + 9: every hydrated launchd plist template
    now stamps this into its `EnvironmentVariables` block so a
    per-profile cron / daemon writes into ITS OWN queue, not the shared
    root's queue.
    """
    workspace = str(profile.workspace_absolute).rstrip("/")
    context["MINERU_HOME"] = str(profile.workspace_absolute)
    context["MEMORY_ROOT"] = str(profile.memory_root)
    context["BRIEFS_ROOT"] = str(profile.briefs_root)
    context["MINERU_INJECT_QUEUE_DIR"] = f"{workspace}/cache/inject-queue"
    home = env.get("HOME")
    if home:
        context["USER_HOME"] = home
    for extras_key, ctx_key in _FILESYSTEM_EXTRAS_MAP:
        if extras_key in extras:
            context[ctx_key] = extras[extras_key]


def _fill_namespacing(
    context: Dict[str, Any], profile: Any, extras: Dict[str, Any]
) -> None:
    """System namespacing (spec §2.4). Secret NAMES only, never values.

    `WEBAPP_PORT` lands here too: profile.extras `webapp_port` overrides
    the default 5195 so a second profile on the same machine can bind
    a different port instead of colliding on 5195. Step-5 audit,
    Finding 8 (webapp templated port).
    """
    context["LAUNCHD_PREFIX"] = profile.launchd_label_prefix
    context["KEYCHAIN_ACCOUNT"] = profile.keychain_account
    context["SECRETS_ENV_PREFIX"] = profile.secrets_env_prefix
    if "app_keychain_service" in extras:
        context["APP_KEYCHAIN_SERVICE"] = extras["app_keychain_service"]
    webapp_port = extras.get("webapp_port", 5195)
    context["WEBAPP_PORT"] = str(webapp_port)


def _fill_household(context: Dict[str, Any], extras: Dict[str, Any]) -> None:
    """Household / relationships (spec §2.6) — list-shaped, optional."""
    for extras_key, ctx_key in _HOUSEHOLD_EXTRAS_MAP:
        if extras_key in extras:
            context[ctx_key] = extras[extras_key]


def _fill_connectors(
    context: Dict[str, Any], connectors: Optional[Dict[str, Any]]
) -> None:
    """Connectors (spec §2.5) — copy each entry verbatim. `None` skipped."""
    if not connectors:
        return
    for key, value in connectors.items():
        # Skip `None` — templates treat it as absent, and `{{#if
        # OPTIONAL_ID}}` cleanly omits when the connector isn't wired.
        if value is None:
            continue
        context[key] = value
