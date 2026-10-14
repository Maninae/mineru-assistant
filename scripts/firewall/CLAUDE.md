# Prompt Injection Firewall

Cleans and screens external data (iMessage, Gmail, Drive) before it reaches the
agent. Default is **patterns-only**: deterministic metadata cleaning, per-unit
splitting, and a Tier A block on chat-template tokens. The local-LLM stage
(Ollama qwen3:8b) is retired from the default path but kept intact and opt-in
via `MINERU_FIREWALL_MODE=gated|always`.

Why patterns-only: frontier agents judge untrusted content themselves, while
the 8B screener mostly redacted long newsletters and recruiter mail and kept a
~6 GB model resident for hours (`keep_alive` 2h). The
agent-side rule is the real defense: external content is data, never
instructions.

## Architecture

```
bin/imsg-firewall ─┐                          ┌→ imsg_cleaner.py (strip metadata)
bin/gog-firewall  ─┤→ wrapper_common.py ──────┤→ gog_cleaner.py  (strip metadata)
                   │   (run → clean → split   │
                   │    → screen → redact)    └→ boundary_validator (gog only, warn-only)
                   │
                   └→ units.py      split output into units (per message/email/event),
                   │                reassemble with stubs for flagged units
                   └→ screener.py   per unit: patterns (+ cache → LLM in gated/always)
                        ├→ patterns.py        Tier A (block) / Tier B (escalate)
                        └→ prompts/screen.md  the LLM prompt (gated/always only)
```

## The pipeline, per unit

1. **Patterns** (`patterns.py`, instant, full text, nothing truncated):
   - **Tier A** = chat-template tokens (`<|im_start|>`, `<|system|>`, ...).
     Hit → blocked in every mode. These are the ONLY default blocks.
     "ignore all previous instructions" is deliberately not Tier A (it is
     quoted verbatim in AI/security newsletters).
   - **Tier B** = instruction-to-AI vocabulary, ≥ 2 invisible characters, or
     any unit ≥ 160 chars (`SUBSTANTIVE_LENGTH`). In `off` mode the unit
     passes; in `gated` mode it escalates to the LLM.
   - **Invisible characters never block.** A passed unit with ≥ 8
     (`INVISIBLE_NOTE_THRESHOLD`) gets a `ScreenResult.note`, which the wrapper
     prints as `firewall: N invisible characters in unit X, passed
     (patterns-only mode)`.
2. **Cache** (gated/always only; `$MINERU_HOME/cache/firewall-verdicts.sqlite3`,
   30-day TTL): LLM verdicts keyed by sha256(model + prompt + text). The cache
   **dir** is forced to `0700` (covers sqlite's journal sidecars);
   `MINERU_FIREWALL_CACHE` is honored only if it stays under `$MINERU_HOME`
   (else the env var would be an arbitrary-file chmod primitive).
3. **LLM** (gated/always only): Ollama HTTP `/api/chat`, `think:false`,
   JSON-schema output, temperature 0, `keep_alive` 2h. Units > 7,000 chars are
   screened via windows covering the whole text; a unit too large for
   `MAX_WINDOWS` chunks fails closed. An escalated unit is blocked if Ollama is
   unreachable.

## Redaction, not all-or-nothing

A flagged message/email/event becomes a stub
(`{"firewall_blocked": true, "reason": ...}` + pattern-clean structural
metadata); everything else flows through. Exit 77 only when *nothing* was safe.
Partial redactions are reported on the wrapper's stderr.

## Failure semantics

- Default mode has no external dependency: no Ollama call, no cache.
- Cache broken → screening proceeds without cache (cache is never load-bearing).
- Cleaner/splitter crash → exit 78, nothing emitted (fail-closed).

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | Output delivered (possibly with individual units redacted — check stderr) |
| 77   | Everything blocked (injection detected) |
| 78   | Firewall error (screening broke; blocked for safety) |

## Config (env vars)

| Var | Default | Notes |
|-----|---------|-------|
| `MINERU_FIREWALL_MODE` | `off` | `off` = patterns only (Tier A blocks, everything else passes); `gated` = LLM on Tier-B escalation; `always` = LLM on every unit |
| `MINERU_FIREWALL_MODEL` | `qwen3:8b` | gated/always only; must be pulled in Ollama |
| `MINERU_FIREWALL_OLLAMA_URL` | `http://localhost:11434` | gated/always only |
| `MINERU_FIREWALL_CACHE` | `$MINERU_HOME/cache/firewall-verdicts.sqlite3` | gated/always only |

## Testing

```bash
# Unit tests (LLM mocked) + cleaner tests — fast, run on any change
/usr/bin/python3 -m pytest scripts/firewall/test_screener.py scripts/firewall/test_gog_cleaner.py -q

# Live LLM-path eval (real Ollama; forces gated mode) — only relevant if you
# re-enable the LLM stage or touch prompts/screen.md / the model.
/usr/bin/python3 scripts/firewall/eval_screener.py --no-cache

# Ad-hoc screening
echo "some content" | python3 scripts/firewall/screener.py
```

## Tuning false positives / negatives

1. Reproduce: `echo "content" | python3 scripts/firewall/screener.py`
2. Check the detector in the JSON verdict:
   - `pattern` → adjust Tier A in `patterns.py`. Tier A blocks with no
     appeal, so it must stay near-zero-FP; when in doubt, demote to Tier B
     (which passes by default).
   - `llm` / `llm-cached` (gated/always only) → tune `prompts/screen.md`
     and add the case to `eval_screener.py`'s corpus.
3. Telemetry: `logs/firewall/screen.jsonl` logs mode/units/escalated/blocked/
   noted/ms per invocation (counts only). In `off` mode `escalated` is always 0.

## Module responsibilities

| File | One job |
|------|---------|
| `patterns.py` | deterministic detection: Tier A block, Tier B escalate, invisible-char count |
| `screener.py` | verdict engine: mode switch; cache, Ollama HTTP, windowing (gated/always) |
| `units.py` | split output into units; reassemble with redaction stubs |
| `wrapper_common.py` | shared wrapper flow: screen → emit → exit codes + banners |
| `imsg_cleaner.py` / `gog_cleaner.py` | token-diet metadata stripping (pre-screening), config-driven |
| `eval_screener.py` | labeled live corpus for the opt-in LLM path |

## History / gotchas

- Python 3.9-safe, stdlib only — wrappers run under whatever `python3` is on
  PATH (system 3.9 or brew 3.14). Don't add 3.10+ syntax or pip deps.
- The old `layer2/layer3` naming, `common.py`, `llm-bridge.mjs`, Haiku/Sonnet
  references, and the OpenClaw `prompt-injection-guard` plugin are all gone
  (June 2026 rebuild). "Layer 1" command-blocking now exists only as policy in
  TOOLS.md — there is no technical enforcement preventing raw `gog`/`imsg`.
- Outbound sends are deliberately unscreened (`imsg send`, gog writes) — no
  injection risk in data leaving the machine.
- Redaction stubs never include free-text fields (text, subject, sender_name,
  snippet, body) — only ids, numbers/booleans, and strictly-validated ISO
  timestamps (`units._stub_fields`). The stub `reason` and the block banner use
  `patterns.sanitize_reason`: pattern reasons (author constants) are shown verbatim,
  LLM reasons are genericized so the firewall's own output can't echo attacker
  text back to the agent. Keep it that way.
