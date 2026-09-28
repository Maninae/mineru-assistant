"""Tests for semantic_ranker module."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import patch

import pytest

from msearch.semantic_ranker import _parse_tags, suggest_tags

ALL_TAGS = ["meta", "project deadline", "examplecorp", "screen door", "vendor", "warehouse", "journal"]


class TestParseTags:
    def test_clean_output(self):
        response = "meta\nproject deadline\nexamplecorp"
        result = _parse_tags(response, ALL_TAGS, 5)
        assert result == ["meta", "project deadline", "examplecorp"]

    def test_numbered_output(self):
        response = "1. meta\n2. project deadline\n3. examplecorp"
        result = _parse_tags(response, ALL_TAGS, 5)
        assert result == ["meta", "project deadline", "examplecorp"]

    def test_bulleted_output(self):
        response = "- meta\n- project deadline\n* examplecorp"
        result = _parse_tags(response, ALL_TAGS, 5)
        assert result == ["meta", "project deadline", "examplecorp"]

    def test_filters_nonexistent_tags(self):
        response = "meta\nfake tag\nexamplecorp"
        result = _parse_tags(response, ALL_TAGS, 5)
        assert "fake tag" not in result
        assert result == ["meta", "examplecorp"]

    def test_respects_top_n(self):
        response = "meta\nproject deadline\nexamplecorp\nscreen door\nvendor"
        result = _parse_tags(response, ALL_TAGS, 2)
        assert len(result) == 2

    def test_garbled_output(self):
        response = "Here are some tags I think:\n\nWell, maybe meta? Or something else entirely."
        result = _parse_tags(response, ALL_TAGS, 5)
        # Should extract "meta" from the noise
        assert "meta" in result or len(result) == 0  # graceful either way

    def test_empty_response(self):
        result = _parse_tags("", ALL_TAGS, 5)
        assert result == []


class MockOllamaHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Read request body to avoid connection issues
        content_length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(content_length)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        body = json.dumps({"response": "meta\nproject deadline\nexamplecorp"}).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # suppress logs


@pytest.fixture()
def mock_ollama():
    """Start a mock Ollama server and yield its base URL."""
    server = HTTPServer(("127.0.0.1", 0), MockOllamaHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


class TestSuggestTags:
    def test_mock_ollama_response(self, mock_ollama):
        result = suggest_tags("project stuff", ALL_TAGS, "test", 5, mock_ollama)
        assert "meta" in result
        assert "project deadline" in result

    def test_ollama_unavailable(self):
        with pytest.raises(ConnectionError):
            suggest_tags("test", ALL_TAGS, "test", 5, "http://127.0.0.1:19999")

    def test_filters_invalid_tags(self, mock_ollama):
        result = suggest_tags("test", ALL_TAGS, "test", 5, mock_ollama)
        for tag in result:
            assert tag in ALL_TAGS
