"""Pure implementations behind the `mineru memory` maintenance verbs.

The verb file (`mineru_cli/verbs/memory.py`) is a thin Typer router; the
actual work of building a warm-resume bundle, walking the annotated
memory tree, snapshotting the tree to a tarball, and staging a day's
raw fragments for later distillation lives here so each piece can be
unit-tested with a tempdir root and no CLI plumbing.

Every entry point takes an explicit `memory_root: Path` (or `Profile`
adapters do the derivation once). No module in this package references
`~/.mineru` or any operator-specific path: the engine is a generic
public framework, so the target roots come exclusively from the active
profile's `memory_root` / `workspace_absolute` fields.
"""

from mineru_cli.memory_ops.backup import (
    DEFAULT_BACKUP_SUBDIR,
    BackupResult,
    create_backup,
)
from mineru_cli.memory_ops.consolidate import (
    ConsolidateResult,
    consolidate_daily_fragments,
    find_dates_missing_consolidation,
)
from mineru_cli.memory_ops.distiller_claude import (
    DistillerError,
    default_claude_cli_distiller,
)
from mineru_cli.memory_ops.tree import build_memory_tree
from mineru_cli.memory_ops.warm_resume import build_warm_resume

__all__ = [
    "BackupResult",
    "ConsolidateResult",
    "DEFAULT_BACKUP_SUBDIR",
    "DistillerError",
    "build_memory_tree",
    "build_warm_resume",
    "consolidate_daily_fragments",
    "create_backup",
    "default_claude_cli_distiller",
    "find_dates_missing_consolidation",
]
