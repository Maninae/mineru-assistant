"""`Profile` dataclass — the foundation-required §5.3 profile.yaml schema.

Only the fields the foundation increment actually consumes are modeled as
first-class attributes; everything else stays on `extras` so a profile
file can carry future increment fields without breaking the loader
(forward-compat contract from F3's done-criteria).

Foundation fields (from §5.3, plus the two `secrets.*` knobs F2 consumes):

  - `name`                        matches the profile directory name.
  - `display_name`                human-facing display of the operator.
  - `assistant_name`              name of the assistant persona (e.g. the
                                  seed profile's `Mineru`; user-customizable
                                  per profile — surfaces in `mineru --help`).
  - `timezone`                    IANA zone (e.g. `America/Los_Angeles`).
  - `keychain_account`            macOS Keychain account namespace
                                  (F2's KeychainBackend reads this).
  - `launchd_label_prefix`        engine-level plist label prefix
                                  (e.g. `com.mineru`); NOT a per-user
                                  rotation knob per §0.
  - `workspace_absolute`          resolved absolute path to the workspace
                                  root the profile points at.
  - `memory_root`                 resolved absolute path to the memory
                                  tree the profile owns.
  - `briefs_root`                 resolved absolute path to the briefs
                                  output directory.
  - `journal_apple_notes_folder`  Apple Notes folder name used by the
                                  journal-export job (per-user content).
  - `secrets_backends`            backend chain from
                                  `secrets.backends` in profile.yaml.
  - `secrets_env_prefix`          env prefix from `secrets.env_prefix`.

Everything else in `profile.yaml` (e.g. `charter:`, `connectors:`, later
increment fields) lands on `extras` verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Profile:
    """Immutable snapshot of the active profile the CLI is running under.

    Frozen so a resolver / verb can hold a reference without worrying that
    the profile has drifted mid-invocation. To pick up a changed
    `profile.yaml`, load a fresh `Profile`.

    `extras` intentionally accepts arbitrary keys — the loader forwards
    every non-foundation top-level key here so future increments can add
    fields without the F3 loader rejecting them.
    """

    # --- Identity ------------------------------------------------------
    name: str
    display_name: str
    assistant_name: str
    timezone: str

    # --- System namespacing -------------------------------------------
    keychain_account: str
    launchd_label_prefix: str

    # --- Filesystem roots (absolute) -----------------------------------
    workspace_absolute: Path
    memory_root: Path
    briefs_root: Path

    # --- Per-user content pointers ------------------------------------
    journal_apple_notes_folder: str

    # --- Secrets seam knobs (F2's SecretsResolver consumes these) ------
    secrets_backends: List[str]
    secrets_env_prefix: str

    # --- Per-profile connector isolation ------------------------------
    # `google_account` is the Google Workspace email this profile owns.
    # When set, every `gog` shellout is prefixed with `--account=<email>`
    # so a two-profile machine addresses two Google accounts cleanly
    # (see mineru_cli/wrappers/gog_firewall.py::run_gog_firewall).
    # `None` on legacy profiles that never went through the `gog auth add`
    # walkthrough during onboarding; the wrapper skips the injection.
    google_account: Optional[str]

    # `imessage_enabled` gates every `mineru imessage` verb per profile.
    # iMessage reads the ONE shared `~/Library/Messages/chat.db` (one
    # Apple ID per macOS user), so a second profile invoking any imessage
    # verb would silently read the primary user's messages — a privacy
    # leak. Default `True` so the seed / owner profile keeps working;
    # `mineru profile init` sets it to `False` for newly-created NON-owner
    # profiles.
    imessage_enabled: bool

    # --- Provenance + forward-compat ----------------------------------
    profile_root: Path  # directory that holds this profile.yaml
    profile_yaml_path: Path
    extras: Dict[str, Any] = field(default_factory=dict)
