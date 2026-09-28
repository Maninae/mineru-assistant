#!/usr/bin/env python3
"""Tests for the read-path allowlist and traversal defenses.

Three surfaces all sit on the same discipline: resolve the untrusted path,
verify the resolved absolute path stays under a known root, and never let a
structurally impossible input raise its way out as a 500.

- `http_helpers.is_path_inside_allowlist` — the last-mile check every route
  runs before it opens a file.
- `feeds.resolve_brief_path` — feed-scoped resolver called from
  `/api/brief/<feed>/<filename>`.
- `library.resolve_library_path` — library-scoped resolver called from every
  `/api/library`, `/raw/library`, and `/sandbox/library` route.

Traversal payloads tested: `..`, absolute paths, embedded NUL bytes,
percent-encoded parent segments after url-unquote, a symlink whose target
escapes the root, and NAME_MAX / ENAMETOOLONG segments. All must degrade to
None or raise `ValueError` — never resolve outside the root, and never leak
a stack trace.

Run: python3 -m pytest tests/test_path_allowlist.py -q
     python3 tests/test_path_allowlist.py
"""

import os
import sys
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _app_test_setup import SyntheticWorkspace   # noqa: E402

import feeds   # noqa: E402
import http_helpers   # noqa: E402
import library   # noqa: E402


class IsPathInsideAllowlistTest(unittest.TestCase):
    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_path_under_allowed_root_returns_true(self):
        target = self.ws.write_brief("alpha", "in-root.md", "# ok\n", mtime=100.0)
        self.assertTrue(http_helpers.is_path_inside_allowlist(target))

    def test_path_outside_all_roots_returns_false(self):
        outside = self.ws.root / "outside.md"
        outside.write_text("x", encoding="utf-8")
        self.assertFalse(http_helpers.is_path_inside_allowlist(outside))

    def test_symlink_escape_returns_false(self):
        # A symlink inside the allowlist that points OUTSIDE must fail the check:
        # Path.resolve() follows the link, and the resolved target lives outside
        # every allowlisted root.
        outside_target = self.ws.root / "secret-outside.md"
        outside_target.write_text("secret", encoding="utf-8")
        link_inside_alpha = self.ws.root / "briefs_alpha" / "escape.md"
        try:
            os.symlink(str(outside_target), str(link_inside_alpha))
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not supported here")
        self.assertFalse(http_helpers.is_path_inside_allowlist(link_inside_alpha))


class ResolveBriefPathTest(unittest.TestCase):
    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_valid_brief_resolves(self):
        self.ws.write_brief("alpha", "morning-2026-08-17.md", "# ok\n", mtime=100.0)
        resolved = feeds.resolve_brief_path("alpha", "morning-2026-08-17.md")
        self.assertIsNotNone(resolved)
        self.assertTrue(resolved.name == "morning-2026-08-17.md")

    def test_unknown_feed_returns_none(self):
        self.assertIsNone(feeds.resolve_brief_path("no-such-feed", "any.md"))

    def test_parent_traversal_rejects(self):
        # `../` should never let a caller reach out of the feed dir.
        # Path.resolve() collapses it; the relative_to(feed_dir) then fails.
        self.assertIsNone(feeds.resolve_brief_path("alpha", "../etc/passwd"))
        self.assertIsNone(feeds.resolve_brief_path("alpha", "..//briefs_beta/foo.md"))

    def test_absolute_path_rejects(self):
        # An absolute-looking filename: Path("/etc/passwd") joined onto feed_dir
        # replaces feed_dir entirely — the relative_to check must catch it.
        self.assertIsNone(feeds.resolve_brief_path("alpha", "/etc/passwd"))

    def test_embedded_nul_rejects(self):
        # Pathlib raises ValueError on NUL in strings; the caller must swallow
        # that into a clean None (never a 500).
        self.assertIsNone(feeds.resolve_brief_path("alpha", "morn\x00.md"))

    def test_backslash_treated_literally_not_as_separator(self):
        # Windows separators must not open an escape path on POSIX.
        # Creating a file literally called "..\\..\\etc\\passwd" is legal;
        # the resolver treats it as a single filename inside the feed dir.
        weird_name = "..\\..\\etc\\passwd"
        # As long as no such file exists, the resolver returns None (not-found).
        self.assertIsNone(feeds.resolve_brief_path("alpha", weird_name))

    def test_percent_encoded_dotdot_after_unquote_rejects(self):
        # Simulate the handler path: unquote first, then resolve.
        raw = "%2e%2e/secret.md"
        unquoted = urllib.parse.unquote(raw)
        self.assertEqual(unquoted, "../secret.md")
        self.assertIsNone(feeds.resolve_brief_path("alpha", unquoted))

    def test_name_too_long_returns_none_not_raise(self):
        # macOS NAME_MAX = 255 bytes; a segment past that makes every FS syscall
        # raise ENAMETOOLONG. The resolver must swallow that into a clean None.
        too_long = ("a" * 300) + ".md"
        self.assertIsNone(feeds.resolve_brief_path("alpha", too_long))


class ResolveLibraryPathTest(unittest.TestCase):
    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_valid_relpath_resolves(self):
        self.ws.write_library_file("reports", "2026/summary.md", "# ok\n")
        resolved = library.resolve_library_path("reports", "2026/summary.md")
        self.assertTrue(str(resolved).endswith("reports/2026/summary.md"))

    def test_empty_relpath_returns_source_root(self):
        got = library.resolve_library_path("reports", "")
        expected = (self.ws.root / "reports").resolve()
        self.assertEqual(got, expected)

    def test_unknown_source_raises(self):
        with self.assertRaises(ValueError):
            library.resolve_library_path("no-such-source", "x.md")

    def test_absolute_relpath_stripped_and_stays_in_root(self):
        # The resolver strips a leading `/` and joins under the source root
        # instead of raising. What matters is that it never escapes.
        # `/etc/passwd` -> `<reports>/etc/passwd`, still inside the root.
        source_root = (self.ws.root / "reports").resolve()
        resolved = library.resolve_library_path("reports", "/etc/passwd")
        # relative_to must succeed — proving no escape.
        resolved.relative_to(source_root)

    def test_parent_traversal_raises(self):
        with self.assertRaises(ValueError):
            library.resolve_library_path("reports", "../secret.md")
        with self.assertRaises(ValueError):
            library.resolve_library_path("reports", "sub/../../secret.md")

    def test_percent_encoded_dotdot_raises_after_unquote(self):
        raw = "%2e%2e/secret.md"
        unquoted = urllib.parse.unquote(raw)
        with self.assertRaises(ValueError):
            library.resolve_library_path("reports", unquoted)

    def test_symlink_escape_raises(self):
        # A symlink INSIDE the reports source that points OUTSIDE must fail.
        outside_target = self.ws.root / "secret-outside.md"
        outside_target.write_text("secret", encoding="utf-8")
        link = self.ws.root / "reports" / "escape.md"
        try:
            os.symlink(str(outside_target), str(link))
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not supported here")
        with self.assertRaises(ValueError):
            library.resolve_library_path("reports", "escape.md")

    def test_name_too_long_downstream_list_directory_returns_404_not_500(self):
        # `.resolve()` on a 300-char segment is a pure string op on 3.9 and
        # does NOT raise; the ENAMETOOLONG surfaces on the next FS syscall.
        # `list_directory` catches that and re-raises FileNotFoundError so the
        # handler can 404 the caller instead of 500ing.
        long_seg = "a" * 300
        with self.assertRaises(FileNotFoundError):
            library.list_directory("reports", long_seg)


class SafeServeFileExtensionAllowlistTest(unittest.TestCase):
    """`safe_serve_file` refuses extensions outside `ALLOWED_EXTENSIONS`."""

    def setUp(self):
        self.ws = SyntheticWorkspace()
        self.ws.setup()

    def tearDown(self):
        self.ws.teardown()

    def test_allowed_extension_served(self):
        target = self.ws.write_library_file("reports", "ok.md", "# ok\n")
        status, headers, body = http_helpers.safe_serve_file(target)
        self.assertEqual(status, 200)
        self.assertIn("Content-Type", headers)

    def test_disallowed_extension_404s(self):
        # .py is not in ALLOWED_EXTENSIONS; the extension check must trip.
        target = self.ws.root / "reports" / "config.py"
        target.write_text("import os\n", encoding="utf-8")
        status, _, _ = http_helpers.safe_serve_file(target)
        self.assertEqual(status, 404)

    def test_path_outside_allowlist_404s(self):
        outside = self.ws.root / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        status, _, _ = http_helpers.safe_serve_file(outside)
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
