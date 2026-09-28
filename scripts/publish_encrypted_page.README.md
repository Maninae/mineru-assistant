# publish_encrypted_page.py

One-command publisher for a StatiCrypt-encrypted, password-gated HTML page in
a public GitHub Pages repo you own. The repo path (`--repo` or
`$MINERU_PUBLISH_REPO`) and the URL that serves it (`--share-base` or
`$MINERU_PUBLISH_SHARE_BASE`) are required. Prints a stable share link.

## Why this exists

Publishing encrypted pages by hand kept hitting the same three traps:

1. **`staticrypt --share` does NOT write files** — it only prints a link.
   So we call staticrypt twice: once to write the encrypted HTML, once with
   `--share` to get the link.
2. **Naive plaintext leak greps false-positive on base64** — short letter
   strings like `"abc"` appear by chance in ciphertext. The correct check
   greps for distinctive multi-word phrases (base64 has no spaces), and
   treats a MISSING output file as failure rather than "clean".
3. **Re-publishing needs the SAME salt + passphrase** — a fresh salt on every
   run breaks every previously-shared link. Salt is persisted per-page in
   `$MINERU_HOME/cache/staticrypt-salts/<slug>.json` (mode `0600`), so subsequent
   runs re-use it and the share link is stable.

All three are baked into this helper.

## Usage

```bash
MINERU_PUBLISH_REPO=~/Developer/my-pages \
MINERU_PUBLISH_SHARE_BASE=https://example.github.io/my-pages \
python3 "$MINERU_HOME/scripts/publish_encrypted_page.py" \
    /path/to/source.html \
    --keychain-service my-private-pages \
    --subpath housing
```

That will:
1. Read the passphrase from macOS Keychain service `my-private-pages` and
   pass it to StatiCrypt via the `STATICRYPT_PASSWORD` env var — never on
   argv, so `ps` never shows it and no error path can echo it.
2. Look up (or first-time generate) the salt for `housing` and persist it.
3. Encrypt `source.html` to `<repo>/housing/index.html`. The
   output is ALWAYS renamed to `index.html` (regardless of the input
   basename) so the share link + Pages route agree — Pages serves
   `<subpath>/` from `<subpath>/index.html`, and a mismatched filename
   would 404.
4. Run the space-phrase leak check against the ciphertext; abort on any hit
   or if StatiCrypt produced no file.
5. Scoped push: `git add -- housing/index.html` + `git commit` + `git push`.
   We ONLY add the single leak-checked file. If the scoped add stages
   nothing, or stages something we did not leak-check, we HARD-FAIL rather
   than falling back to `git add -A` (the pages repo is PUBLIC — a wider
   add could push an unchecked artifact).
6. Poll `<share-base>/housing/` with `curl --retry`
   until it responds `200`.
7. Print the share link (`https://…/housing/#staticrypt_pwd=…&remember_me`).

## Flags

- `--dry-run` — encrypt to a tmpdir, run the leak check, print the (real)
  share link. Does NOT touch the pages repo (so `--repo` is not needed), does NOT poll. Use this to
  preview the flow before a real publish.
- `--no-poll` — skip the live-URL poll after push (fine for CI or if you're
  publishing several pages in a row and don't want to wait for each).
- `--share-base URL` — base URL that serves the repo (default:
  `$MINERU_PUBLISH_SHARE_BASE`; required).
- `--repo PATH` — local path to the pages repo (default:
  `$MINERU_PUBLISH_REPO`; required unless `--dry-run`).
- `--commit-message TEXT` — override the git commit message.

## Import surface

```python
from publish_encrypted_page import publish
share_link = publish(
    input_html=Path("source.html"),
    keychain_service="my-private-pages",
    subpath="housing",
    share_base="https://example.github.io/my-pages",
    repo=Path("~/Developer/my-pages").expanduser(),
    dry_run=False,
)
```

## Files

- Salt store: `$MINERU_HOME/cache/staticrypt-salts/<slug>.json` (mode `0600`,
  parent dir `0700`).
- Encrypted output: `<repo>/<subpath>/index.html` (ciphertext only,
  ever — plaintext must never land in the public repo).
- Passphrase: macOS Keychain, service = your `--keychain-service` argument.

## Tests

`tests/test_publish_encrypted_page.py` covers salt persistence (mode 0600 +
per-page + stable across runs), distinctive-phrase extraction, the two leak
failure modes (missing output + phrase hit), the base64 false-positive
resistance, share-link parsing, and a full dry-run orchestration with
subprocess and Keychain stubbed. Run:

```bash
python3 -m unittest tests.test_publish_encrypted_page -v
```
