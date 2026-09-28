#!/usr/bin/env python3
"""Security invariants for the 1Password re-login credential fill.

Guards two properties that a security audit established and that must never
regress (SECURITY.md §6):

1. **Domain binding** (`op-relogin-fill.py`): a credential is filled only when the
   tab's live host equals, or is a subdomain of, a host saved on the 1Password item
   itself — never a guessed eTLD+1. Every classic bypass (prefix/suffix append,
   userinfo `@`, unrelated host, IP mismatch, multi-part-suffix sibling like
   `evil.co.uk` vs `tesco.co.uk`) must fail closed; legitimate exact + sibling
   subdomains must be accepted.
2. **Log scrubbing** (`browser/actions.scrub_secret_from_text`): a filled value must
   never survive in a log line or error, in either its raw form or its JSON-escaped
   form (Playwright embeds it as `fill("...")`, later JSON-serialized). The redaction
   marker is fixed-width (no length disclosure).

Pure-function tests — no browser, no 1Password, no network.

Run: python3 -m pytest tests/test_op_relogin_fill.py -q
     python3 tests/test_op_relogin_fill.py
"""

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

MINERU_ROOT = Path(__file__).resolve().parent.parent
if str(MINERU_ROOT) not in sys.path:
    sys.path.insert(0, str(MINERU_ROOT))

from browser.actions import scrub_secret_from_text  # noqa: E402


def load_fill_module():
    """Import op-relogin-fill.py by path (its hyphenated name isn't importable)."""
    path = MINERU_ROOT / "scripts" / "op-relogin-fill.py"
    spec = importlib.util.spec_from_file_location("op_relogin_fill", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


orf = load_fill_module()


def domain_allowed(item_urls, tab_url):
    """Mirror enforce_domain_binding's decision without the HTTP /tabs round-trip."""
    bases = orf.match_bases([orf.host_of(u) for u in item_urls])
    if not bases:
        return "NO_BASES"  # fails closed: enforce_domain_binding raises "no saved URL"
    return orf.host_allowed(orf.host_of(tab_url), bases)


class TestDomainBinding(unittest.TestCase):
    """Realistic store logins pass; every attacker trick fails closed."""

    def test_legitimate_logins_allowed(self):
        cases = [
            (["https://www.shopmart.com/"], "https://www.shopmart.com/account"),      # exact
            (["https://www.shopmart.com/"], "https://signin.shopmart.com/login"),     # sibling via www-strip
            (["https://shopmart.com/"], "https://signin.shopmart.com/login"),         # subdomain of bare apex
            (["https://www.homedepot.com/"], "https://www.homedepot.com/auth"),
            (["https://www.tesco.co.uk/"], "https://secure.tesco.co.uk/account"), # multi-part suffix, real subdomain
            (["https://www.shopmart.com/", "https://shopmartauth.onmicrosoft.com/"],  # saved IdP host
             "https://shopmartauth.onmicrosoft.com/x"),
            (["shopmart.com"], "https://signin.shopmart.com/x"),                      # scheme-less item URL
            (["https://www.shopmart.com/"], "https://www.shopmart.com:8443/x"),       # port stripped
            (["https://www.shopmart.com/"], "https://www.shopmart.com./"),            # trailing dot
            (["https://www.Shopmart.com/"], "https://WWW.SHOPMART.COM/"),             # case-insensitive
        ]
        for item_urls, tab_url in cases:
            self.assertIs(domain_allowed(item_urls, tab_url), True,
                          msg="should ALLOW item=%s tab=%s" % (item_urls, tab_url))

    def test_attacker_hosts_refused(self):
        cases = [
            (["https://www.shopmart.com/"], "https://attacker.com/phish"),          # unrelated
            (["https://shopmart.com/"], "https://evilshopmart.com/"),                 # prefix trick
            (["https://shopmart.com/"], "https://shopmart.com.evil.com/"),            # suffix-append
            (["https://www.shopmart.com/"], "https://shopmart.com@evil.com/"),        # userinfo -> evil.com
            (["https://www.shopmart.com/"], "https://shopmart.com%00.evil.com/"),     # null-byte
            (["https://www.tesco.co.uk/"], "https://evil.co.uk/"),                # multi-part suffix sibling (M1)
            (["https://alice.github.io/"], "https://evil.github.io/"),            # multi-tenant apex sibling
            (["http://10.0.1.1/"], "http://192.168.9.1/"),                        # IP mismatch (M1 collapse gone)
            (["https://www.xn--80ak6aa92e.com/"], "https://apple.com/"),          # punycode mismatch
        ]
        for item_urls, tab_url in cases:
            self.assertIs(domain_allowed(item_urls, tab_url), False,
                          msg="should REFUSE item=%s tab=%s" % (item_urls, tab_url))

    def test_no_usable_item_url_fails_closed(self):
        for item_urls in ([], [""], ["   "], ["not a url with spaces"]):
            self.assertEqual(domain_allowed(item_urls, "https://www.shopmart.com/"), "NO_BASES",
                             msg="empty/junk item URLs must fail closed: %s" % item_urls)

    def test_www_strip_never_reduces_to_single_label(self):
        # www.com must NOT yield base "com" (which would match any *.com).
        bases = orf.match_bases([orf.host_of("https://www.com/")])
        self.assertNotIn("com", bases)
        self.assertFalse(orf.host_allowed(orf.host_of("https://anything.com/"), bases))


class TestLogScrubbing(unittest.TestCase):
    """A filled value never survives a log line, raw or JSON-escaped."""

    SECRET = 'p@ss"w\\ord'  # contains BOTH a double-quote and a backslash

    def test_raw_playwright_calllog_scrubbed(self):
        raw = 'Locator.fill: Error ... - fill("%s")\n  - attempting fill' % self.SECRET
        out = scrub_secret_from_text(raw, self.SECRET)
        self.assertNotIn(self.SECRET, out)
        self.assertNotIn("p@ss", out)
        self.assertIn("<redacted>", out)

    def test_json_serialized_body_scrubbed(self):
        # Exactly what the client reads back: the raw error JSON-serialized into a body.
        raw = 'Internal error: ... - fill("%s")' % self.SECRET
        body = json.dumps({"error": raw})
        out = scrub_secret_from_text(body, self.SECRET)
        self.assertNotIn(self.SECRET, out)
        self.assertNotIn(json.dumps(self.SECRET)[1:-1], out)  # JSON-escaped form gone too
        self.assertNotIn("p@ss", out)

    def test_marker_is_fixed_width_no_length_disclosure(self):
        out = scrub_secret_from_text("x %s y" % self.SECRET, self.SECRET)
        self.assertIn("<redacted>", out)
        self.assertNotIn("chars>", out)

    def test_empty_secret_is_noop(self):
        self.assertEqual(scrub_secret_from_text("nothing to scrub", ""), "nothing to scrub")

    def test_idempotent(self):
        once = scrub_secret_from_text("fill(\"%s\")" % self.SECRET, self.SECRET)
        twice = scrub_secret_from_text(once, self.SECRET)
        self.assertEqual(once, twice)


class TestBrowserAuthAndVault(unittest.TestCase):
    """Fills carry the engine's bearer token; the vault has no operator default."""

    def test_bearer_header_read_from_mineru_home(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "cache"))
            with open(os.path.join(home, "cache", "browser-server.token"), "w") as f:
                f.write("tok123\n")
            with mock.patch.dict(os.environ, {"MINERU_HOME": home}):
                headers = orf.browser_auth_headers({"Content-Type": "application/json"})
        self.assertEqual(headers["Authorization"], "Bearer tok123")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_missing_token_fails_loud(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"MINERU_HOME": home}):
                with self.assertRaises(RuntimeError):
                    orf.browser_auth_headers()

    def test_vault_required_without_env(self):
        from unittest import mock
        argv = ["op-relogin-fill.py", "--item", "Store", "--target", "tab_x", "--password-ref", "e1"]
        env = {k: v for k, v in os.environ.items() if k != orf.VAULT_ENV_VAR}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as ctx:
                orf.main()
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
