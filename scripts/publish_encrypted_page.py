#!/usr/bin/env python3
"""Publish a single HTML file as a StatiCrypt-encrypted, password-gated page
to a public GitHub Pages repo you own, and print a stable share link
whose URL fragment is safe to hand to a viewer.

Why this exists:

  We keep publishing "here is a private thing, share it via a passphrase"
  pages (private family pages, reading collections, ad-hoc reports…). Doing it by hand caught a
  cluster of specific traps three separate times. This helper bakes those in:

  1. StatiCrypt --share does NOT write output files. It only prints a link.
     So we invoke staticrypt TWICE:
       (a) encrypt+write:  staticrypt <in> -d <outdir> -s <salt> -p <pass>
       (b) get share link: staticrypt <in> -s <salt> --share <baseurl> -p <pass>
     Doing it in one call misses either the file or the link.

  2. The plaintext-leak grep must NOT check short letter-strings like "abc".
     Base64 ciphertext contains every 3-letter combo by chance, so those
     produce constant false positives. The correct check greps for
     distinctive SPACE-containing PHRASES pulled from the source text —
     base64 has no spaces, so a hit means real leakage. And a MISSING output
     file must count as failure, not "clean" (the empty read hits no needles).

  3. Re-publishing must reuse the SAME salt AND passphrase. StatiCrypt's
     share link contains a hash of {passphrase, salt}; a fresh salt on every
     run breaks every previously-shared link. So we persist the salt per-page
     in a private config file at $MINERU_HOME/cache/staticrypt-salts/<slug>.json
     (mode 0600). First run generates one, later runs reuse it.

  Then commit + push the ciphertext-only pages repo, poll the live URL
  with `curl --retry` until it 200s, and print the share link.

The pages repo (--repo or $MINERU_PUBLISH_REPO) and the URL that serves it
(--share-base or $MINERU_PUBLISH_SHARE_BASE) are required; there are no
built-in defaults. The repo is public but only ever contains ciphertext. This
helper never writes plaintext there.

Runs on macOS system Python 3.9. Uses only stdlib + subprocess + StatiCrypt
(`npx -y staticrypt`, v3.5.x).
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence

REPO_ENV_VAR = "MINERU_PUBLISH_REPO"
SHARE_BASE_ENV_VAR = "MINERU_PUBLISH_SHARE_BASE"
MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
SALTS_DIR = MINERU_HOME / "cache" / "staticrypt-salts"
STATICRYPT_CMD = ["npx", "-y", "staticrypt"]
# curl --retry retries transient network failures; --retry-all-errors makes it
# retry non-2xx responses too (needed while Pages is still deploying).
CURL_POLL_CMD_TEMPLATE = [
    "curl", "-fsS",
    "--retry", "30",
    "--retry-delay", "5",
    "--retry-all-errors",
    "--max-time", "10",
    "-o", "/dev/null",
    "-w", "%{http_code}",
]

# Minimum distinctive-phrase length (in chars) for the leak check. Short
# phrases still get consumed by base64 chance-hits; ~12+ chars with a space
# is well past that threshold in practice.
MIN_LEAK_PHRASE_CHARS = 12
# How many distinctive phrases to check. Twenty is plenty — a real leak
# will hit dozens; we stop at the first mismatch anyway.
LEAK_PHRASE_SAMPLE_SIZE = 40


# ---------------------------------------------------------------------------
# Small subprocess helpers
# ---------------------------------------------------------------------------


def run(argv: Sequence[str], cwd: Optional[Path] = None,
        env: Optional[dict] = None,
        scrub: Optional[Sequence[str]] = None) -> str:
    """Run a subprocess and return stdout. Exit on any nonzero return code
    with the child's stderr surfaced so failures land as visible errors
    rather than silent no-ops (the trap the original had).

    `scrub` is a list of substrings that MUST NOT appear in the SystemExit
    message on failure — they're replaced with `<redacted>` before any argv,
    stderr, or stdout gets echoed. Callers pass the passphrase here (belt-
    and-suspenders alongside using STATICRYPT_PASSWORD env var) so a future
    accidental `-p <pass>` doesn't leak into logs or a scheduled-job
    stderr.log. Empty/None entries are ignored so callers can pass a raw
    passphrase variable that might legitimately be empty in a test path.

    stdin is pinned to DEVNULL — a child prompting for confirmation (e.g.
    StatiCrypt's short-password prompt) must not hang the pipeline waiting
    for a keypress that will never come.
    """
    res = subprocess.run(
        list(argv), cwd=str(cwd) if cwd else None, env=env,
        text=True, capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    if res.returncode != 0:
        stderr_trim = (res.stderr or "").strip()
        stdout_trim = (res.stdout or "").strip()
        argv_str = " ".join(map(str, argv))
        message = (
            "command failed (%d): %s\nstderr: %s\nstdout: %s"
            % (res.returncode, argv_str, stderr_trim, stdout_trim)
        )
        for needle in scrub or ():
            if needle:
                message = message.replace(needle, "<redacted>")
        raise SystemExit(message)
    return res.stdout


# ---------------------------------------------------------------------------
# Salt persistence (per-page, private, 0600)
# ---------------------------------------------------------------------------


def slugify_subpath(subpath: str) -> str:
    """Turn 'housing/' or 'anthology' into a safe filename stem.

    Keeps ASCII letters/digits/hyphens; other chars collapse to hyphens.
    Empty result falls back to a short hash so we can still index roots.
    """
    stem = re.sub(r"[^A-Za-z0-9-]+", "-", subpath.strip("/").lower()).strip("-")
    if not stem:
        stem = hashlib.sha1(subpath.encode("utf-8")).hexdigest()[:12]
    return stem


def salt_path_for(subpath: str) -> Path:
    return SALTS_DIR / f"{slugify_subpath(subpath)}.json"


def get_or_create_salt(subpath: str) -> str:
    """Return the 32-hex-char salt for this page, generating + persisting
    one on the first call.

    Load-bearing behavior: the SAME salt has to come back on every future
    run, because it's baked into the share link. If this file gets deleted,
    the previously-published share link stops decrypting. That's why we go
    out of our way to write mode 0600 and use a stable per-page slug.
    """
    p = salt_path_for(subpath)
    if p.exists():
        try:
            data = json.loads(p.read_text())
            salt = data.get("salt")
            if _is_valid_salt(salt):
                return salt
        except (json.JSONDecodeError, OSError):
            pass  # fall through to regenerate — better than crashing on drift

    SALTS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SALTS_DIR, 0o700)
    salt = secrets.token_hex(16)  # 32 hex chars, StatiCrypt's expected format
    # 0600 so a wandering process or another user account can't read the salt.
    # Not a secret on its own (it's shipped inside the ciphertext) but pairing
    # it with the passphrase in Keychain is what stabilizes the share link.
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"salt": salt, "subpath": subpath}, f)
    return salt


def _is_valid_salt(salt: Optional[str]) -> bool:
    if not isinstance(salt, str):
        return False
    return bool(re.fullmatch(r"[0-9a-f]{32}", salt))


# ---------------------------------------------------------------------------
# Keychain
# ---------------------------------------------------------------------------


def get_passphrase(keychain_service: str) -> str:
    p = run(["security", "find-generic-password", "-s", keychain_service, "-w"]).strip()
    if not p:
        raise SystemExit("keychain service %r returned empty passphrase" % keychain_service)
    return p


# ---------------------------------------------------------------------------
# Leak check (the space-phrase rule)
# ---------------------------------------------------------------------------


def strip_html_tags(html: str) -> str:
    """Cheap tag stripper — good enough to find visible words for the leak
    check. Not a real HTML parser, and doesn't need to be: we're building a
    haystack of the *visible* text so we can grep for it in the ciphertext.
    """
    # Drop <script>, <style> blocks — they're already inline JS with lots of
    # noise that isn't user-facing content.
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # Drop remaining tags.
    return re.sub(r"<[^>]+>", " ", html)


def extract_distinctive_phrases(source_html: str) -> List[str]:
    """Pick multi-word phrases from the source that would only appear in
    plaintext, so we can grep for them in the ciphertext.

    Rule: base64 output contains NO spaces at all. Any run of visible text
    with a space in it, once we filter to reasonably long phrases (>= 12
    chars), is distinctive enough that a chance hit in ciphertext is
    effectively zero. Grepping short ASCII strings against base64 was the
    false-positive source we're avoiding.

    Returns a de-duplicated list capped at LEAK_PHRASE_SAMPLE_SIZE.
    """
    text = strip_html_tags(source_html)
    # Split into "sentences" on . ! ? and newlines — a simple heuristic that
    # gives us multi-word chunks without needing a real tokenizer.
    chunks = re.split(r"[.!?\n\r]+", text)
    seen = set()
    phrases: List[str] = []
    for chunk in chunks:
        # Collapse whitespace, so a distinctive phrase is stable regardless
        # of how the source formatted it.
        chunk = re.sub(r"\s+", " ", chunk).strip()
        if " " not in chunk:
            continue
        if len(chunk) < MIN_LEAK_PHRASE_CHARS:
            continue
        # Trim to a reasonable haystack-needle size (~120 chars) so we don't
        # false-negative on any minor whitespace / entity difference between
        # source and ciphertext.
        needle = chunk[:120]
        if needle in seen:
            continue
        seen.add(needle)
        phrases.append(needle)
        if len(phrases) >= LEAK_PHRASE_SAMPLE_SIZE:
            break
    return phrases


def assert_no_plaintext_leak(source_html: str, encrypted_path: Path) -> None:
    """Grep the encrypted output for distinctive plaintext phrases.

    Two failure modes are BOTH errors:
      1. Encrypted output is missing — a silently-skipped write can look
         "clean" (empty haystack, no needle matches) if we're sloppy.
      2. A phrase from the source appears verbatim in the ciphertext.

    We fail before touching git so a leak never gets pushed.
    """
    if not encrypted_path.exists():
        raise SystemExit(
            "leak check: encrypted output %s does NOT exist — StatiCrypt "
            "produced no file, refusing to publish" % encrypted_path
        )
    cipher = encrypted_path.read_text(encoding="utf-8", errors="replace")
    phrases = extract_distinctive_phrases(source_html)
    if not phrases:
        # Nothing distinctive to check — e.g. the source is empty or all
        # single words. Rather than silently pass, warn loudly so the caller
        # knows the guard didn't run.
        print(
            "WARNING: no distinctive space-containing phrases found in source; "
            "leak check skipped. Confirm the source really has visible text.",
            file=sys.stderr,
        )
        return
    for needle in phrases:
        if needle in cipher:
            raise SystemExit(
                "PLAINTEXT LEAK: phrase %r found verbatim in %s — refusing to push"
                % (needle, encrypted_path)
            )


# ---------------------------------------------------------------------------
# StatiCrypt: two-call flow
# ---------------------------------------------------------------------------


# All ciphertext we publish is served at `<subpath>/index.html`, so we always
# rename StatiCrypt's output to this name after it writes. Documented in the
# module README + reinforced in the caller (index.html-only push scope).
PUBLISHED_FILENAME = "index.html"


def _staticrypt_env(passphrase: str) -> dict:
    """Return an env dict for a staticrypt call that reads the password from
    STATICRYPT_PASSWORD.

    StatiCrypt supports `STATICRYPT_PASSWORD` as an env-only override for
    `-p`, which keeps the passphrase off argv (so out of `ps` output, out
    of subprocess error strings, out of any zsh history if a shell were
    involved). We start from the current os.environ so the child still
    finds `PATH`, `HOME`, etc.
    """
    env = os.environ.copy()
    env["STATICRYPT_PASSWORD"] = passphrase
    return env


def staticrypt_encrypt(
    input_file: Path,
    output_dir: Path,
    salt: str,
    passphrase: str,
    extra_args: Optional[List[str]] = None,
) -> Path:
    """Call (a): write the encrypted HTML.

    `-c false` tells StatiCrypt NOT to read/write a `.staticrypt.json` config
    beside the input file. We pass the salt inline via `-s`; the passphrase
    ALWAYS goes via `STATICRYPT_PASSWORD` env var (never `-p <pass>`, which
    would leak to `ps` output and any subprocess-error log line).

    Returns the resolved output path — ALWAYS `<output_dir>/index.html`,
    because that's what GitHub Pages serves at `<subpath>/`. We rename here
    if the input basename differs, so a caller passing `report.html` still
    ends up with `index.html` on disk. This alignment is load-bearing: the
    share link points at `<share_base>/<subpath>/`, and the poll expects a
    200 there — a mismatched filename would 404.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    argv = list(STATICRYPT_CMD) + [
        str(input_file),
        "-d", str(output_dir),
        "-s", salt,
        "-c", "false",
        # --short silences the interactive short-passphrase prompt. Without
        # it StatiCrypt reads from stdin — and with stdin=DEVNULL the EOF
        # makes it exit failing to write. Passphrase policy lives with the
        # caller (Keychain), not with staticrypt.
        "--short",
    ]
    if extra_args:
        argv.extend(extra_args)
    # scrub= is defense-in-depth: env var carries the secret, but if a
    # future edit accidentally puts it back on argv, we still won't log it.
    run(argv, env=_staticrypt_env(passphrase), scrub=[passphrase])

    # StatiCrypt names the output after the input basename; rename to
    # index.html so the share link + Pages route agree.
    written = output_dir / input_file.name
    target = output_dir / PUBLISHED_FILENAME
    if written != target:
        if not written.exists():
            raise SystemExit(
                "staticrypt: expected output at %s but it was not created"
                % written
            )
        # replace() is atomic on POSIX and clobbers an existing target, so
        # a re-publish just overwrites the previous ciphertext.
        written.replace(target)
    return target


def staticrypt_share_link(
    input_file: Path,
    salt: str,
    passphrase: str,
    share_base: str,
) -> str:
    """Call (b): ask StatiCrypt for the share link. NOTE: --share does NOT
    write files. It only prints the link. That's the whole reason we need
    two calls.

    Passphrase again goes via STATICRYPT_PASSWORD env — never argv. We add a
    trailing slash to share_base so the produced URL points at the directory
    index (Pages serves `.../subpath/` -> `.../subpath/index.html`).
    StatiCrypt appends `#staticrypt_pwd=<hash>` to whatever we pass.
    """
    share_url = share_base.rstrip("/") + "/"
    argv = list(STATICRYPT_CMD) + [
        str(input_file),
        "-s", salt,
        "-c", "false",
        "--short",  # see staticrypt_encrypt for why
        "--share", share_url,
        "--share-remember",  # auto-tick "remember me" so viewer only decrypts once
    ]
    output = run(argv, env=_staticrypt_env(passphrase), scrub=[passphrase])
    # StatiCrypt prints the link somewhere in stdout, e.g.:
    #   Your file has been encrypted, ...
    #   Your share link: https://.../#staticrypt_pwd=abc123
    m = re.search(r"https?://\S*#staticrypt_pwd=\S+", output)
    if not m:
        raise SystemExit(
            "share link: could not find `#staticrypt_pwd=…` in StatiCrypt "
            "output. Output was:\n%s" % output
        )
    return m.group(0).rstrip(".,)")


# ---------------------------------------------------------------------------
# Git commit + push, then live-URL poll
# ---------------------------------------------------------------------------


def commit_and_push(repo: Path, subpath: str, commit_msg: str,
                    output_file: Path) -> bool:
    """Commit the leak-checked ciphertext file and push. Returns True if
    there was anything to publish, False if the working tree was clean
    at that path.

    HARD security invariant: we only ever `git add` the SINGLE file we
    just ran the leak check against (`output_file`). Two anti-patterns
    that used to live here are removed:

    - No `git add -A` fallback. The pages repo is PUBLIC. A `-A` on any
      dirty working tree could stage and push an unchecked artifact
      (someone else's WIP, a plaintext scratch file, a rendered draft)
      alongside our ciphertext. If the scoped add stages nothing, that's
      a caller bug (bad subpath, wrong repo, stale checkout) — we raise
      so it's visible, we do NOT widen the blast radius.
    - No `git add <subpath>` (the whole directory). Scope == leak-check
      scope: the exact `index.html` we verified, nothing else.
    """
    if not (repo / ".git").exists():
        raise SystemExit("pages repo not found at %s — clone it first." % repo)
    if not output_file.exists():
        raise SystemExit(
            "commit: leak-checked output %s is missing — refusing to push"
            % output_file
        )
    # Path used by `git add` and diff --cached MUST be relative to the repo
    # root so git parses it as a pathspec, not a filesystem path.
    try:
        rel = output_file.resolve().relative_to(repo.resolve())
    except ValueError as e:
        raise SystemExit(
            "commit: output %s is not inside repo %s — refusing to push"
            % (output_file, repo)
        ) from e

    # git status limited to the single file we plan to push. If it's clean,
    # nothing to do (this is normal on a no-op re-run with unchanged input).
    status = run(["git", "status", "--porcelain", "--", str(rel)], cwd=repo)
    if not status.strip():
        return False

    run(["git", "add", "--", str(rel)], cwd=repo)

    # Belt-and-suspenders: what did we actually stage? If the scoped add
    # produced nothing, refuse — do NOT fall back to a wider add.
    staged = run(["git", "diff", "--cached", "--name-only"], cwd=repo).strip().splitlines()
    if not staged:
        raise SystemExit(
            "commit: `git add %s` staged nothing — refusing to widen scope. "
            "This usually means the output path is not inside %s, or the "
            "subpath is wrong. Investigate before rerunning." % (rel, repo)
        )
    if staged != [str(rel).replace(os.sep, "/")]:
        # Something else got staged too — abort. We only push what we
        # leak-checked. A prior dirty index or a hook adding extras must
        # not sneak a plaintext byte into a public repo.
        raise SystemExit(
            "commit: staged files %r != expected [%r] — refusing to push. "
            "Reset the index and rerun." % (staged, str(rel))
        )

    run(["git", "commit", "-m", commit_msg], cwd=repo)
    run(["git", "push"], cwd=repo)
    return True


def poll_live_url(url: str, verbose: bool = True) -> int:
    """Poll the live URL with `curl --retry` (built-in) until it returns 200.

    We rely on curl's own retry machinery — no shell `sleep` loop — because
    curl backs off and honors --max-time consistently, and returns the final
    HTTP code as its stdout for us to parse.

    Returns the final HTTP status code. Non-2xx raises so a stalled Pages
    deploy is visible.
    """
    # Strip the fragment before polling — curl sends only path+query.
    poll_url = url.split("#", 1)[0]
    argv = list(CURL_POLL_CMD_TEMPLATE) + [poll_url]
    if verbose:
        print(f"polling {poll_url} (curl --retry, up to ~2.5min)...")
    res = subprocess.run(argv, text=True, capture_output=True)
    code_str = (res.stdout or "").strip()
    try:
        code = int(code_str[-3:])  # curl -w prints only 3 chars but be safe
    except ValueError:
        code = 0
    if not (200 <= code < 300):
        raise SystemExit(
            "live URL never returned 2xx (last code=%s). Pages may still be "
            "deploying, or the subpath is wrong. curl stderr: %s"
            % (code_str, (res.stderr or "").strip())
        )
    if verbose:
        print(f"live URL responded {code}")
    return code


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def publish(
    input_html: Path,
    keychain_service: str,
    subpath: str,
    share_base: Optional[str] = None,
    repo: Optional[Path] = None,
    dry_run: bool = False,
    commit_msg: Optional[str] = None,
    poll: bool = True,
) -> str:
    """End-to-end publish. Returns the share link (a URL with the
    `#staticrypt_pwd=…` hash appended)."""
    share_base = share_base or os.environ.get(SHARE_BASE_ENV_VAR)
    if not share_base:
        raise SystemExit("share base URL required: pass --share-base or set $%s" % SHARE_BASE_ENV_VAR)
    input_html = Path(input_html).resolve()
    if not input_html.exists():
        raise SystemExit("input HTML not found: %s" % input_html)

    passphrase = get_passphrase(keychain_service)
    salt = get_or_create_salt(subpath)
    source_html = input_html.read_text(encoding="utf-8", errors="replace")

    # For dry-run we never touch the pages repo — encrypt into a tmpdir,
    # do the leak check, do NOT commit/push/poll. Print the (real) share
    # link so the caller can preview it before the real run.
    if dry_run:
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            encrypted = staticrypt_encrypt(input_html, out_dir, salt, passphrase)
            assert_no_plaintext_leak(source_html, encrypted)
            share_url_base = share_base.rstrip("/") + "/" + subpath.strip("/")
            share_link = staticrypt_share_link(
                input_html, salt, passphrase, share_url_base
            )
            print("[dry-run] encrypted OK to %s" % encrypted)
            print("[dry-run] share link would be: %s" % share_link)
            return share_link

    repo_value = repo or os.environ.get(REPO_ENV_VAR)
    if not repo_value:
        raise SystemExit("pages repo required: pass --repo or set $%s" % REPO_ENV_VAR)
    repo = Path(repo_value)
    output_dir = (repo / subpath.strip("/"))
    encrypted = staticrypt_encrypt(input_html, output_dir, salt, passphrase)
    assert_no_plaintext_leak(source_html, encrypted)

    changed = commit_and_push(
        repo, subpath, commit_msg or f"Publish {subpath}",
        output_file=encrypted,
    )
    if not changed:
        print("no changes to publish (working tree clean)")

    share_url_base = share_base.rstrip("/") + "/" + subpath.strip("/")
    share_link = staticrypt_share_link(input_html, salt, passphrase, share_url_base)

    if poll and changed:
        poll_live_url(share_link)

    print("share link: %s" % share_link)
    return share_link


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_argv(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=("Publish an HTML file to a GitHub Pages repo as a "
                     "StatiCrypt-encrypted, password-gated page and print "
                     "a stable share link.")
    )
    p.add_argument("input_html", help="path to the source HTML file to encrypt")
    p.add_argument(
        "--keychain-service", required=True,
        help="Keychain service holding the passphrase (e.g. my-private-pages)",
    )
    p.add_argument(
        "--subpath", required=True,
        help="output subdirectory under the pages repo root (e.g. 'housing')",
    )
    p.add_argument(
        "--share-base", default=None,
        help="base URL that serves the pages repo (default: $%s)" % SHARE_BASE_ENV_VAR,
    )
    p.add_argument(
        "--repo", default=None,
        help="local path to the pages repo (default: $%s)" % REPO_ENV_VAR,
    )
    p.add_argument("--commit-message", default=None,
                   help="git commit message (default: 'Publish <subpath>')")
    p.add_argument("--dry-run", action="store_true",
                   help="encrypt to a tmpdir + leak-check + print the share "
                        "link, but do NOT commit/push/poll")
    p.add_argument("--no-poll", action="store_true",
                   help="skip the live-URL poll after push (useful for CI/tests)")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_argv(argv if argv is not None else sys.argv[1:])
    publish(
        input_html=Path(args.input_html),
        keychain_service=args.keychain_service,
        subpath=args.subpath,
        share_base=args.share_base,
        repo=Path(args.repo) if args.repo else None,
        dry_run=args.dry_run,
        commit_msg=args.commit_message,
        poll=not args.no_poll,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
