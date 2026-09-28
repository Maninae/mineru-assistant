#!/usr/bin/env python3
"""Tests for the --photo image delivery arm of deliver-output.py.

Covers three surfaces:
 - extract_photo_args (CLI parser) — pulls --photo and --caption out of argv
   without touching other tokens, and coexists cleanly with the existing
   --imessage / --imessage-keychain / --imessage-format extractors.
 - send_telegram_photo — builds a real multipart/form-data request, wired
   through a fake urlopen so we can assert on the payload without a network.
 - Backward-compat guard — the text-mode path resolves through
   load_telegram_config() and send_telegram unchanged when no --photo is set.

Run: python3 -m pytest tests/test_deliver_output_photo.py -v
"""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "deliver-output.py"
spec = importlib.util.spec_from_file_location("deliver_output", MODULE_PATH)
deliver_output = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deliver_output)


class ExtractPhotoArgsTest(unittest.TestCase):
    def test_pops_photo_and_caption(self) -> None:
        argv = ["--photo", "/tmp/a.png", "--caption", "hi there"]
        out, photo, caption = deliver_output.extract_photo_args(argv)
        self.assertEqual(out, [])
        self.assertEqual(photo, "/tmp/a.png")
        self.assertEqual(caption, "hi there")

    def test_photo_alone(self) -> None:
        argv = ["--photo", "/tmp/a.png"]
        out, photo, caption = deliver_output.extract_photo_args(argv)
        self.assertEqual(out, [])
        self.assertEqual(photo, "/tmp/a.png")
        self.assertIsNone(caption)

    def test_photo_absent_returns_none(self) -> None:
        argv = ["some.md", "--imessage-format", "transactions"]
        out, photo, caption = deliver_output.extract_photo_args(argv)
        self.assertEqual(out, ["some.md", "--imessage-format", "transactions"])
        self.assertIsNone(photo)
        self.assertIsNone(caption)

    def test_leaves_other_tokens_intact(self) -> None:
        argv = ["other.md", "--photo", "/tmp/a.png", "--imessage", "+15555550100"]
        out, photo, caption = deliver_output.extract_photo_args(argv)
        # --imessage is *not* consumed by extract_photo_args (that's the
        # imessage extractor's job). It must round-trip untouched.
        self.assertEqual(out, ["other.md", "--imessage", "+15555550100"])
        self.assertEqual(photo, "/tmp/a.png")
        self.assertIsNone(caption)

    def test_caption_only_consumed_in_photo_mode(self) -> None:
        # L2 (audit): a text-mode caller passing --caption for some future
        # feature must NOT have that flag consumed by this extractor.
        argv = ["some.md", "--caption", "not-a-photo-caption"]
        out, photo, caption = deliver_output.extract_photo_args(argv)
        self.assertEqual(out, argv)
        self.assertIsNone(photo)
        self.assertIsNone(caption)

    def test_caption_consumed_when_photo_present(self) -> None:
        # And the opposite: with --photo, --caption still gets extracted.
        argv = ["--caption", "hi", "--photo", "/tmp/a.png"]
        out, photo, caption = deliver_output.extract_photo_args(argv)
        self.assertEqual(out, [])
        self.assertEqual(photo, "/tmp/a.png")
        self.assertEqual(caption, "hi")


class SendTelegramPhotoTest(unittest.TestCase):
    def _fake_response(self, ok: bool = True) -> mock.MagicMock:
        r = mock.MagicMock()
        r.__enter__.return_value = r
        r.__exit__.return_value = False
        r.read.return_value = json.dumps({"ok": ok, "result": {}}).encode("utf-8")
        return r

    def test_missing_file_returns_false(self) -> None:
        result = deliver_output.send_telegram_photo(
            "/nonexistent/nope.png", "cap", "tok", "12345"
        )
        self.assertFalse(result)

    def test_multipart_body_contains_chat_id_caption_and_photo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Minimal PNG magic bytes — content just has to be a real file.
            img = Path(tmp) / "shot.png"
            img.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03")

            captured: dict = {}

            def fake_urlopen(req, timeout=None):
                captured["url"] = req.full_url
                captured["headers"] = dict(req.header_items())
                captured["body"] = req.data
                return self._fake_response(ok=True)

            with mock.patch.object(deliver_output.urllib.request, "urlopen", fake_urlopen):
                ok = deliver_output.send_telegram_photo(
                    str(img), "hello caption", "TESTTOKEN", "99999"
                )

            self.assertTrue(ok)
            self.assertEqual(
                captured["url"], "https://api.telegram.org/botTESTTOKEN/sendPhoto"
            )
            content_type = captured["headers"].get("Content-type", "")
            self.assertTrue(content_type.startswith("multipart/form-data; boundary="))

            body = captured["body"]
            # Text fields are UTF-8, PNG bytes are binary — search each in its
            # native form.
            self.assertIn(b'name="chat_id"', body)
            self.assertIn(b"99999", body)
            self.assertIn(b'name="caption"', body)
            self.assertIn(b"hello caption", body)
            self.assertIn(b'name="photo"', body)
            self.assertIn(b'filename="shot.png"', body)
            self.assertIn(b"\x89PNG", body)

    def test_no_caption_omits_caption_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "shot.png"
            img.write_bytes(b"\x89PNG\r\n\x1a\n")

            captured: dict = {}

            def fake_urlopen(req, timeout=None):
                captured["body"] = req.data
                return self._fake_response(ok=True)

            with mock.patch.object(deliver_output.urllib.request, "urlopen", fake_urlopen):
                ok = deliver_output.send_telegram_photo(
                    str(img), None, "TESTTOKEN", "99999"
                )

            self.assertTrue(ok)
            self.assertNotIn(b'name="caption"', captured["body"])
            self.assertNotIn(b'name="parse_mode"', captured["body"])

    def test_api_ok_false_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "shot.png"
            img.write_bytes(b"\x89PNG")

            def fake_urlopen(req, timeout=None):
                return self._fake_response(ok=False)

            with mock.patch.object(deliver_output.urllib.request, "urlopen", fake_urlopen):
                ok = deliver_output.send_telegram_photo(
                    str(img), "cap", "TESTTOKEN", "99999"
                )
            self.assertFalse(ok)

    def test_html_special_chars_in_caption_are_escaped(self) -> None:
        # M1 (audit): parse_mode=HTML means an ampersand or angle bracket in
        # a raw caption crashes Telegram with HTTP 400. Escape must happen
        # in send_telegram_photo before the multipart body is built.
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "shot.png"
            img.write_bytes(b"\x89PNG")

            captured: dict = {}

            def fake_urlopen(req, timeout=None):
                captured["body"] = req.data
                return self._fake_response(ok=True)

            with mock.patch.object(deliver_output.urllib.request, "urlopen", fake_urlopen):
                ok = deliver_output.send_telegram_photo(
                    str(img), "Smith & <Burns> tour on 9/14",
                    "TESTTOKEN", "99999",
                )
            self.assertTrue(ok)
            body = captured["body"]
            # Raw form MUST NOT appear (would 400 on the Telegram side).
            self.assertNotIn(b"Smith & <Burns>", body)
            # Escaped form MUST appear.
            self.assertIn(b"Smith &amp; &lt;Burns&gt; tour on 9/14", body)

    def test_html_escape_only_when_parse_mode_html(self) -> None:
        # A caller passing parse_mode="" (plain text) opts out of the
        # escape and gets its literal caption through.
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "shot.png"
            img.write_bytes(b"\x89PNG")

            captured: dict = {}

            def fake_urlopen(req, timeout=None):
                captured["body"] = req.data
                return self._fake_response(ok=True)

            with mock.patch.object(deliver_output.urllib.request, "urlopen", fake_urlopen):
                deliver_output.send_telegram_photo(
                    str(img), "A & B", "TESTTOKEN", "99999", parse_mode="",
                )
            body = captured["body"]
            self.assertIn(b"A & B", body)
            self.assertNotIn(b"A &amp; B", body)

    def test_long_caption_tail_is_html_escaped_too(self) -> None:
        # M1 (audit) part 2: the >1024-char tail rides on a follow-up
        # sendMessage call. If we forgot to escape it, the head sends OK
        # (partial delivery) and the tail 400s. Both parts must be escaped.
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "shot.png"
            img.write_bytes(b"\x89PNG")
            # Make head fit but tail contains "&": force overflow so the
            # tail path is exercised.
            padding = "x" * deliver_output.TELEGRAM_PHOTO_CAPTION_LIMIT
            tail = " tail-with-& in it"
            caption = padding + tail

            call_bodies: list = []

            def fake_urlopen(req, timeout=None):
                call_bodies.append(req.data or b"")
                return self._fake_response(ok=True)

            with mock.patch.object(deliver_output.urllib.request, "urlopen", fake_urlopen):
                ok = deliver_output.send_telegram_photo(
                    str(img), caption, "TESTTOKEN", "99999",
                )
            self.assertTrue(ok)
            self.assertEqual(len(call_bodies), 2)
            # First call is sendPhoto (multipart body — raw & would appear
            # as bytes in the head). Second call is sendMessage (JSON body).
            follow_body = call_bodies[1].decode("utf-8", errors="replace")
            self.assertIn("tail-with-&amp;", follow_body)
            self.assertNotIn("tail-with-& ", follow_body)  # raw form absent

    def test_filename_with_crlf_is_sanitized_in_header(self) -> None:
        # L1 (audit): a filename containing \r or \n would inject header
        # rows / body chunks into the multipart request. Sanitize to
        # something safe.
        with tempfile.TemporaryDirectory() as tmp:
            weird = Path(tmp) / "ok.png"
            weird.write_bytes(b"\x89PNG")

            captured: dict = {}

            def fake_urlopen(req, timeout=None):
                captured["body"] = req.data
                captured["headers"] = dict(req.header_items())
                return self._fake_response(ok=True)

            # We can't create a file whose OS name literally contains CRLF
            # (kernel disallows /), so simulate by passing the raw name
            # through the sanitizer directly.
            hostile = 'a"\r\nContent-Type: text/html\r\n\r\n<script>alert(1)</script>.png'
            safe = deliver_output._sanitize_multipart_filename(hostile)
            self.assertNotIn("\r", safe)
            self.assertNotIn("\n", safe)
            self.assertNotIn('"', safe)

            # Also spot-check that a normal filename passes through cleanly.
            self.assertEqual(
                deliver_output._sanitize_multipart_filename("normal-name_1.png"),
                "normal-name_1.png",
            )

            # And prove send_telegram_photo doesn't crash on a normal file.
            with mock.patch.object(deliver_output.urllib.request, "urlopen", fake_urlopen):
                ok = deliver_output.send_telegram_photo(
                    str(weird), None, "TESTTOKEN", "99999",
                )
            self.assertTrue(ok)

    def test_long_caption_splits_into_photo_head_plus_followup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "shot.png"
            img.write_bytes(b"\x89PNG")
            over_limit = "x" * (deliver_output.TELEGRAM_PHOTO_CAPTION_LIMIT + 200)

            calls: list = []

            def fake_urlopen(req, timeout=None):
                calls.append(req.full_url)
                return self._fake_response(ok=True)

            with mock.patch.object(deliver_output.urllib.request, "urlopen", fake_urlopen):
                ok = deliver_output.send_telegram_photo(
                    str(img), over_limit, "TESTTOKEN", "99999"
                )

            self.assertTrue(ok)
            # First call is sendPhoto (with the head of the caption). Second
            # call is sendMessage (for the tail). Anything else = regression.
            self.assertEqual(len(calls), 2)
            self.assertTrue(calls[0].endswith("/sendPhoto"))
            self.assertTrue(calls[1].endswith("/sendMessage"))


class IMessageRecipientMaskingTest(unittest.TestCase):
    """Delivery stdout/stderr lands in job logs: never echo a full recipient number."""

    NUMBER = "+15555550189"

    def run_send(self, send_callable, returncode, stderr=""):
        completed = mock.Mock(returncode=returncode, stderr=stderr)
        captured_out, captured_err = io.StringIO(), io.StringIO()
        with mock.patch.object(deliver_output.subprocess, "run", return_value=completed), \
                mock.patch("sys.stdout", captured_out), mock.patch("sys.stderr", captured_err):
            send_callable()
        return captured_out.getvalue(), captured_err.getvalue()

    def test_mask_keeps_last_two_digits_only(self) -> None:
        self.assertEqual(deliver_output.mask_recipient(self.NUMBER), "…89")
        self.assertEqual(deliver_output.mask_recipient("(555) 555-0142"), "…42")
        self.assertEqual(deliver_output.mask_recipient(""), "…")

    def test_text_send_success_prints_masked(self) -> None:
        out, _ = self.run_send(lambda: deliver_output.send_imessage("hello", self.NUMBER), 0)
        self.assertIn("via iMessage to …89", out)
        self.assertNotIn("5555550189", out)

    def test_photo_send_success_prints_masked(self) -> None:
        out, _ = self.run_send(
            lambda: deliver_output.send_imessage_photo("/tmp/a.png", "cap", self.NUMBER), 0)
        self.assertIn("via iMessage to …89", out)
        self.assertNotIn("5555550189", out)

    def test_failure_stderr_echoing_number_is_masked(self) -> None:
        failure = f"no buddy for {self.NUMBER}"
        _, err = self.run_send(lambda: deliver_output.send_imessage("hi", self.NUMBER), 1, failure)
        self.assertNotIn("5555550189", err)
        _, err = self.run_send(
            lambda: deliver_output.send_imessage_photo("/tmp/a.png", None, self.NUMBER), 1, failure)
        self.assertNotIn("5555550189", err)


class BackwardCompatTest(unittest.TestCase):
    """The text path must survive additive changes intact.

    Simpler check than replaying the whole main(): assert the well-known
    text-path symbols are still exported and behave.
    """

    def test_public_text_path_functions_still_present(self) -> None:
        self.assertTrue(callable(getattr(deliver_output, "send_telegram", None)))
        self.assertTrue(callable(getattr(deliver_output, "load_telegram_config", None)))
        self.assertTrue(callable(getattr(deliver_output, "chunk_message", None)))
        self.assertTrue(callable(getattr(deliver_output, "build_inject_pointer", None)))

    def test_new_image_symbols_exported(self) -> None:
        self.assertTrue(callable(getattr(deliver_output, "send_telegram_photo", None)))
        self.assertTrue(callable(getattr(deliver_output, "send_imessage_photo", None)))
        self.assertTrue(callable(getattr(deliver_output, "extract_photo_args", None)))


if __name__ == "__main__":
    unittest.main()
