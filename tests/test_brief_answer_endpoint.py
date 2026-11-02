#!/usr/bin/env python3
"""HTTP tests for the note routes against the real dispatcher.

Covers `POST /api/brief/<feed>/<file>/answer`, `POST /api/library/<source>/<relpath>/answer`,
and the `answer_status` / `notes_enabled` read fields. Uses
test_http_security.HttpServerFixture (real ThreadingHttpServer, gate OFF,
SyntheticWorkspace tempdir with feeds `alpha` + `beta`, sources `reports` + `creations`).

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
        self.assertEqual((detail["notes_enabled"], detail["answer_status"]), (True, "answered_pending_filing"))
        self.assertNotIn("answerable", detail)

    def test_every_feed_takes_notes(self):
        beta_path = self.fx.workspace.write_brief("beta", "q.md", QUESTION_TEXT)
        status, _h, body = self.post("/api/brief/beta/q.md/answer", {"text": "hi"})
        self.assertEqual(status, 200, body)
        self.assertTrue(beta_path.read_text(encoding="utf-8").startswith(MARKER_PENDING_FILING + "\n"))

    def test_directory_named_md_is_404(self):
        (self.fx.workspace.root / "briefs_alpha" / "folder.md").mkdir()
        status, _h, _b = self.post("/api/brief/alpha/folder.md/answer", {"text": "hi"})
        self.assertEqual(status, 404)

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

    def test_hidden_subdirectory_inside_feed_dir_is_404(self):
        hidden_path = self.fx.workspace.write_brief("alpha", ".drafts/q.md", QUESTION_TEXT)
        alpha_dir = hidden_path.parent.parent
        (alpha_dir / "link").symlink_to(alpha_dir / ".drafts")
        for path in ("/api/brief/alpha/.drafts%2Fq.md/answer", "/api/brief/alpha/link%2Fq.md/answer"):
            status, _h, _b = self.post(path, {"text": "hi"})
            self.assertEqual(status, 404, path)
        self.assertEqual(hidden_path.read_text(encoding="utf-8"), QUESTION_TEXT)

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

    def test_filed_question_plus_note_reopens_to_pending(self):
        self.brief_path.write_text(MARKER_FILED_PREFIX + " · saved to memory/x.md\n\nQ\n", encoding="utf-8")
        status, _h, body = self.post(ANSWER_PATH, {"text": "correction"})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["answer_status"], "answered_pending_filing")
        on_disk = self.brief_path.read_text(encoding="utf-8")
        self.assertTrue(on_disk.startswith(MARKER_PENDING_FILING + "\n\nQ\n\n---\n\n**💬 Answer · "))

    def test_plain_brief_plus_note_keeps_line_one(self):
        plain = "# Morning\n\nRain.\n"
        morning_path = self.fx.workspace.write_brief("alpha", "morning.md", plain)
        status, _h, body = self.post("/api/brief/alpha/morning.md/answer", {"text": "hi"})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body), {
            "ok": True, "answer_status": None, "filename": "morning.md", "feed_id": "alpha",
        })
        on_disk = morning_path.read_text(encoding="utf-8")
        self.assertTrue(on_disk.startswith("# Morning\n\nRain.\n\n---\n\n**💬 Note · "))
        self.assertTrue(on_disk.endswith("\n\nhi\n"))

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

    def test_feeds_index_has_no_answerable_flag(self):
        status, _h, body = self.fx.request("GET", "/api/feeds")
        self.assertEqual(status, 200)
        for row in json.loads(body)["feeds"]:
            self.assertNotIn("answerable", row)

    def test_brief_detail_reports_status_for_plain_brief(self):
        self.fx.workspace.write_brief("beta", "plain.md", "# Plain\n\nBody.\n")
        status, _h, body = self.fx.request("GET", "/api/brief/beta/plain.md")
        detail = json.loads(body)
        self.assertEqual((detail["notes_enabled"], detail["answer_status"]), (True, None))

    def test_list_items_carry_answer_status(self):
        self.fx.workspace.write_brief("alpha", "plain-zebra.md", "# Zebra plain\n\nBody.\n")
        expected = {"question-2026-11-01.md": "unanswered", "plain-zebra.md": None}

        status, _h, body = self.fx.request("GET", "/api/feed/alpha")
        self.assertEqual(status, 200)
        got = {item["filename"]: item["answer_status"] for item in json.loads(body)["items"]}
        self.assertEqual(got, expected)

        status, _h, body = self.fx.request("GET", "/api/today")
        self.assertEqual(status, 200)
        got = {item["filename"]: item["answer_status"] for item in json.loads(body)["items"]}
        self.assertEqual(got, expected)

        status, _h, body = self.fx.request("GET", "/api/search?q=quiz")
        self.assertEqual(status, 200)
        hits = [hit for hit in json.loads(body)["results"] if hit["kind"] == "brief"]
        self.assertEqual([(h["filename"], h["answer_status"]) for h in hits],
                         [("question-2026-11-01.md", "unanswered")])

    def test_list_status_refreshes_after_a_note(self):
        self.fx.request("GET", "/api/today")  # warm the Today cache
        self.post(ANSWER_PATH, {"text": "hi"})
        status, _h, body = self.fx.request("GET", "/api/today")
        statuses = {item["filename"]: item["answer_status"] for item in json.loads(body)["items"]}
        self.assertEqual(statuses["question-2026-11-01.md"], "answered_pending_filing")


class LibraryAnswerEndpointTest(_HttpTestCaseBase):
    def setUp(self):
        super().setUp()
        self.report_path = self.fx.workspace.write_library_file(
            "reports", "sub/2026-11-01-plan.md", "# Plan\n\nShip it.\n")

    def post(self, path: str, payload=None, raw_body: bytes = None, headers=None):
        body = raw_body if raw_body is not None else json.dumps(payload).encode("utf-8")
        merged = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.fx.port}"}
        if headers:
            merged.update(headers)
        return self.fx.request("POST", path, body=body, extra_headers=merged)

    def test_hidden_directory_reached_via_symlink_or_leading_space_is_404(self):
        hidden_path = self.fx.workspace.write_library_file("reports", ".hidden/x.md", "# H\n")
        reports_root = hidden_path.parent.parent
        (reports_root / "link").symlink_to(reports_root / ".hidden")
        for path in ("/api/library/reports/link%2Fx.md/answer",
                     "/api/library/reports/%20.hidden%2Fx.md/answer",
                     "/api/library/reports/.hidden%2Fx.md/answer"):
            status, _h, _b = self.post(path, {"text": "hi"})
            self.assertEqual(status, 404, path)
        self.assertEqual(hidden_path.read_text(encoding="utf-8"), "# H\n")

    def test_oversized_target_is_404_and_untouched(self):
        import brief_answer_endpoint
        big = "# Big\n\n" + ("x" * 1024 + "\n") * 2049  # just over 2 MiB
        big_path = self.fx.workspace.write_library_file("reports", "big.md", big)
        self.assertGreater(big_path.stat().st_size, brief_answer_endpoint.NOTE_TARGET_MAX_BYTES)
        status, _h, _b = self.post("/api/library/reports/big.md/answer", {"text": "hi"})
        self.assertEqual(status, 404)
        self.assertEqual(big_path.stat().st_size, len(big.encode("utf-8")))

    def test_happy_path_with_encoded_subdirectory(self):
        status, headers, body = self.post("/api/library/reports/sub%2F2026-11-01-plan.md/answer",
                                          {"text": "Looks right."})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body), {
            "ok": True, "answer_status": None, "source": "reports", "relpath": "sub/2026-11-01-plan.md",
        })
        self.assertIn("max-age=0", headers.get("Cache-Control", ""))
        on_disk = self.report_path.read_text(encoding="utf-8")
        self.assertTrue(on_disk.startswith("# Plan\n\nShip it.\n\n---\n\n**💬 Note · "))
        self.assertTrue(on_disk.endswith("\n\nLooks right.\n"))

    def test_marker_file_in_library_flips_to_pending(self):
        question = self.fx.workspace.write_library_file("creations", "q.md", QUESTION_TEXT)
        status, _h, body = self.post("/api/library/creations/q.md/answer", {"text": "hi"})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["answer_status"], "answered_pending_filing")
        self.assertTrue(question.read_text(encoding="utf-8").startswith(MARKER_PENDING_FILING + "\n"))

    def test_unencoded_slash_does_not_match_route(self):
        status, _h, _b = self.post("/api/library/reports/sub/2026-11-01-plan.md/answer", {"text": "hi"})
        self.assertEqual(status, 404)
        self.assertEqual(self.report_path.read_text(encoding="utf-8"), "# Plan\n\nShip it.\n")

    def test_non_md_is_404(self):
        txt = self.fx.workspace.write_library_file("reports", "notes.txt", "plain\n")
        status, _h, _b = self.post("/api/library/reports/notes.txt/answer", {"text": "hi"})
        self.assertEqual(status, 404)
        self.assertEqual(txt.read_text(encoding="utf-8"), "plain\n")

    def test_directory_is_404(self):
        (self.fx.workspace.root / "reports" / "folder.md").mkdir()
        for path in ("/api/library/reports/sub/answer", "/api/library/reports/sub%2F/answer",
                     "/api/library/reports/folder.md/answer"):
            status, _h, _b = self.post(path, {"text": "hi"})
            self.assertEqual(status, 404, path)

    def test_dot_files_are_404(self):
        hidden = self.fx.workspace.write_library_file("reports", ".hidden.md", "# H\n")
        nested = self.fx.workspace.write_library_file("reports", ".cache/x.md", "# X\n")
        for path in ("/api/library/reports/.hidden.md/answer", "/api/library/reports/.cache%2Fx.md/answer"):
            status, _h, _b = self.post(path, {"text": "hi"})
            self.assertEqual(status, 404, path)
        self.assertEqual(hidden.read_text(encoding="utf-8"), "# H\n")
        self.assertEqual(nested.read_text(encoding="utf-8"), "# X\n")

    def test_parent_escapes_are_404(self):
        brief = self.fx.workspace.write_brief("alpha", "q.md", QUESTION_TEXT)
        memory_dir = self.fx.workspace.root / "memory"
        memory_dir.mkdir(exist_ok=True)
        secret = memory_dir / "secret.md"
        secret.write_text(QUESTION_TEXT, encoding="utf-8")
        (self.fx.workspace.root / "reports" / "to-memory.md").symlink_to(secret)
        for path in ("/api/library/reports/..%2Fbriefs_alpha%2Fq.md/answer",
                     "/api/library/reports/%2e%2e%2fmemory%2fsecret.md/answer",
                     "/api/library/reports/sub%2F..%2F..%2Fmemory%2Fsecret.md/answer",
                     "/api/library/reports/%2Fetc%2Fhosts.md/answer",
                     "/api/library/reports/to-memory.md/answer",
                     "/api/library/nope/x.md/answer"):
            status, _h, _b = self.post(path, {"text": "hi"})
            self.assertEqual(status, 404, path)
        self.assertEqual(brief.read_text(encoding="utf-8"), QUESTION_TEXT)
        self.assertEqual(secret.read_text(encoding="utf-8"), QUESTION_TEXT)

    def test_bad_text_and_oversize_body(self):
        status, _h, _b = self.post("/api/library/reports/sub%2F2026-11-01-plan.md/answer", {"text": " "})
        self.assertEqual(status, 400)
        status, _h, _b = self.post("/api/library/reports/sub%2F2026-11-01-plan.md/answer",
                                   raw_body=json.dumps({"text": "x" * 17000}).encode())
        self.assertEqual(status, 413)
        self.assertEqual(self.report_path.read_text(encoding="utf-8"), "# Plan\n\nShip it.\n")

    def test_bad_origin_is_403(self):
        status, _h, _b = self.post("/api/library/reports/sub%2F2026-11-01-plan.md/answer", {"text": "hi"},
                                   headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_locked_gate_rejects_the_write_with_401(self):
        import unlock_gate
        original_reader = unlock_gate.read_passphrase_hash_from_keychain
        unlock_gate.read_passphrase_hash_from_keychain = lambda: "0" * 64
        unlock_gate.GATE_ENABLED_CACHE = None
        unlock_gate.refresh_gate_state()
        try:
            self.assertTrue(unlock_gate.is_gate_enabled())
            status, _h, body = self.post("/api/library/reports/sub%2F2026-11-01-plan.md/answer", {"text": "hi"})
        finally:
            unlock_gate.read_passphrase_hash_from_keychain = original_reader
            unlock_gate.GATE_ENABLED_CACHE = None
            unlock_gate.refresh_gate_state()
        self.assertEqual(status, 401, body)
        self.assertEqual(self.report_path.read_text(encoding="utf-8"), "# Plan\n\nShip it.\n")

    def test_library_metadata_carries_note_fields(self):
        self.fx.workspace.write_library_file("creations", "q.md", QUESTION_TEXT)
        self.fx.workspace.write_library_file("creations", "data.txt", "x\n")
        cases = (("reports/sub%2F2026-11-01-plan.md", (True, None)),
                 ("creations/q.md", (True, "unanswered")),
                 ("creations/data.txt", (False, None)))
        for path, expected in cases:
            status, _h, body = self.fx.request("GET", "/api/library/" + path)
            self.assertEqual(status, 200, path)
            meta = json.loads(body)
            self.assertEqual((meta["notes_enabled"], meta["answer_status"]), expected, path)


if __name__ == "__main__":
    unittest.main()
