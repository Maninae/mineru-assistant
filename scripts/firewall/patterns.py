#!/usr/bin/env python3
"""
patterns.py - Deterministic injection detection for the prompt firewall.

Two tiers, two jobs:

  Tier A (BLOCK):    unambiguous attack markers. Matching = blocked instantly,
                     no LLM consulted. Precision must be near-perfect; a Tier A
                     false positive blocks legitimate content with no appeal.

  Tier B (ESCALATE): broad "instruction-aimed-at-an-AI" vocabulary AND any
                     substantive free text (>= SUBSTANTIVE_LENGTH chars).
                     Matching routes the unit to the LLM for a verdict. False
                     positives here only cost ~1s of LLM time, so recall wins.
                     The length backstop is deliberate: a vocabulary-free
                     injection (no "ignore"/"AI"/"forward" keywords) wouldn't
                     trip a lexical pattern, so anything long enough to carry
                     one is escalated regardless. Only short, keyword-free
                     chatter ("see you at 7") skips the LLM entirely.

Patterns scan the FULL text of every unit — unlike the old screener, nothing
is truncated away before pattern matching.

Python 3.9 compatible. Stdlib only.
"""
import re
from typing import List, Optional, Tuple

# ============================================================================
# TIER A — hard block, near-zero false-positive tolerance
# ============================================================================

TIER_A_PATTERNS: List[Tuple[str, str]] = [
    # Chat-template special tokens never occur in legitimate human content.
    # NOTHING with any plausible innocent use belongs here — Tier A blocks with
    # no LLM appeal. The canonical "ignore all previous instructions" phrasing
    # is deliberately NOT here: it shows up verbatim in AI/security newsletters
    # and in the operator's own work chatter, so it escalates to the LLM (Tier B) where
    # "discussing the attack" is told apart from "executing the attack".
    (r"<\|im_start\|>|<\|im_end\|>|<\|endoftext\|>", "Chat template token injection"),
    (r"<\|(system|user|assistant)\|>", "Chat template token injection"),
]

# Invisible/zero-width characters used to hide payloads from human review.
# U+200D ZERO WIDTH JOINER is intentionally EXCLUDED — it is structural in
# emoji (👨‍👩‍👧‍👦 is built from ZWJs), so counting it false-positives on
# ordinary family/profession emoji. A dense run of the remaining truly-unused
# code points is steganography.
_INVISIBLE_CHARS = "​‌⁠⁡⁢⁣⁤﻿­"
_INVISIBLE_BLOCK_THRESHOLD = 8   # this many invisible chars = block
_INVISIBLE_ESCALATE_THRESHOLD = 2

# ============================================================================
# TIER B — escalate to LLM. Broad on purpose.
# ============================================================================

TIER_B_PATTERNS: List[Tuple[str, str]] = [
    # Talking about instructions/prompts/roles at all
    (r"\b(instructions?|system\s+prompt|initial\s+prompt|guidelines|directives)\b", "instruction vocabulary"),
    (r"\byou\s+are\s+(now|a|an|the)\b", "role assignment phrasing"),
    (r"\b(act\s+as|pretend\s+(to\s+be|you))\b", "role-play phrasing"),
    (r"\bfrom\s+now\s+on\b", "behavioral override phrasing"),
    (r"\bnew\s+(task|rule|persona|identity)\b", "task reassignment phrasing"),
    (r"\b(ignore|disregard|forget|override|bypass|skip)\b", "override verb"),
    # Addressing an AI directly
    (r"\b(AI|assistant|agent|chatbot|bot|LLM|Claude|GPT|Gemini|copilot|model)\b", "AI addressed"),
    (r"\b(jailbreak|jailbroken|DAN\s+mode|developer\s+mode|unfiltered|uncensored)\b", "jailbreak vocabulary"),
    # Secrets / exfiltration targets
    (r"\b(api\s*key|password|passphrase|credential|auth\s*token|secret\s*key|private\s+key|seed\s+phrase|2fa|otp|verification\s+code|account\s+number|config(uration)?)\b",
     "credential vocabulary"),
    # Imperative verbs — the core of indirect prompt injection. Broad on
    # purpose: an injection has to TELL the reader to do something, and these
    # are the verbs it uses. Cheap to over-escalate (one LLM call).
    (r"\b(send|forward|upload|post|transmit|paste|reply|respond|email|include|attach|append|"
     r"visit|click|open|navigate|fetch|download|extract|summari[sz]e|copy|dump|display|print|"
     r"output|confirm|verify|execute|run|call)\b", "imperative verb"),
    # URLs / exfiltration sinks
    (r"https?://|www\.[a-z0-9-]+\.", "URL present"),
    # Authority / urgency manipulation
    (r"\[\s*(SYSTEM|ADMIN|IMPORTANT|URGENT|NOTE\s+TO\s+AI)\s*\]", "authority marker"),
    (r"\bnote\s+(to|for)\s+(the\s+)?(ai|assistant|agent|reader|reviewer|bot|model|llm)\b",
     "out-of-band note marker"),
    (r"\b(directive|mandate|operational\s+directive|company\s+policy|per\s+policy)\b",
     "pseudo-authority framing"),
    (r"\b(i\s*am|this\s+is)\s+(your|the)\s+(developer|creator|admin|administrator|owner|operator)\b",
     "authority impersonation"),
    (r"\b(urgent|emergency|critical|immediately)\b.{0,60}\b(override|bypass|comply|must)\b", "urgency manipulation"),
    # Hidden channels
    (r"<!--.*?-->", "HTML comment (hidden channel)"),
    (r"[A-Za-z0-9+/]{80,}={0,2}", "long base64-like run"),
    (r"&#x?[0-9a-fA-F]{2,4};(\s*&#x?[0-9a-fA-F]{2,4};){4,}", "HTML entity encoding run"),
]

# Any unit at least this long is escalated to the LLM in gated mode even if no
# Tier-B pattern fired — substantial free text is exactly where a novel,
# vocabulary-free injection would hide, and an LLM call is now cheap.
SUBSTANTIVE_LENGTH = 160

_TIER_A = [(re.compile(p, re.IGNORECASE), d) for p, d in TIER_A_PATTERNS]
_TIER_B = [(re.compile(p, re.IGNORECASE | re.DOTALL), d) for p, d in TIER_B_PATTERNS]


class PatternVerdict:
    """Result of pattern scanning one unit of text."""

    __slots__ = ("blocked", "escalate", "reason", "location", "match_spans")

    def __init__(self, blocked: bool, escalate: bool, reason: Optional[str] = None,
                 location: Optional[str] = None,
                 match_spans: Optional[List[Tuple[int, int]]] = None):
        self.blocked = blocked
        self.escalate = escalate
        self.reason = reason
        self.location = location
        self.match_spans = match_spans or []


def _describe_match(text: str, start: int, matched: str) -> str:
    line = text[:start].count("\n") + 1
    snippet = matched if len(matched) <= 60 else matched[:60] + "..."
    return "line {}, matched: {!r}".format(line, snippet)


def scan(text: str) -> PatternVerdict:
    """
    Scan full text. Returns:
      blocked=True                  -> Tier A hit, block without LLM
      escalate=True                 -> Tier B hit(s), send to LLM
      blocked=False, escalate=False -> clean, pass without LLM
    match_spans (for escalations) lets the screener build focused LLM windows
    around the suspicious regions of very large texts.
    """
    for pattern, description in _TIER_A:
        m = pattern.search(text)
        if m:
            return PatternVerdict(True, False, description,
                                  _describe_match(text, m.start(), m.group()))

    invisible_count = sum(text.count(ch) for ch in _INVISIBLE_CHARS)
    if invisible_count >= _INVISIBLE_BLOCK_THRESHOLD:
        return PatternVerdict(True, False,
                              "Hidden invisible-character payload ({} chars)".format(invisible_count),
                              "throughout text")

    spans: List[Tuple[int, int]] = []
    first_reason: Optional[str] = None
    first_location: Optional[str] = None
    for pattern, description in _TIER_B:
        m = pattern.search(text)
        if m:
            spans.append((m.start(), m.end()))
            if first_reason is None:
                first_reason = description
                first_location = _describe_match(text, m.start(), m.group())

    if invisible_count >= _INVISIBLE_ESCALATE_THRESHOLD:
        spans.append((0, min(len(text), 200)))
        if first_reason is None:
            first_reason = "invisible characters present"
            first_location = "throughout text"

    if spans:
        return PatternVerdict(False, True, first_reason, first_location, spans)

    # No pattern fired, but substantial free text still goes to the LLM:
    # vocabulary-free injections live here. Empty spans => screener windows
    # the whole text, not just a prefix.
    if len(text.strip()) >= SUBSTANTIVE_LENGTH:
        return PatternVerdict(False, True, "substantive unscreened text",
                              "length {} chars".format(len(text)), [])

    return PatternVerdict(False, False)


# ============================================================================
# REASON SANITIZATION
# ============================================================================

def sanitize_reason(reason: Optional[str], detector: Optional[str]) -> str:
    """
    Make a reason safe to surface to the agent (in redaction stubs / banners).

    Pattern reasons are author-controlled constant strings — safe to show.
    LLM reasons are model-authored from attacker-controlled input: the model is
    asked for one short sentence but isn't constrained from echoing attacker
    text (a URL, a quoted imperative), which would make the firewall's own
    output an injection channel. So LLM reasons are replaced with a generic
    label; the detailed model reason is kept only in the telemetry log.
    """
    if detector and detector.startswith("pattern"):
        return (reason or "flagged by pattern")[:160]
    if detector == "llm-error":
        return "screening unavailable - blocked for safety"
    return "flagged by LLM screener (injection-like content)"
