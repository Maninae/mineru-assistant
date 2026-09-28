"""Tests for tag_extractor module."""

import tempfile
from pathlib import Path

from msearch.tag_extractor import extract_tags, has_frontmatter


class TestHasFrontmatter:
    def test_file_with_frontmatter(self, daily_dir):
        assert has_frontmatter(daily_dir / "2026-03-17.md") is True

    def test_file_without_frontmatter(self, tmp_path):
        f = tmp_path / "no_fm.md"
        f.write_text("# Just a heading\n\nSome content.\n")
        assert has_frontmatter(f) is False

    def test_nonexistent_file(self, tmp_path):
        assert has_frontmatter(tmp_path / "nope.md") is False


class TestExtractTags:
    def test_standard_yaml_frontmatter(self, daily_dir):
        tags = extract_tags(daily_dir / "2026-03-17.md")
        assert "meta" in tags
        assert "project deadline" in tags
        assert len(tags) > 5

    def test_no_frontmatter(self, tmp_path):
        f = tmp_path / "no_fm.md"
        f.write_text("# Title\n\nBody text.\n")
        assert extract_tags(f) == []

    def test_empty_tags_list(self, tmp_path):
        f = tmp_path / "empty_tags.md"
        f.write_text("---\ntitle: test\ntags:\n---\n\nContent.\n")
        assert extract_tags(f) == []

    def test_inline_array_tags(self, tmp_path):
        f = tmp_path / "inline.md"
        f.write_text("---\ntags: [alpha, beta, gamma]\n---\n\nContent.\n")
        tags = extract_tags(f)
        assert tags == ["alpha", "beta", "gamma"]

    def test_malformed_frontmatter_no_closing(self, tmp_path):
        f = tmp_path / "malformed.md"
        f.write_text("---\ntags:\n  - orphan\nSome content without closing.\n")
        assert extract_tags(f) == []

    def test_tags_with_special_chars(self, tmp_path):
        f = tmp_path / "special.md"
        f.write_text("---\ntags:\n  - c++\n  - .net framework\n  - AI/ML\n---\n\nContent.\n")
        tags = extract_tags(f)
        assert "c++" in tags
        assert ".net framework" in tags
        assert "AI/ML" in tags

    def test_tags_with_quotes(self, tmp_path):
        f = tmp_path / "quoted.md"
        f.write_text('---\ntags:\n  - "quoted tag"\n  - \'single quoted\'\n---\n\nContent.\n')
        tags = extract_tags(f)
        assert "quoted tag" in tags
        assert "single quoted" in tags

    def test_mixed_case_preserved(self, tmp_path):
        f = tmp_path / "case.md"
        f.write_text("---\ntags:\n  - CamelCase\n  - UPPERCASE\n  - lowercase\n---\n\n")
        tags = extract_tags(f)
        assert "CamelCase" in tags
        assert "UPPERCASE" in tags
        assert "lowercase" in tags

    def test_real_fixture_reports(self, reports_dir):
        tags = extract_tags(reports_dir / "screen-door-costco.md")
        assert "screen door" in tags
        assert "costco" in tags
