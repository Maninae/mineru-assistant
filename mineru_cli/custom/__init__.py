"""User-defined custom-verb registry for the mineru CLI (P3-07).

Public surface:
  - `CustomVerbEntry`             — immutable dataclass modeling one entry.
  - `CustomVerbRegistry`          — owns `<profile_root>/custom_verbs.yaml`.
  - `CustomVerbError`             — raised on invalid entry / collision / IO.
  - `NAME_PATTERN`                — the `^[a-z][a-z0-9-]*$` name regex.
  - `CUSTOM_VERBS_FILENAME`       — the on-disk filename constant.

The registry is the source of truth for `mineru custom {add,list,remove,show}`
and for the dynamic-dispatch layer in `mineru_cli.app.root`. Missing
custom_verbs.yaml is treated as an empty registry (fail-open on discovery,
so a fresh install never crashes at import time) but any write of a
malformed entry fails loud (§0 fail-loud policy).

See `mineru_cli.custom.registry` for the implementation and
`mineru_cli.verbs.custom` for the Typer wire-up.
"""

from mineru_cli.custom.registry import (
    CUSTOM_VERBS_FILENAME,
    CustomVerbEntry,
    CustomVerbError,
    CustomVerbRegistry,
    NAME_PATTERN,
    builtin_verb_names,
    registry_path_for_profile,
)

__all__ = [
    "CUSTOM_VERBS_FILENAME",
    "CustomVerbEntry",
    "CustomVerbError",
    "CustomVerbRegistry",
    "NAME_PATTERN",
    "builtin_verb_names",
    "registry_path_for_profile",
]
