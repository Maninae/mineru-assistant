#!/usr/bin/env python3
"""Fill 1Password credentials directly into the Mineru browser, bypassing the agent.

The ONLY sanctioned path for website credentials during a remote re-login
(mineru__browser-checkpoints "Sessions & logins"). The secret's entire journey:
macOS Keychain (service-account token) -> op subprocess (env) -> this process's
memory -> HTTP fill on 127.0.0.1:$MINERU_BROWSER_PORT (bearer-authed) -> the form field. It must never appear in
argv, stdout, tool transcripts, logs, or files.

Usage (agent runs this; its stdout/stderr are safe to read):
    op-relogin-fill.py --vault <vault> --item "Warehouse Store" --target tab_abc123 \
        --username-ref e4 --password-ref e6 [--otp-ref e9] [--submit-ref e7]
    op-relogin-fill.py --test-value plumbing-test --target tab_x --password-ref e6

Security invariants (verified by the SECURITY.md fresh-context audit):
- Secrets are never CLI arguments (op reads via env token; fill goes over localhost POST).
- Fill requests set redact=true, so neither the server response nor the server logs echo the value.
- Domain binding: before filling, the target tab's live URL must match a domain saved on the
  1Password item itself (the trusted source). This blocks an injected agent from filling a real
  password into an attacker page. The agent does NOT get to assert the domain.
- Every error path scrubs every fetched secret in BOTH raw and JSON-escaped form before printing.
- --test-value skips 1Password and the domain check entirely; it exists to test plumbing with NON-secrets.
- Requires the read-only service-account token in Keychain (service $MINERU_OP_KEYCHAIN_SERVICE,
  default "op-service-account-token"; account $MINERU_KEYCHAIN_ACCOUNT, default "mineru"),
  scoped to a single vault; fails loud if absent.
- The vault comes from --vault or $MINERU_OP_VAULT; there is no built-in default.
- Every browser-server request carries the per-boot bearer token from
  $MINERU_HOME/cache/browser-server.token, exactly as bin/browser does.

Note on process env (accepted risk, see SECURITY.md): the token is passed to `op` via
OP_SERVICE_ACCOUNT_TOKEN (1Password's documented mechanism). A same-user process could read it
via `ps -E` during op's brief run, but same-user compromise already implies Keychain access.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Set

KEYCHAIN_SERVICE = os.environ.get("MINERU_OP_KEYCHAIN_SERVICE", "op-service-account-token")
KEYCHAIN_ACCOUNT = os.environ.get("MINERU_KEYCHAIN_ACCOUNT", "mineru")
# Absolute paths only — never a bare "op" resolved off the agent's PATH (which
# includes user-writable dirs); a planted binary would receive the vault token.
OP_BINARY_CANDIDATES = ["/opt/homebrew/bin/op", "/usr/local/bin/op"]
BROWSER_PORT = int(os.environ.get("MINERU_BROWSER_PORT", "9471"))
BROWSER_ACTION_URL = "http://127.0.0.1:%d/action" % BROWSER_PORT
BROWSER_TABS_URL = "http://127.0.0.1:%d/tabs" % BROWSER_PORT
VAULT_ENV_VAR = "MINERU_OP_VAULT"
SUBPROCESS_TIMEOUT_SECONDS = 30
HTTP_TIMEOUT_SECONDS = 30


def browser_token_path() -> str:
    """Path of the browser server's per-boot bearer token (same rule as bin/browser)."""
    home = os.environ.get("MINERU_HOME") or os.path.join(os.path.expanduser("~"), ".mineru")
    return os.path.join(home, "cache", "browser-server.token")


def browser_auth_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Headers for a browser-server request, with `Authorization: Bearer <token>`.

    Fails loud when the token file is missing: the server would 401 anyway, and a
    clear path to check beats an opaque HTTP error mid-fill.
    """
    try:
        with open(browser_token_path(), "rb") as token_file:
            token = token_file.read().decode("utf-8", errors="replace").strip()
    except OSError:
        token = ""
    if not token:
        raise RuntimeError(
            "no browser-server bearer token at %s (is the browser server running?)"
            % browser_token_path()
        )
    headers = dict(extra or {})
    headers["Authorization"] = "Bearer %s" % token
    return headers


def find_op_binary() -> str:
    """Locate the 1Password CLI at a known absolute path. Fail loud if absent."""
    for candidate in OP_BINARY_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    raise RuntimeError(
        "1Password CLI (op) not found at %s" % " or ".join(OP_BINARY_CANDIDATES)
    )


def read_service_account_token() -> str:
    """Read the scoped service-account token from macOS Keychain.

    Fails loud if the entry is missing (setup incomplete) rather than degrading
    into a confusing op auth error downstream.
    """
    result = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
         "-a", KEYCHAIN_ACCOUNT, "-w"],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            "service-account token not in Keychain (service=%s account=%s). "
            "Finish the 1Password service-account setup first."
            % (KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
        )
    return result.stdout.strip()


def run_op(op_binary: str, token: str, op_args: List[str]) -> str:
    """Run an op subcommand with the token via env, return stdout with only the trailing newline stripped."""
    env = dict(os.environ)
    env["OP_SERVICE_ACCOUNT_TOKEN"] = token
    result = subprocess.run(
        [op_binary] + op_args,
        capture_output=True, text=True, env=env, timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip().replace(token, "<token>")
        raise RuntimeError("op %s failed: %s" % (op_args[0] if op_args else "?", stderr))
    # rstrip only the newline op appends — preserve any legitimate leading/trailing
    # whitespace that is part of the credential itself (L2).
    return result.stdout.rstrip("\n")


def host_of(url: str) -> str:
    """Extract the lowercase hostname from a URL, empty string if unparseable.

    `urlparse().hostname` already lowercases, strips any port, drops a trailing
    dot's port ambiguity, and — critically — returns the host AFTER any `user@`
    userinfo, so `https://shopmart.com@evil.com/` yields `evil.com` (fails closed).
    A scheme-less item URL like `shopmart.com` parses as a path, so retry with a
    `//` prefix to recover the host.
    """
    try:
        host = urllib.parse.urlparse(url).hostname
        if not host and url and "://" not in url:
            host = urllib.parse.urlparse("//" + url).hostname
        host = (host or "").strip().lower().rstrip(".")
        # Reject junk that isn't a real hostname (whitespace, no label) so it can't
        # become a bogus match base; a real URL's hostname never contains a space.
        if not host or " " in host:
            return ""
        return host
    except ValueError:
        return ""


def match_bases(item_hosts: List[str]) -> Set[str]:
    """Acceptable login hosts, derived from the item's OWN saved hosts.

    We match the tab host against the item's actual hostnames (exact or subdomain),
    never a guessed eTLD+1 — so a shared public suffix (co.uk, github.io, com.au) or
    a shared IP octet can never make two unrelated domains compare equal. Each saved
    host contributes itself, plus its `www.`-stripped form when that still leaves a
    multi-label domain (so an item saved as `www.shopmart.com` also accepts sibling
    login subdomains like `signin.shopmart.com`).
    """
    bases = set()  # type: Set[str]
    for host in item_hosts:
        host = (host or "").lower().rstrip(".")
        if not host:
            continue
        bases.add(host)
        if host.startswith("www.") and host.count(".") >= 2:
            bases.add(host[len("www."):])
    return bases


def host_allowed(tab_host: str, bases: Set[str]) -> bool:
    """True iff the tab host equals, or is a subdomain of, one of the item's bases."""
    tab_host = (tab_host or "").lower().rstrip(".")
    if not tab_host:
        return False
    return any(tab_host == base or tab_host.endswith("." + base) for base in bases)


def get_tab_url(target_id: str) -> str:
    """Read the target tab's live URL from the browser server (GET /tabs)."""
    request = urllib.request.Request(BROWSER_TABS_URL, headers=browser_auth_headers())
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        data = json.loads(response.read().decode("utf-8"))
    for tab in data.get("tabs", []):
        if tab.get("targetId") == target_id:
            return tab.get("url", "")
    raise RuntimeError("target tab %s not found on the browser server" % target_id)


def enforce_domain_binding(item: str, item_urls: List[str], target_id: str) -> None:
    """Refuse to fill unless the tab's live domain matches a domain saved on the item.

    The item's own URLs are the trusted anchor; the agent cannot assert the domain.
    Fails closed: an item with no saved URL, or a mismatch, raises (which the caller
    surfaces to the operator as a checkpoint rather than filling blindly).
    """
    bases = match_bases([host_of(u) for u in item_urls])
    if not bases:
        raise RuntimeError(
            "item '%s' has no saved website URL to verify the login page against; "
            "refusing to fill. Add the login URL to the 1Password item." % item
        )
    tab_url = get_tab_url(target_id)
    tab_host = host_of(tab_url)
    if not host_allowed(tab_host, bases):
        raise RuntimeError(
            "refusing to fill: login page host '%s' (%s) is not the item's saved site "
            "or a subdomain of it %s. If this is a legitimate login/SSO host, add its "
            "URL to the 1Password item." % (tab_host, tab_url, sorted(bases))
        )


def browser_fill(target_id: str, ref: str, secret: str) -> None:
    """POST a redacted fill action to the local browser server.

    redact=true makes the server omit the value from its response and logs; any
    error string is additionally decoded and scrubbed here (raw + JSON-escaped)
    before it can propagate to output.
    """
    payload = json.dumps({
        "action": "act", "targetId": target_id, "kind": "fill",
        "ref": ref, "text": secret, "redact": True,
    }).encode("utf-8")
    request = urllib.request.Request(
        BROWSER_ACTION_URL, data=payload,
        headers=browser_auth_headers({"Content-Type": "application/json"}),
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as http_error:
        raw = http_error.read().decode("utf-8", "replace")
        raise RuntimeError("browser fill on %s failed (HTTP %d): %s"
                           % (ref, http_error.code, scrub(raw, secret)))
    if body.get("error"):
        raise RuntimeError("browser fill on %s failed: %s"
                           % (ref, scrub(str(body["error"]), secret)))


def browser_click(target_id: str, ref: str) -> None:
    """POST a plain click (used for the submit button; no secret involved)."""
    payload = json.dumps({
        "action": "act", "targetId": target_id, "kind": "click", "ref": ref,
    }).encode("utf-8")
    request = urllib.request.Request(
        BROWSER_ACTION_URL, data=payload,
        headers=browser_auth_headers({"Content-Type": "application/json"}),
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        body = json.loads(response.read().decode("utf-8"))
    if body.get("error"):
        raise RuntimeError("submit click on %s failed: %s" % (ref, body["error"]))


def scrub(message: str, secret: str) -> str:
    """Remove a secret (raw and JSON-escaped forms) from a string. No-op if empty (L3)."""
    if not secret:
        return message
    message = message.replace(secret, "***")
    escaped = json.dumps(secret)[1:-1]  # how the secret appears once JSON-serialized in transit
    if escaped != secret:
        message = message.replace(escaped, "***")
    return message


def main() -> int:
    """Fetch requested fields (with domain binding) and fill them; print only sanitized statuses."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--item", help="1Password item name (required unless --test-value)")
    parser.add_argument("--vault", default=os.environ.get(VAULT_ENV_VAR),
                        help="1Password vault holding the item (default: $%s)" % VAULT_ENV_VAR)
    parser.add_argument("--target", required=True, help="browser targetId (tab_...)")
    parser.add_argument("--username-ref", help="snapshot ref of the username field")
    parser.add_argument("--password-ref", help="snapshot ref of the password field")
    parser.add_argument("--otp-ref", help="snapshot ref of the one-time-code field")
    parser.add_argument("--submit-ref",
                        help="snapshot ref of the submit button; clicked right after the last "
                             "fill so the filled DOM is never left exposed for readback")
    parser.add_argument("--test-value",
                        help="TEST ONLY: fill this literal non-secret into the given refs, "
                             "skipping 1Password and the domain check")
    args = parser.parse_args()

    ref_plan = [("username", args.username_ref),
                ("password", args.password_ref),
                ("otp", args.otp_ref)]
    ref_plan = [(field, ref) for field, ref in ref_plan if ref]
    if not ref_plan:
        parser.error("at least one of --username-ref/--password-ref/--otp-ref is required")
    if not args.test_value and not args.item:
        parser.error("--item is required unless --test-value is given")
    if not args.test_value and not args.vault:
        parser.error("--vault (or $%s) is required unless --test-value is given" % VAULT_ENV_VAR)

    secrets_fetched = []  # type: List[str]  # every fetched value, for scrubbing error output
    try:
        if args.test_value:
            values = {field: args.test_value for field, _ in ref_plan}
        else:
            op_binary = find_op_binary()
            token = read_service_account_token()
            secrets_fetched.append(token)

            # One item-get pulls URLs (for the domain check) + username/password in one call.
            item_json = run_op(op_binary, token,
                               ["item", "get", args.item, "--vault", args.vault, "--format", "json"])
            item = json.loads(item_json)
            item_urls = [u.get("href", "") for u in item.get("urls", []) if u.get("href")]

            fields = item.get("fields", [])
            by_purpose = {}  # type: Dict[str, str]
            for f in fields:
                purpose = f.get("purpose")
                if purpose and f.get("value") is not None:
                    by_purpose[purpose] = f.get("value")

            values = {}  # type: Dict[str, str]
            for field, _ in ref_plan:
                if field == "username":
                    values[field] = by_purpose.get("USERNAME", "")
                elif field == "password":
                    values[field] = by_purpose.get("PASSWORD", "")
                elif field == "otp":
                    values[field] = run_op(op_binary, token,
                                          ["item", "get", args.item, "--vault", args.vault, "--otp"])
                if values.get(field):
                    secrets_fetched.append(values[field])

            missing = [field for field, _ in ref_plan if not values.get(field)]
            if missing:
                raise RuntimeError("item '%s' has no value for: %s" % (args.item, ", ".join(missing)))

            # Domain binding — trusted item URLs vs the tab's live URL. Fails closed.
            enforce_domain_binding(args.item, item_urls, args.target)

        for field, ref in ref_plan:
            browser_fill(args.target, ref, values[field])
            print("filled %s into %s: ok" % (field, ref))  # no length — avoid disclosing it

        if args.submit_ref:
            browser_click(args.target, args.submit_ref)
            print("clicked submit (%s): ok" % args.submit_ref)
        return 0
    except Exception as error:  # scrub every fetched secret before the message can reach output
        message = str(error)
        for secret in secrets_fetched:
            message = scrub(message, secret)
        print("ERROR: %s" % message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
