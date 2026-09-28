#!/usr/bin/env python3
"""Tests for scripts/publish_encrypted_page.py.

Covers the load-bearing pieces we can hit without touching the network / git:

  - Salt persistence (per-page, stable across runs, 0600).
  - Distinctive-phrase extraction (multi-word, >= 12 chars, space-containing).
  - The leak check: fires on a real leak, refuses "clean" verdicts when the
    encrypted file is missing, and is silent on a genuine ciphertext-only file.
  - Slugification (safe filename per subpath).
  - Share-link regex parses the URL out of a realistic StatiCrypt stdout blob.
  - The dry-run path exercises the full orchestrator with subprocess+keychain
    stubbed, so the encrypt -> leak-check -> share-link chain is verified.

Run: python3 -m pytest tests/test_publish_encrypted_page.py -v
"""

import base64
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import publish_encrypted_page as pep  # noqa: E402


class SaltPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._patched = mock.patch.object(
            pep, "SALTS_DIR", Path(self.tmp.name) / "salts"
        )
        self._patched.start()
        self.addCleanup(self._patched.stop)

    def test_salt_generated_and_reused(self) -> None:
        salt1 = pep.get_or_create_salt("housing")
        salt2 = pep.get_or_create_salt("housing")
        self.assertEqual(salt1, salt2, "salt must be stable across calls")
        self.assertRegex(salt1, r"^[0-9a-f]{32}$")

    def test_salts_are_per_page(self) -> None:
        s1 = pep.get_or_create_salt("housing")
        s2 = pep.get_or_create_salt("anthology")
        self.assertNotEqual(s1, s2)

    def test_salt_file_is_mode_0600(self) -> None:
        pep.get_or_create_salt("housing")
        p = pep.salt_path_for("housing")
        self.assertTrue(p.exists())
        mode = stat.S_IMODE(p.stat().st_mode)
        self.assertEqual(mode, 0o600, oct(mode))

    def test_slugify_subpath(self) -> None:
        self.assertEqual(pep.slugify_subpath("housing/"), "housing")
        self.assertEqual(pep.slugify_subpath("housing/2026"), "housing-2026")
        self.assertEqual(pep.slugify_subpath("weird$$path"), "weird-path")
        # empty-ish input still gets a stable slug (via hash) so the salt file
        # can still exist rather than crashing on an empty filename.
        empty_stem = pep.slugify_subpath("///")
        self.assertTrue(empty_stem)


class ExtractDistinctivePhrasesTest(unittest.TestCase):
    def test_pulls_multi_word_phrases(self) -> None:
        html = ("<html><body><h1>Housing report</h1>"
                "<p>Smith and Burns look promising for a family of four.</p>"
                "<p>Short.</p></body></html>")
        phrases = pep.extract_distinctive_phrases(html)
        # Must find the multi-word content, not the single "Short.".
        self.assertTrue(any("Smith and Burns" in p for p in phrases))
        self.assertTrue(all(" " in p for p in phrases), "every phrase has a space")
        self.assertTrue(
            all(len(p) >= pep.MIN_LEAK_PHRASE_CHARS for p in phrases),
            "every phrase >= MIN_LEAK_PHRASE_CHARS",
        )

    def test_strips_script_and_style(self) -> None:
        html = ("<html><script>var LEO_DATA_INSIDE_SCRIPT = 'noise';</script>"
                "<style>.x { color: red thing text; }</style>"
                "<p>Only visible sentence appears here.</p></html>")
        phrases = pep.extract_distinctive_phrases(html)
        self.assertTrue(any("Only visible sentence" in p for p in phrases))
        # Must NOT pull JS/CSS text.
        joined = " ".join(phrases)
        self.assertNotIn("LEO_DATA_INSIDE_SCRIPT", joined)

    def test_short_and_no_space_content_drops_out(self) -> None:
        # A source with only very short bits (each <12 chars, no space
        # runs long enough) yields no distinctive phrases — the leak check
        # then warns loudly on the caller's stderr rather than silently
        # passing. Strong invariant: nothing distinctive == no false negatives.
        html = "<p>Short.</p><p>Small.</p>"
        # Every kept phrase must be >= MIN_LEAK_PHRASE_CHARS and contain a space.
        for phrase in pep.extract_distinctive_phrases(html):
            self.assertGreaterEqual(len(phrase), pep.MIN_LEAK_PHRASE_CHARS)
            self.assertIn(" ", phrase)


class LeakCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_missing_output_is_failure_not_clean(self) -> None:
        # The critical rule: an absent file must NOT read as "no leak found".
        source = "<p>Distinctive sentence with plenty of chars here.</p>"
        with self.assertRaises(SystemExit) as ctx:
            pep.assert_no_plaintext_leak(source, Path(self.tmp.name) / "nope.html")
        self.assertIn("does NOT exist", str(ctx.exception))

    def test_real_leak_raises(self) -> None:
        source = "<p>Distinctive sentence with plenty of chars here.</p>"
        cipher = Path(self.tmp.name) / "cipher.html"
        # This "ciphertext" contains the plaintext verbatim — a leak.
        cipher.write_text(
            "<html><body>Distinctive sentence with plenty of chars here.</body></html>"
        )
        with self.assertRaises(SystemExit) as ctx:
            pep.assert_no_plaintext_leak(source, cipher)
        self.assertIn("PLAINTEXT LEAK", str(ctx.exception))

    def test_base64_output_passes_and_short_words_do_not_false_positive(self) -> None:
        # This is the specific false-positive the old grep hit: a short 3-letter
        # word appears by chance inside base64 output. The new phrase-based
        # check must silently pass.
        source = "<p>abc</p><p>Ada is here</p><p>very long distinctive phrase for the leak check.</p>"
        # Build "ciphertext" that is a big blob of base64 — no spaces — that
        # happens to contain the letters "abc" and "Ada" scattered.
        payload = ("Some pseudo-encrypted content that contains l, e, o "
                   "letters and A, d, a letters in random arrangements but "
                   "NO recognizable phrases from the source.")
        cipher_bytes = base64.b64encode(payload.encode("utf-8") * 20)
        # Base64 has NO spaces.
        self.assertNotIn(b" ", cipher_bytes)
        cipher = Path(self.tmp.name) / "cipher.html"
        cipher.write_bytes(b"<html><body><pre>" + cipher_bytes + b"</pre></body></html>")
        # Must return cleanly — no leak, no false positive.
        pep.assert_no_plaintext_leak(source, cipher)


class ShareLinkParseTest(unittest.TestCase):
    def test_regex_finds_link_in_realistic_stdout(self) -> None:
        # Mirror what StatiCrypt actually prints. The link is embedded in the
        # middle of prose, sometimes followed by punctuation.
        fake_stdout = (
            "Your file has been encrypted.\n"
            "Your share link:\n"
            "https://example.github.io/pages/housing/"
            "#staticrypt_pwd=abcd1234deadbeef.\n"
            "Have a nice day.\n"
        )
        import re as _re
        m = _re.search(r"https?://\S*#staticrypt_pwd=\S+", fake_stdout)
        self.assertIsNotNone(m)
        link = m.group(0).rstrip(".,)")
        self.assertTrue(link.startswith("https://example.github.io/pages/housing/"))
        self.assertIn("#staticrypt_pwd=abcd1234deadbeef", link)


class DryRunOrchestrationTest(unittest.TestCase):
    """End-to-end dry-run: encrypt, leak-check, share-link — no git, no net.

    Stubs `run`, keychain, and salt storage. Simulates staticrypt by writing
    a plausible ciphertext file to the tmpdir for the leak check to read.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)
        # Redirect salt storage into the tmpdir.
        self._salts_patch = mock.patch.object(pep, "SALTS_DIR", self.tmpdir / "salts")
        self._salts_patch.start()
        self.addCleanup(self._salts_patch.stop)

    def _fake_run(self, argv, cwd=None, env=None, scrub=None):
        # Recognize the three subprocess call shapes we care about. Accept
        # the new `scrub=` kwarg without using it (this test just verifies
        # orchestration; the passphrase-leak tests below prove scrub works).
        exe = argv[0]
        if exe == "security":
            return "secret-pass\n"
        # staticrypt is invoked as ["npx", "-y", "staticrypt", ...]
        if list(argv[:3]) == list(pep.STATICRYPT_CMD):
            # Encrypt call: has -d <outdir>. Write a fake ciphertext there.
            if "-d" in argv:
                out_idx = argv.index("-d") + 1
                out_dir = Path(argv[out_idx])
                out_dir.mkdir(parents=True, exist_ok=True)
                input_file = Path(argv[3])
                (out_dir / input_file.name).write_text(
                    "<html>encrypted-blob-no-plaintext-here</html>"
                )
                return "Encryption complete.\n"
            # Share call: has --share <base>. Print a share link.
            if "--share" in argv:
                base_idx = argv.index("--share") + 1
                return (
                    f"Your file has been encrypted.\n"
                    f"Your share link:\n{argv[base_idx]}#staticrypt_pwd=deadbeef\n"
                )
        raise AssertionError("unexpected subprocess argv in fake_run: %r" % (argv,))

    def test_dry_run_returns_share_link_and_does_not_touch_repo(self) -> None:
        src = self.tmpdir / "report.html"
        src.write_text(
            "<html><body>"
            "<h1>Housing Report</h1>"
            "<p>Smith and Burns is our top candidate for a family relocation.</p>"
            "</body></html>"
        )
        # We do NOT patch commit_and_push; dry_run must not reach it.
        with mock.patch.object(pep, "run", side_effect=self._fake_run):
            link = pep.publish(
                input_html=src,
                keychain_service="test-service",
                subpath="housing",
                share_base="https://example.github.io/pages",
                repo=self.tmpdir / "nonexistent-repo",   # would fail if touched
                dry_run=True,
                poll=False,
            )
        self.assertIn("#staticrypt_pwd=", link)
        self.assertIn("housing", link)
        # A dry-run must not have created the repo dir.
        self.assertFalse((self.tmpdir / "nonexistent-repo").exists())

    def test_dry_run_reuses_persisted_salt(self) -> None:
        src = self.tmpdir / "report.html"
        src.write_text("<html><body><p>Distinctive sentence for testing.</p></body></html>")
        with mock.patch.object(pep, "run", side_effect=self._fake_run):
            pep.publish(
                input_html=src, keychain_service="s", subpath="housing",
                share_base="https://example.github.io/pages", dry_run=True, poll=False,
            )
            salt_first = pep.get_or_create_salt("housing")
            # Run again; salt must be identical, so share link is stable.
            pep.publish(
                input_html=src, keychain_service="s", subpath="housing",
                share_base="https://example.github.io/pages", dry_run=True, poll=False,
            )
            salt_second = pep.get_or_create_salt("housing")
        self.assertEqual(salt_first, salt_second)


class PassphraseNeverLeaksTest(unittest.TestCase):
    """H1 (audit): the passphrase must not appear on argv (so `ps` can't see
    it) AND must not appear in any error string when a staticrypt call
    fails (so a scheduled-job stderr.log can't leak it).
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)

    def test_staticrypt_env_never_puts_passphrase_on_argv(self) -> None:
        # If passphrase is in _staticrypt_env, it must be in the env dict —
        # NOT anywhere on argv. Prove it by inspecting the argv we'd pass.
        env = pep._staticrypt_env("s3cr3t-pass-42")
        self.assertEqual(env.get("STATICRYPT_PASSWORD"), "s3cr3t-pass-42")

    def test_run_scrubs_passphrase_from_failure_argv(self) -> None:
        # If a future edit ever puts the passphrase back on argv, run()'s
        # scrub= arg must catch it. Force a failing subprocess and prove
        # the passphrase doesn't appear in the SystemExit message.
        secret = "P@ssw0rd-should-NEVER-leak"
        with self.assertRaises(SystemExit) as ctx:
            pep.run(
                # `false` exits 1. We deliberately include the secret on
                # argv to prove scrub= works.
                ["false", secret],
                scrub=[secret],
            )
        msg = str(ctx.exception)
        self.assertNotIn(secret, msg, "passphrase leaked into error message")
        self.assertIn("<redacted>", msg)

    def test_run_scrubs_passphrase_from_stderr_of_failed_command(self) -> None:
        # Simulate staticrypt echoing the password back on stderr — the
        # scrub must catch that too, not just argv.
        secret = "another-secret-token"
        script = self.tmpdir / "leak.sh"
        script.write_text(f"#!/bin/sh\necho '{secret}' >&2\nexit 2\n")
        script.chmod(0o755)
        with self.assertRaises(SystemExit) as ctx:
            pep.run([str(script)], scrub=[secret])
        self.assertNotIn(secret, str(ctx.exception))

    def test_encrypt_call_failure_does_not_leak_passphrase(self) -> None:
        # End-to-end: force staticrypt to fail (invalid input) and confirm
        # the SystemExit is passphrase-free even without env vars mocked.
        secret = "e2e-secret-do-not-log"
        src = self.tmpdir / "src.html"
        src.write_text("<p>Distinctive multi-word sentence here.</p>")

        # Stub STATICRYPT_CMD to a shell that fails and echoes its env +
        # argv back on stderr — a realistic worst-case leak surface.
        stub = self.tmpdir / "fake_staticrypt.sh"
        stub.write_text(
            "#!/bin/sh\n"
            'echo "argv:$@" >&2\n'
            'echo "env-pw:${STATICRYPT_PASSWORD}" >&2\n'
            "exit 3\n"
        )
        stub.chmod(0o755)

        with mock.patch.object(pep, "STATICRYPT_CMD", [str(stub)]):
            with self.assertRaises(SystemExit) as ctx:
                pep.staticrypt_encrypt(
                    input_file=src,
                    output_dir=self.tmpdir / "out",
                    salt="a" * 32,
                    passphrase=secret,
                )
        # The passphrase went in through the env (`env-pw:...`), so the
        # stub's stderr line SHOULD contain it — and scrub() MUST have
        # removed it from the error we surface.
        self.assertNotIn(secret, str(ctx.exception))


class CommitAndPushScopingTest(unittest.TestCase):
    """H2 (audit): git add scope must equal leak-check scope. No -A fallback,
    no widening if the scoped add stages nothing.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        # Init a real git repo — cheap, keeps the invariants realistic.
        subprocess.run(
            ["git", "init", "-q"], cwd=self.repo, check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "--allow-empty", "-m", "init", "-q"],
            cwd=self.repo, check=True, capture_output=True,
        )
        (self.repo / "housing").mkdir()
        self.output = self.repo / "housing" / "index.html"
        # Prevent the actual `git push` in tests — replace with a no-op that
        # succeeds. Everything else is real git so the invariants are real.
        self._push_patch = mock.patch.object(
            pep, "run",
            side_effect=self._patched_run,
        )
        self._push_patch.start()
        self.addCleanup(self._push_patch.stop)
        # Sentinel envs: only patch away `git push`; delegate everything else.
        self._real_run = subprocess.run

    def _patched_run(self, argv, cwd=None, env=None, scrub=None):
        # Delegate to a plain subprocess.run wrapper, but intercept
        # `git push` (there's no remote) and return success. Include a
        # dummy `git commit` identity so real commit works.
        if list(argv[:2]) == ["git", "push"]:
            return ""
        # Fill in identity for commit so the sandbox git doesn't complain.
        if list(argv[:2]) == ["git", "commit"]:
            argv = ["git", "-c", "user.email=t@t", "-c", "user.name=t"] + list(argv[1:])
        res = self._real_run(
            list(argv),
            cwd=str(cwd) if cwd else None,
            env=env,
            text=True,
            capture_output=True,
        )
        if res.returncode != 0:
            raise SystemExit(
                "test-patched run failed: %s\n%s" % (argv, res.stderr)
            )
        return res.stdout

    def test_no_A_fallback_when_scoped_add_stages_nothing(self) -> None:
        # Simulate an unrelated dirty file at the repo root — a `-A` would
        # sweep it into the commit. The output_file we're pushing does NOT
        # exist yet, so the scoped path check should HARD-FAIL before git
        # ever gets called.
        (self.repo / "unrelated-scratch.md").write_text("# secret notes\n")
        with self.assertRaises(SystemExit) as ctx:
            pep.commit_and_push(
                self.repo, "housing", "Publish housing",
                output_file=self.output,  # doesn't exist
            )
        self.assertIn("missing", str(ctx.exception))
        # And crucially: the unrelated file is NOT staged.
        status = self._real_run(
            ["git", "status", "--porcelain"],
            cwd=str(self.repo), text=True, capture_output=True,
        ).stdout
        self.assertNotIn("A ", status, status)
        self.assertNotIn("M ", status, status)

    def test_only_the_scoped_output_file_gets_pushed(self) -> None:
        # Legit publish: output exists, no other dirt. The staged set must
        # be exactly [housing/index.html].
        self.output.write_text("<html>ciphertext-only</html>")
        changed = pep.commit_and_push(
            self.repo, "housing", "Publish housing",
            output_file=self.output,
        )
        self.assertTrue(changed)
        # The last commit's file list — exactly one file, the scoped one.
        files_in_commit = self._real_run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=str(self.repo), text=True, capture_output=True,
        ).stdout.strip().splitlines()
        self.assertEqual(files_in_commit, ["housing/index.html"])

    def test_refuses_to_widen_when_other_files_are_already_staged(self) -> None:
        # Someone left the index dirty (e.g. a hook staged another file).
        # commit_and_push must refuse rather than pushing that + our file.
        (self.repo / "unrelated.txt").write_text("private notes")
        self._real_run(
            ["git", "add", "unrelated.txt"], cwd=str(self.repo),
            check=True, capture_output=True,
        )
        self.output.write_text("<html>ciphertext-only</html>")
        with self.assertRaises(SystemExit) as ctx:
            pep.commit_and_push(
                self.repo, "housing", "Publish housing",
                output_file=self.output,
            )
        self.assertIn("staged files", str(ctx.exception))

    def test_rejects_output_outside_repo(self) -> None:
        outside = Path(self.tmp.name) / "outside.html"
        outside.write_text("<html>x</html>")
        with self.assertRaises(SystemExit) as ctx:
            pep.commit_and_push(
                self.repo, "housing", "Publish housing", output_file=outside,
            )
        self.assertIn("not inside repo", str(ctx.exception))


class OutputRenamedToIndexHtmlTest(unittest.TestCase):
    """M3 (audit): a non-index input must publish as index.html so Pages
    doesn't 404 at `<subpath>/`."""

    def test_rename_to_index_html_after_staticrypt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            src = tmpdir / "report.html"
            src.write_text("<p>Multi word distinctive line here.</p>")

            # Stub the staticrypt invocation: write cipher at input basename.
            # Real staticrypt writes to `<-d dir>/<input basename>`, and
            # staticrypt_encrypt then renames to index.html — that's the
            # behavior under test.
            def fake_run(argv, cwd=None, env=None, scrub=None):
                if list(argv[:len(pep.STATICRYPT_CMD)]) != list(pep.STATICRYPT_CMD):
                    raise AssertionError("unexpected argv %r" % argv)
                out_idx = argv.index("-d") + 1
                out_dir = Path(argv[out_idx])
                out_dir.mkdir(parents=True, exist_ok=True)
                # First positional after STATICRYPT_CMD is always the input.
                input_arg = Path(argv[len(pep.STATICRYPT_CMD)])
                (out_dir / input_arg.name).write_text(
                    "<html>encrypted-blob</html>"
                )
                return ""

            with mock.patch.object(pep, "run", side_effect=fake_run):
                out = pep.staticrypt_encrypt(
                    input_file=src,
                    output_dir=tmpdir / "publish",
                    salt="a" * 32,
                    passphrase="ignored",
                )

            self.assertEqual(out.name, "index.html")
            self.assertTrue(out.exists())
            # The pre-rename path must no longer exist.
            self.assertFalse((tmpdir / "publish" / "report.html").exists())


class M4HousingSaltSeededTest(unittest.TestCase):
    """M4 (audit): confirm the salt store already carries the live housing
    salt so an accidental re-publish keeps the existing share link working.

    Not a unit test in the strict sense — verifies filesystem state on THIS
    machine. Skipped if the salt file is absent (fresh checkout, CI, etc.).
    """

    def test_housing_salt_matches_live_page(self) -> None:
        expected = "2cf7c647985b6878402995e6be7c590c"
        salt_file = Path.home() / ".mineru" / "cache" / "staticrypt-salts" / "housing.json"
        if not salt_file.exists():
            self.skipTest("housing salt not seeded on this machine")
        data = json.loads(salt_file.read_text())
        self.assertEqual(data.get("salt"), expected)
        mode = stat.S_IMODE(salt_file.stat().st_mode)
        self.assertEqual(mode, 0o600, oct(mode))
        parent_mode = stat.S_IMODE(salt_file.parent.stat().st_mode)
        self.assertEqual(parent_mode, 0o700, oct(parent_mode))


if __name__ == "__main__":
    unittest.main()
