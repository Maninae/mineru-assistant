"""Machine-level human registry (Phase 1 multi-profile framework).

A registry file at the workspace root holds a thin, stateless map from
human handle -> Telegram identity. Humans DO NOT have per-user
directories, memories, cron jobs, or any other per-user state — those
belong to agent profiles. This registry is only "who might message the
machine" — the identifying data an agent's access allowlist keys off.

Canonical filename (2026-09-16 audit §2A F3): `people.yaml`. Pre-rename
workspaces still keep a `humans.yaml`; the loader falls back to it when
`people.yaml` is absent. Writers always write the canonical name and
refresh a pre-existing legacy file (mirror the `active`/`current`
symlink pattern from commit `c318df6`) so downstream readers can never
see a stale answer. Fresh workspaces get only `people.yaml` — no
`humans.yaml` planted.

The internal Python package name (`mineru_cli.humans`) intentionally
keeps the pre-rename spelling for now — internal name; users never see
it. Audit F8 sequences the module rename as a follow-up.

Schema (see `schema.py::Human` for the dataclass form; unchanged from
the pre-2026-09-16 shape):

    # people.yaml at <workspace_root>/people.yaml (or legacy humans.yaml)
    humans:
      sam:
        telegram_id: 123456789
        display_name: "Sam Rivera"
      mira:
        telegram_id: 987654321
        display_name: "Mira Rivera"

The YAML top-level `humans:` key stays the same across the rename —
the FILE moved, the PAYLOAD did not. Pre-rename consumers that parsed
`--json` output on the top-level `humans` key keep working.

Public surface:
  - `Human`                        — immutable dataclass for one entry.
  - `HumansRegistry`               — collection with lookup helpers.
  - `HumansError`                  — fail-loud loader error.
  - `load_humans_registry(...)`    — parse the registry at the workspace
                                     root (or an override path); prefers
                                     `people.yaml`, falls back to
                                     `humans.yaml`.
  - `default_people_yaml_path(...)` — CANONICAL write-target path.
  - `default_humans_yaml_path(...)` — LEGACY alias; returns the
                                      canonical people.yaml path so
                                      pre-rename imports keep working.
  - `legacy_humans_yaml_path(...)`  — explicit path helper for the
                                      pre-rename humans.yaml (used by
                                      writers that need to refresh a
                                      pre-existing legacy file).
  - `resolve_registry_yaml_path(...)` — READ path picker (people.yaml
                                        first, humans.yaml fallback,
                                        canonical default for the
                                        "nothing on disk" case).
"""

from mineru_cli.humans.loader import (
    HumansError,
    HUMANS_YAML_FILENAME,
    PEOPLE_YAML_FILENAME,
    default_humans_yaml_path,
    default_people_yaml_path,
    legacy_humans_yaml_path,
    load_humans_registry,
    resolve_registry_yaml_path,
)
from mineru_cli.humans.schema import Human, HumansRegistry

__all__ = [
    "HUMANS_YAML_FILENAME",
    "Human",
    "HumansError",
    "HumansRegistry",
    "PEOPLE_YAML_FILENAME",
    "default_humans_yaml_path",
    "default_people_yaml_path",
    "legacy_humans_yaml_path",
    "load_humans_registry",
    "resolve_registry_yaml_path",
]
