"""msearch rewrites `memory/tags-index.json` in place instead of needing it deleted first.

The nightly consolidation used to `trash` the cache to force a rebuild, which piled a
copy into the Trash every night. `msearch tags --no-cache` now rebuilds AND rewrites
the cache atomically (temp file + rename), so nothing is ever deleted.
"""

import json
import subprocess
import sys
from pathlib import Path

MSEARCH_PATH = Path(__file__).resolve().parent.parent / "scripts" / "msearch" / "msearch"


def test_no_cache_rebuild_overwrites_stale_cache_in_place(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (tmp_path / "reports").mkdir()
    (memory_dir / "note.md").write_text("---\ntags: [kelp]\n---\n\nBody.\n")
    cache_path = memory_dir / "tags-index.json"
    cache_path.write_text(json.dumps({"tag_to_files": {}, "file_to_tags": {}, "all_tags": [], "built_at": 0}))

    completed = subprocess.run(
        [sys.executable, str(MSEARCH_PATH), "tags", "--workspace", str(tmp_path), "--no-cache"],
        capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    rewritten_cache = json.loads(cache_path.read_text())
    assert rewritten_cache["built_at"] > 0
    assert "kelp" in rewritten_cache["all_tags"]
    # Atomic write leaves no temp file next to the cache.
    assert sorted(p.name for p in memory_dir.iterdir()) == ["note.md", "tags-index.json"]
