"""Tests for keyword_searcher module."""

from msearch.index_builder import build_index
from msearch.keyword_searcher import search, search_content, search_tags


class TestSearchTags:
    def test_exact_tag_match(self, all_dirs):
        index = build_index(all_dirs)
        results = search_tags("meta", index)
        files = [r["file"] for r in results]
        assert "2026-03-17.md" in files

    def test_substring_tag_match(self, all_dirs):
        index = build_index(all_dirs)
        results = search_tags("project", index)
        matched_tags = set()
        for r in results:
            matched_tags.update(r["matched_tags"])
        assert "project deadline" in matched_tags
        assert "project timeline" in matched_tags

    def test_case_insensitive(self, all_dirs):
        index = build_index(all_dirs)
        results_lower = search_tags("meta", index)
        results_upper = search_tags("META", index)
        assert results_lower == results_upper

    def test_no_results(self, all_dirs):
        index = build_index(all_dirs)
        results = search_tags("zzzznonexistentzzzz", index)
        assert results == []


class TestSearchContent:
    def test_finds_text_in_files(self, all_dirs):
        results = search_content("ExampleCorp", all_dirs)
        files = [r["file"] for r in results]
        assert "2026-03-17.md" in files

    def test_returns_line_numbers(self, all_dirs):
        results = search_content("screen door", all_dirs)
        assert len(results) > 0
        assert "matched_lines" in results[0]
        assert "line" in results[0]["matched_lines"][0]

    def test_case_insensitive(self, all_dirs):
        results = search_content("examplecorp", all_dirs)
        assert len(results) > 0

    def test_no_results(self, all_dirs):
        results = search_content("zzzznonexistentzzzz", all_dirs)
        assert results == []


class TestCombinedSearch:
    def test_returns_both_types(self, all_dirs):
        index = build_index(all_dirs)
        result = search("meta", index, all_dirs)
        assert "tag_matches" in result
        assert "content_matches" in result

    def test_keyword_examplecorp(self, all_dirs):
        index = build_index(all_dirs)
        result = search("examplecorp", index, all_dirs)
        # Should find in tags and/or content
        has_results = len(result["tag_matches"]) > 0 or len(result["content_matches"]) > 0
        assert has_results
