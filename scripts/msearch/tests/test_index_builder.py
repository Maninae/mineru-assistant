"""Tests for index_builder module."""

import json
import time
from pathlib import Path

from msearch.index_builder import Index, build_index, load_or_build


class TestBuildIndex:
    def test_builds_from_fixtures(self, all_dirs):
        index = build_index(all_dirs)
        assert len(index.tag_to_files) > 0
        assert len(index.file_to_tags) > 0
        assert len(index.all_tags) > 0
        assert index.built_at > 0

    def test_tag_to_files_contains_meta(self, all_dirs):
        index = build_index(all_dirs)
        assert "meta" in index.tag_to_files
        assert "2026-03-17.md" in index.tag_to_files["meta"]

    def test_file_to_tags_preserves_case(self, all_dirs):
        index = build_index(all_dirs)
        tags = index.file_to_tags.get("2026-03-17.md", [])
        # Original case should be preserved
        assert any(t == "meta" for t in tags)

    def test_all_tags_sorted_and_deduplicated(self, all_dirs):
        index = build_index(all_dirs)
        assert index.all_tags == sorted(set(index.all_tags))

    def test_nonexistent_dir_skipped(self, tmp_path):
        index = build_index([tmp_path / "nonexistent"])
        assert len(index.tag_to_files) == 0

    def test_tag_normalization_lowercase(self, all_dirs):
        index = build_index(all_dirs)
        for tag in index.tag_to_files:
            assert tag == tag.lower()


class TestLoadOrBuild:
    def test_creates_cache_file(self, all_dirs, tmp_path):
        cache = tmp_path / "cache.json"
        index = load_or_build(cache, all_dirs)
        assert cache.exists()
        data = json.loads(cache.read_text())
        assert "tag_to_files" in data
        assert "built_at" in data

    def test_loads_fresh_cache(self, all_dirs, tmp_path):
        cache = tmp_path / "cache.json"
        index1 = load_or_build(cache, all_dirs)
        t1 = index1.built_at
        # Load again — should use cache (same built_at)
        index2 = load_or_build(cache, all_dirs)
        assert index2.built_at == t1

    def test_rebuilds_stale_cache(self, all_dirs, tmp_path):
        cache = tmp_path / "cache.json"
        # Create a cache with old timestamp
        old_index = build_index(all_dirs)
        old_index.built_at = time.time() - 600  # 10 minutes ago
        data = {
            "tag_to_files": old_index.tag_to_files,
            "file_to_tags": old_index.file_to_tags,
            "all_tags": old_index.all_tags,
            "built_at": old_index.built_at,
        }
        cache.write_text(json.dumps(data))

        index = load_or_build(cache, all_dirs, max_age=300)
        assert index.built_at > old_index.built_at

    def test_handles_corrupt_cache(self, all_dirs, tmp_path):
        cache = tmp_path / "cache.json"
        cache.write_text("not valid json{{{")
        index = load_or_build(cache, all_dirs)
        assert len(index.tag_to_files) > 0
