"""The compact-dump exclude list is operator config, not engine code."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "concat-memory.py"


def _load(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MINERU_HOME", str(tmp_path))
    monkeypatch.delenv("MINERU_COMPACT_EXCLUDES_FILE", raising=False)
    spec = importlib.util.spec_from_file_location("concat_memory_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shipped_engine_has_no_compact_excludes(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path)
    assert module.COMPACT_EXCLUDE_PATTERNS == []


def test_operator_file_feeds_compact_excludes(monkeypatch, tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "compact-excludes.txt").write_text("# comment\n\n^memory/private/.*\n^memory/travel/.*\n")
    module = _load(monkeypatch, tmp_path)
    assert module.COMPACT_EXCLUDE_PATTERNS == ["^memory/private/.*", "^memory/travel/.*"]
    assert module.should_exclude("memory/travel/japan.md", compact=True)
    assert not module.should_exclude("memory/travel/japan.md", compact=False)
