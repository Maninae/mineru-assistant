"""safezip — security-hardened zip extraction for untrusted archives.

This package wraps Python's stdlib `zipfile` with a threat-mapped hardening
layer: file-count / total-size / compression-ratio caps, symlink and special-file
rejection, path-traversal recheck, per-member decompressed-size bound (real bomb
defense), an extension allowlist, and magic-byte content verification. Nothing
unsafe is ever written to disk (inspect-then-write), so the tool never needs to
delete a partial artifact — which is important because this workspace forbids
`rm`.

Public surface:
  scripts.safezip.extractor.SafeZipExtractor
  scripts.safezip.cli.main
  scripts.safezip.manifest.ExtractionManifest
  scripts.safezip.config          (caps, allowlist, magic signatures)
"""
