"""Integration tests for the msearch CLI."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

MSEARCH = str(Path(__file__).resolve().parent.parent / "src" / "msearch" / "msearch")
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "test_fixtures"


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    """Create a workspace with the expected directory layout using symlinks."""
    ws = tmp_path_factory.mktemp("workspace")
    memory_dir = ws / "memory" / "daily"
    memory_dir.mkdir(parents=True)
    # Symlink each fixture file into the workspace layout
    for f in (FIXTURES_DIR / "daily").glob("*.md"):
        os.symlink(f, memory_dir / f.name)
    reports_dir = ws / "reports"
    os.symlink(FIXTURES_DIR / "reports", reports_dir)
    return str(ws)


def run_msearch(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, MSEARCH] + list(args),
        capture_output=True, text=True, timeout=15,
    )


class TestKeywordMode:
    def test_keyword_meta(self, workspace):
        r = run_msearch("keyword", "meta", "--workspace", workspace)
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert data["mode"] == "keyword"
        assert len(data["tag_matches"]) > 0

    def test_keyword_nonexistent(self, workspace):
        r = run_msearch("keyword", "zzzznotreal", "--workspace", workspace)
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["tag_matches"] == []
        assert data["content_matches"] == []

    def test_keyword_screen_door(self, workspace):
        r = run_msearch("keyword", "screen door", "--workspace", workspace)
        assert r.returncode == 0
        data = json.loads(r.stdout)
        has_results = len(data["tag_matches"]) > 0 or len(data["content_matches"]) > 0
        assert has_results

    def test_keyword_examplecorp(self, workspace):
        r = run_msearch("keyword", "examplecorp", "--workspace", workspace)
        assert r.returncode == 0
        data = json.loads(r.stdout)
        has_results = len(data["tag_matches"]) > 0 or len(data["content_matches"]) > 0
        assert has_results


class TestTagsMode:
    def test_tags_list(self, workspace):
        r = run_msearch("tags", "--workspace", workspace)
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["mode"] == "tags"
        assert len(data["tags"]) > 0

    def test_tags_count(self, workspace):
        r = run_msearch("tags", "--count", "--workspace", workspace)
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert len(data["tags"]) > 0
        assert data["tags"][0]["count"] >= data["tags"][-1]["count"]


class TestGlobalFlags:
    def test_workspace_flag(self, workspace):
        r = run_msearch("tags", "--workspace", workspace)
        assert r.returncode == 0

    def test_timing_flag(self, workspace):
        r = run_msearch("keyword", "meta", "--workspace", workspace, "--timing")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert "timing" in data
        assert "index_ms" in data["timing"]
        assert "total_ms" in data["timing"]

    def test_no_cache_flag(self, workspace):
        r = run_msearch("keyword", "meta", "--workspace", workspace, "--no-cache")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert len(data["tag_matches"]) > 0

    def test_pretty_flag(self, workspace):
        r = run_msearch("keyword", "meta", "--workspace", workspace, "--pretty")
        assert r.returncode == 0
        assert "Search:" in r.stdout or "Tag Matches" in r.stdout

    def test_no_command_shows_help(self):
        r = run_msearch()
        assert r.returncode != 0
