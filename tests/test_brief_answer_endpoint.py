#!/usr/bin/env python3
"""HTTP tests for POST /api/brief/<feed>/<file>/answer against the real dispatcher.

Uses test_http_security.HttpServerFixture (real ThreadingHttpServer, gate OFF,
SyntheticWorkspace tempdir). In that workspace feed `alpha` is answerable and
feed `beta` is not.

Run: /usr/bin/python3 -m pytest tests/test_brief_answer_endpoint.py -q
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_http_security import _HttpTestCaseBase  # noqa: E402

from brief_answers import (  # noqa: E402
    MARKER_FILED_PREFIX,
    MARKER_PENDING_FILING,
    MARKER_UNANSWERED,
)


QUESTION_TEXT = MARKER_UNANSWERED + "\n\n🦊 **Curiosity question**\n\nWhat did the quiz make you?\n"
ANSWER_PATH = "/api/brief/alpha/question-2026-11-01.md/answer"


class BriefAnswerEndpointTest(_HttpTestCaseBase):
    def setUp(self):
        super().setUp()
        self.brief_path = self.fx.workspace.write_brief("alpha", "question-2026-11-01.md", QUESTION_TEXT)

    def post(self, path: str, payload=None, raw_body: bytes = None, headers=None):
        body = raw_body if raw_body is not None else json.dumps(payload).encode("utf-8")
        merged = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.fx.port}"}
        if headers:
            merged.update(headers)
        return self.fx.request("POST", path, body=body, extra_headers=merged)

    def test_wrong_content_type_is_415(self):
        status, _h, _b = self.post(ANSWER_PATH, {"text": "hi"}, headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertEqual(self.brief_path.read_text(encoding="utf-8"), QUESTION_TEXT)

    def test_bad_origin_is_403(self):
        status, _h, _b = self.post(ANSWER_PATH, {"text": "hi"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.brief_path.read_text(encoding="utf-8"), QUESTION_TEXT)

    def test_happy_path_writes_note_and_flips_marker(self):
        status, headers, body = self.post(ANSWER_PATH, {"text": "  Charmander, with Pikachu.  "})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body), {
            "ok": True, "answer_status": "answered_pending_filing",
            "filename": "question-2026-11-01.md", "feed_id": "alpha",
        })
        self.assertIn("max-age=0", headers.get("Cache-Control", ""))
        on_disk = self.brief_path.read_text(encoding="utf-8")
        self.assertTrue(on_disk.startswith(MARKER_PENDING_FILING + "\n"))
        self.assertIn("**💬 Answer · ", on_disk)
        self.assertTrue(on_disk.endswith("\n\nCharmander, with Pikachu.\n"))

        # The brief detail now reports the new status.
        status, _h, body = self.fx.request("GET", "/api/brief/alpha/question-2026-11-01.md")
        detail = json.loads(body)
        self.assertEqual((detail["answerable"], detail["answer_status"]), (True, "answered_pending_filing"))

    def test_non_answerable_feed_is_404(self):
        beta_path = self.fx.workspace.write_brief("beta", "q.md", QUESTION_TEXT)
        status, _h, _b = self.post("/api/brief/beta/q.md/answer", {"text": "hi"})
        self.assertEqual(status, 404)
        self.assertEqual(beta_path.read_text(encoding="utf-8"), QUESTION_TEXT)

    def test_unknown_feed_is_404(self):
        status, _h, _b = self.post("/api/brief/nope/q.md/answer", {"text": "hi"})
        self.assertEqual(status, 404)

    def test_missing_file_is_404(self):
        status, _h, _b = self.post("/api/brief/alpha/absent.md/answer", {"text": "hi"})
        self.assertEqual(status, 404)

    def test_dotdot_filenames_are_404(self):
        beta_path = self.fx.workspace.write_brief("beta", "q.md", QUESTION_TEXT)
        for path in ("/api/brief/alpha/../answer",
                     "/api/brief/alpha/..%2fbriefs_beta%2fq.md/answer",
                     "/api/brief/alpha/%2e%2e%2fbriefs_beta%2fq.md/answer"):
            status, _h, _b = self.post(path, {"text": "hi"})
            self.assertEqual(status, 404, path)
        self.assertEqual(beta_path.read_text(encoding="utf-8"), QUESTION_TEXT)

    def test_symlink_inside_feed_dir_cannot_reach_other_feed_or_memory(self):
        beta_path = self.fx.workspace.write_brief("beta", "q.md", QUESTION_TEXT)
        memory_dir = self.fx.workspace.root / "memory"
        memory_dir.mkdir(exist_ok=True)
        memory_path = memory_dir / "secret.md"
        memory_path.write_text(QUESTION_TEXT, encoding="utf-8")
        alpha_dir = self.fx.workspace.root / "briefs_alpha"
        (alpha_dir / "to-beta.md").symlink_to(beta_path)
        (alpha_dir / "to-memory.md").symlink_to(memory_path)
        for path in ("/api/brief/alpha/to-beta.md/answer", "/api/brief/alpha/to-memory.md/answer"):
            status, _h, _b = self.post(path, {"text": "hi"})
            self.assertEqual(status, 404, path)
        self.assertEqual(beta_path.read_text(encoding="utf-8"), QUESTION_TEXT)
        self.assertEqual(memory_path.read_text(encoding="utf-8"), QUESTION_TEXT)

    def test_dot_prefixed_filename_is_404(self):
        hidden_path = self.fx.workspace.write_brief("alpha", ".hidden.md", QUESTION_TEXT)
        status, _h, _b = self.post("/api/brief/alpha/.hidden.md/answer", {"text": "hi"})
        self.assertEqual(status, 404)
        self.assertEqual(hidden_path.read_text(encoding="utf-8"), QUESTION_TEXT)

    def test_non_utf8_file_is_409_and_untouched(self):
        raw = (MARKER_UNANSWERED + "\n\ncaf").encode("utf-8") + b"\xe9\n"
        self.brief_path.write_bytes(raw)
        status, _h, body = self.post(ANSWER_PATH, {"text": "hi"})
        self.assertEqual(status, 409, body)
        self.assertEqual(self.brief_path.read_bytes(), raw)

    def test_locked_gate_rejects_the_write_with_401(self):
        import unlock_gate
        original_reader = unlock_gate.read_passphrase_hash_from_keychain
        unlock_gate.read_passphrase_hash_from_keychain = lambda: "0" * 64
        unlock_gate.GATE_ENABLED_CACHE = None
        unlock_gate.refresh_gate_state()
        try:
            self.assertTrue(unlock_gate.is_gate_enabled())
            status, _h, body = self.post(ANSWER_PATH, {"text": "hi"})
        finally:
            unlock_gate.read_passphrase_hash_from_keychain = original_reader
            unlock_gate.GATE_ENABLED_CACHE = None
            unlock_gate.refresh_gate_state()
        self.assertEqual(status, 401, body)
        self.assertEqual(json.loads(body), {"error": "locked"})
        self.assertEqual(self.brief_path.read_text(encoding="utf-8"), QUESTION_TEXT)

    def test_non_md_file_is_404(self):
        txt_path = self.fx.workspace.write_brief("alpha", "q.txt", QUESTION_TEXT)
        status, _h, _b = self.post("/api/brief/alpha/q.txt/answer", {"text": "hi"})
        self.assertEqual(status, 404)
        self.assertEqual(txt_path.read_text(encoding="utf-8"), QUESTION_TEXT)

    def test_empty_text_is_400(self):
        for payload in ({"text": ""}, {"text": "   "}, {}, {"text": 5}):
            status, _h, _b = self.post(ANSWER_PATH, payload)
            self.assertEqual(status, 400, payload)

    def test_bad_json_is_400(self):
        status, _h, _b = self.post(ANSWER_PATH, raw_body=b"{not json")
        self.assertEqual(status, 400)
        status, _h, _b = self.post(ANSWER_PATH, raw_body=b"[1, 2]")
        self.assertEqual(status, 400)

    def test_filed_file_is_409(self):
        filed = MARKER_FILED_PREFIX + " · saved to memory/x.md\n\nQ\n"
        self.brief_path.write_text(filed, encoding="utf-8")
        status, _h, body = self.post(ANSWER_PATH, {"text": "late"})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body), {"error": "already filed"})
        self.assertEqual(self.brief_path.read_text(encoding="utf-8"), filed)

    def test_no_marker_file_is_409(self):
        self.fx.workspace.write_brief("alpha", "morning.md", "# Morning\n\nRain.\n")
        status, _h, body = self.post("/api/brief/alpha/morning.md/answer", {"text": "hi"})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body), {"error": "not answerable"})

    def test_body_over_16k_is_413(self):
        status, _h, _b = self.post(ANSWER_PATH, raw_body=json.dumps({"text": "x" * 17000}).encode())
        self.assertEqual(status, 413)
        self.assertEqual(self.brief_path.read_text(encoding="utf-8"), QUESTION_TEXT)

    def test_4000_char_cjk_emoji_answer_succeeds(self):
        # Browser JSON.stringify sends raw UTF-8 (3 bytes/CJK, 4 bytes/emoji), not \u escapes.
        answer = ("答" * 2000) + ("🦊" * 2000)
        body = json.dumps({"text": answer}, ensure_ascii=False).encode("utf-8")
        self.assertGreater(len(body), 4096)
        self.assertLess(len(body), 16384)
        status, _h, resp = self.post(ANSWER_PATH, raw_body=body)
        self.assertEqual(status, 200, resp)
        self.assertTrue(self.brief_path.read_text(encoding="utf-8").endswith(answer + "\n"))

    def test_seen_post_keeps_4k_cap(self):
        # The per-route override must not widen the global cap for other routes.
        status, _h, _b = self.post("/api/seen", raw_body=json.dumps({"feed_id": "x" * 5000}).encode())
        self.assertEqual(status, 413)

    def test_feeds_index_reports_answerable(self):
        status, _h, body = self.fx.request("GET", "/api/feeds")
        flags = {row["id"]: row["answerable"] for row in json.loads(body)["feeds"]}
        self.assertEqual(flags, {"alpha": True, "beta": False})

    def test_brief_detail_reports_status_for_plain_brief(self):
        self.fx.workspace.write_brief("beta", "plain.md", "# Plain\n\nBody.\n")
        status, _h, body = self.fx.request("GET", "/api/brief/beta/plain.md")
        detail = json.loads(body)
        self.assertEqual((detail["answerable"], detail["answer_status"]), (False, None))


if __name__ == "__main__":
    unittest.main()
