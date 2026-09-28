# Prompt Injection Firewall

Screens external data (iMessage, Gmail, Calendar, Drive) for prompt injection
before it reaches the agent. Rebuilt June 2026: per-unit screening with
redaction, deterministic-first pipeline, local Ollama over HTTP.

## Architecture

```
bin/imsg-firewall ─┐                          ┌→ imsg_cleaner.py (strip metadata)
bin/gog-firewall  ─┤→ wrapper_common.py ──────┤→ gog_cleaner.py  (strip metadata)
                   │   (run → clean → split   │
                   │    → screen → redact)    └→ boundary_validator (gog only, warn-only)
                   │
                   └→ units.py      split output into units (per message/email/event),
                   │                reassemble with stubs for flagged units
                   └→ screener.py   per unit: cache → patterns → LLM
                        ├→ patterns.py        Tier A (block) / Tier B (escalate)
                        └→ prompts/screen.md  the one LLM prompt
```

## The pipeline, per unit

1. **Patterns** (`patterns.py`, instant, full text — nothing truncated):
   - **Tier A** = unambiguous attack markers (chat-template tokens, "ignore all
     previous instructions", dense invisible-char runs). Hit → blocked, no LLM.
   - **Tier B** = broad instruction-to-AI vocabulary **OR** any unit ≥ 160 chars
     (`SUBSTANTIVE_LENGTH`). Hit → escalate to LLM. The length backstop closes
     the vocabulary-free-injection gap: a payload with no "ignore"/"AI"/"forward"
     keywords still gets the LLM if it's long enough to carry an instruction.
   - No hit (short, keyword-free chatter like "see you at 7") → safe, done.
     **This is the only path with no LLM call.**
2. **Cache** (`$MINERU_HOME/cache/firewall-verdicts.sqlite3`, 30-day TTL):
   LLM verdicts keyed by sha256(model + prompt + text). Repeat reads are free.
   Keyspace includes the prompt, so editing `prompts/screen.md` auto-invalidates.
   The cache **dir** is forced to `0700` (covers sqlite's journal sidecars, not
   just the DB); `MINERU_FIREWALL_CACHE` is honored only if it stays under
   `$MINERU_HOME` (else the env var would be an arbitrary-file chmod primitive).
3. **LLM** (only for escalated units): Ollama HTTP `/api/chat`, qwen3:8b,
   `think:false` (critical — thinking mode was the old 18-90s latency),
   JSON-schema-constrained output, temperature 0, `keep_alive` 2h.
   Units > 7,000 chars are screened via windows: around the pattern matches if
   any, else the whole text is chunked (head/tail/sampled middle) — an attack
   at char 13,000 is still seen (the old screener truncated at 12k). A unit too
   large for `MAX_WINDOWS` chunks fails closed (blocked) rather than claimed clean.

## Redaction, not all-or-nothing

A flagged message/email/event becomes a stub
(`{"firewall_blocked": true, "reason": ...}` + pattern-clean structural
metadata); everything else flows through. Exit 77 only when *nothing* was safe.
Partial redactions are reported on the wrapper's stderr.

## Failure semantics

- LLM needed but unreachable → that unit is **blocked** (fail-closed),
  pattern-clean units still flow. An Ollama outage no longer blocks all reads.
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
| `MINERU_FIREWALL_MODE` | `gated` | `gated` = LLM only on Tier-B escalation; `always` = LLM on every unit; `off` = patterns only |
| `MINERU_FIREWALL_MODEL` | `qwen3:8b` | must be pulled in Ollama; thinking models OK (`think:false` is sent) |
| `MINERU_FIREWALL_OLLAMA_URL` | `http://localhost:11434` | |
| `MINERU_FIREWALL_CACHE` | `$MINERU_HOME/cache/firewall-verdicts.sqlite3` | |

## Testing

```bash
# Unit tests (LLM mocked) + cleaner tests — fast, run on any change
/usr/bin/python3 -m pytest scripts/firewall/test_screener.py scripts/firewall/test_gog_cleaner.py -q

# Live accuracy + latency eval (real Ollama) — run after touching
# patterns.py, prompts/screen.md, or the model. Expect 21/21, p50 < 1s.
/usr/bin/python3 scripts/firewall/eval_screener.py --no-cache

# Ad-hoc screening
echo "some content" | python3 scripts/firewall/screener.py
```

## Tuning false positives / negatives

1. Reproduce: `echo "content" | python3 scripts/firewall/screener.py`
2. Check the detector in the JSON verdict:
   - `pattern` → adjust Tier A/B in `patterns.py` (Tier A must stay
     near-zero-FP; when in doubt, demote to Tier B)
   - `llm` → tune `prompts/screen.md` (the contrastive EXAMPLES section is the
     highest-leverage spot for small-model judgment)
3. Add the case to `eval_screener.py`'s corpus and re-run the eval.
4. Telemetry: `logs/firewall/screen.jsonl` logs units/escalations/blocks/ms
   per invocation — check escalation rate if reads feel slow.

## Module responsibilities

| File | One job |
|------|---------|
| `patterns.py` | deterministic detection: Tier A block, Tier B escalate |
| `screener.py` | verdict engine: cache, Ollama HTTP, windowing, parallelism |
| `units.py` | split output into units; reassemble with redaction stubs |
| `wrapper_common.py` | shared wrapper flow: screen → emit → exit codes + banners |
| `imsg_cleaner.py` / `gog_cleaner.py` | token-diet metadata stripping (pre-screening), config-driven |
| `eval_screener.py` | labeled live corpus; the accuracy/latency regression gate |

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
  `patterns.sanitize_reason`: pattern reasons (my constants) are shown verbatim,
  LLM reasons are genericized so the firewall's own output can't echo attacker
  text back to the agent. Keep it that way.
