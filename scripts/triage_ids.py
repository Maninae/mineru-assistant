#!/usr/bin/env python3
"""Deterministic thread-ID machinery for the inbox-triage job.

The LLM's only job in triage is judgment: deciding which unread emails are
noise safe to mark read. Capturing the exact Gmail thread IDs for those
decisions is plumbing, so it lives here in code, not in the model's output.
This removes two failure modes the LLM-written manifest had:

  1. Skipped manifests. The model was asked to hand-write
     `triage-<date>-ids.json`; it did so on only ~78% of runs. `write` here
     produces it every time from data, or fails loud.
  2. Mangled / hallucinated IDs. IDs are copied VERBATIM from the fetched
     search results — the model never types a 16-char hex id, it only names
     candidate rows by their integer index.

Three subcommands, one per phase of the job:

  fetch     Run the unread search(es), dedupe, assign stable integer indices,
            write the candidates file, and print a numbered table the LLM reads
            to classify.
  write     Given the LLM's per-clump row-index picks, resolve each index to
            its verbatim thread id and write the canonical manifest.
  clear     When the operator approves ("clear all" / "clear 1,3"), read the manifest
            and mark those threads read via gog-firewall.

The candidates file (indices -> ids) persists between fetch and write so the
indexing the model saw is exactly the indexing write resolves against.

Python 3.9-compatible (may be invoked by /usr/bin/python3). Stdlib only; Gmail
access goes through the same `gog-firewall` CLI the rest of the workspace uses.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
GOG_FIREWALL = MINERU_HOME / "bin" / "gog-firewall"
CANDIDATES_DIR = MINERU_HOME / "cache" / "triage"
BRIEFS_INBOX_DIR = MINERU_HOME / "briefs_inbox"

DEFAULT_SEARCH_QUERY = "is:unread"
DEFAULT_MAX_RESULTS = 100

# Column widths for the printed candidate table (display only; the full values
# always live untruncated in the candidates JSON).
DATE_DISPLAY_WIDTH = 16
SENDER_DISPLAY_WIDTH = 32
SUBJECT_DISPLAY_WIDTH = 68

# gog-firewall exit code meaning "every result in this query was redacted as a
# possible injection" (see scripts/firewall/CLAUDE.md). For a triage sweep that
# is "nothing classifiable here", not a search failure.
FIREWALL_ALL_BLOCKED_EXIT = 77


def candidates_path(date: str) -> Path:
    """Path to the persisted candidate list for a given YYYY-MM-DD triage run."""
    return CANDIDATES_DIR / f"triage-{date}-candidates.json"


def manifest_path(date: str) -> Path:
    """Path to the canonical thread-ID manifest the `clear` step reads back."""
    return BRIEFS_INBOX_DIR / f"triage-{date}-ids.json"


def run_gog_search(query: str, max_results: int) -> list:
    """Run one `gog-firewall gmail search` and return its thread objects.

    Returns the raw `.threads[]` list (each: id, from, subject, date, labels).
    Firewall-redacted stubs and any entry without a usable id are dropped by
    the caller, not here. Lets a non-zero exit / bad JSON raise so a broken
    search fails loud rather than silently yielding zero candidates.
    """
    result = subprocess.run(
        [str(GOG_FIREWALL), "gmail", "search", query, "--max", str(max_results), "--json"],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode == FIREWALL_ALL_BLOCKED_EXIT:
        # This query's results were all redacted — contributes zero candidates,
        # but must NOT abort the other queries in the sweep. Keep raising on 78
        # (firewall itself broke) and every other non-zero exit below.
        print(f"[triage] query {query!r}: all results redacted (exit 77), skipping",
              file=sys.stderr)
        return []
    if result.returncode != 0:
        raise RuntimeError(
            f"gog-firewall search failed (exit {result.returncode}) for query {query!r}: "
            f"{result.stderr.strip()}"
        )
    payload = json.loads(result.stdout)
    # gog-firewall returns {"threads": null} (not []) for a zero-match query,
    # so coalesce rather than trusting the .get default.
    return payload.get("threads") or []


def fetch_candidates(queries: list, max_results: int) -> list:
    """Fetch unread threads across one or more queries, deduped and indexed.

    Threads are merged across queries (a thread matching two queries appears
    once), sorted newest-first by date, and assigned a stable integer `index`
    the LLM references when classifying. Redacted / id-less entries are skipped
    ("keep, don't classify" — they never become clear candidates).
    """
    by_id = {}
    for query in queries:
        for thread in run_gog_search(query, max_results):
            thread_id = thread.get("id")
            if not thread_id or thread.get("firewall_blocked"):
                continue
            # First query to surface a thread wins; later duplicates are ignored.
            by_id.setdefault(thread_id, {
                "id": thread_id,
                "from": thread.get("from", ""),
                "subject": thread.get("subject", ""),
                "date": thread.get("date", ""),
                "labels": thread.get("labels", []),
            })
    ordered = sorted(by_id.values(), key=lambda t: t["date"], reverse=True)
    for index, thread in enumerate(ordered):
        thread["index"] = index
    return ordered


def print_candidate_table(candidates: list) -> None:
    """Print the numbered table the LLM reads to pick safe-to-clear rows."""
    print(f"{'#':>3}  {'DATE':<{DATE_DISPLAY_WIDTH}}  {'FROM':<{SENDER_DISPLAY_WIDTH}}  SUBJECT")
    for c in candidates:
        subject = c["subject"]
        if len(subject) > SUBJECT_DISPLAY_WIDTH:
            subject = subject[:SUBJECT_DISPLAY_WIDTH - 1] + "…"
        sender = c["from"]
        if len(sender) > SENDER_DISPLAY_WIDTH:
            sender = sender[:SENDER_DISPLAY_WIDTH - 1] + "…"
        print(f"{c['index']:>3}  {c['date']:<{DATE_DISPLAY_WIDTH}}  {sender:<{SENDER_DISPLAY_WIDTH}}  {subject}")


def cmd_fetch(args: argparse.Namespace) -> None:
    """fetch: search unread, persist candidates, print the numbered table."""
    queries = args.query or [DEFAULT_SEARCH_QUERY]
    candidates = fetch_candidates(queries, args.max)
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    out = candidates_path(args.date)
    out.write_text(
        json.dumps({"date": args.date, "queries": queries, "candidates": candidates},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print_candidate_table(candidates)
    print(f"\n{len(candidates)} candidates written to {out}", file=sys.stderr)


def load_candidates(date: str) -> list:
    """Read back the candidate list `fetch` persisted for this date."""
    path = candidates_path(date)
    if not path.exists():
        raise FileNotFoundError(
            f"No candidates file at {path}. Run `triage_ids.py fetch --date {date}` first."
        )
    return json.loads(path.read_text(encoding="utf-8"))["candidates"]


def parse_decisions(args: argparse.Namespace) -> dict:
    """Load the LLM's clump decisions (row indices per clump), from arg or file.

    Shape: {"<clump-number>": {"label": str, "rows": [int, ...]}, ...}. IDs are
    deliberately absent — the model names rows, not ids.
    """
    if args.decisions_file:
        raw = Path(args.decisions_file).read_text(encoding="utf-8")
    elif args.decisions:
        raw = args.decisions
    else:
        raise ValueError("provide --decisions '<json>' or --decisions-file <path>")
    return json.loads(raw)


def cmd_write(args: argparse.Namespace) -> None:
    """write: resolve each clump's row indices to verbatim ids, write manifest.

    Every referenced index is validated against the candidates file; an
    out-of-range index fails loud rather than silently dropping an email.
    """
    candidates = load_candidates(args.date)
    index_to_id = {c["index"]: c["id"] for c in candidates}
    decisions = parse_decisions(args)

    clumps = {}
    for clump_key, clump in decisions.items():
        rows = clump.get("rows", [])
        bad = [r for r in rows if r not in index_to_id]
        if bad:
            raise ValueError(
                f"clump {clump_key!r} references unknown candidate rows {bad}; "
                f"valid range is 0..{len(candidates) - 1}"
            )
        # IDs copied verbatim from fetched data — never model-typed.
        clumps[str(clump_key)] = {
            "label": clump.get("label", ""),
            "ids": [index_to_id[r] for r in rows],
        }

    BRIEFS_INBOX_DIR.mkdir(parents=True, exist_ok=True)
    out = manifest_path(args.date)
    out.write_text(
        json.dumps({"date": args.date, "clumps": clumps}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    total = sum(len(c["ids"]) for c in clumps.values())
    print(f"Wrote {out} — {len(clumps)} clumps, {total} thread ids", file=sys.stderr)


def select_clump_ids(manifest: dict, clumps_arg: str) -> list:
    """Collect thread ids for the requested clumps ("all" or e.g. "1,3")."""
    clumps = manifest.get("clumps", {})
    if clumps_arg.strip().lower() == "all":
        keys = list(clumps.keys())
    else:
        keys = [k.strip() for k in clumps_arg.split(",") if k.strip()]
    ids: list = []
    for key in keys:
        if key not in clumps:
            raise ValueError(f"clump {key!r} not in manifest (have: {sorted(clumps)})")
        ids.extend(clumps[key]["ids"])
    return ids


def mark_threads_read(thread_ids: list) -> tuple:
    """Mark every given thread read (remove UNREAD) in one gog-firewall call.

    Returns `(ok_count, failures)` where `failures` is a list of the per-thread
    result records that did not succeed. Uses `--json` and inspects each
    result's `success` flag rather than the process exit code: `gmail labels
    modify` exits 0 even when individual thread ids fail (a stale/deleted id
    reports `success: false` in its result record, not via the exit code), so
    trusting the exit code would let a partial clear pass silently.
    """
    if not thread_ids:
        return 0, []
    result = subprocess.run(
        [str(GOG_FIREWALL), "gmail", "labels", "modify", *thread_ids,
         "--remove", "UNREAD", "--no-input", "--json"],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(f"mark-read failed (exit {result.returncode}): {result.stderr.strip()}")
    results = json.loads(result.stdout).get("results", [])
    ok = sum(1 for r in results if r.get("success"))
    failures = [r for r in results if not r.get("success")]
    return ok, failures


def cmd_clear(args: argparse.Namespace) -> None:
    """clear: mark the approved clumps' threads read (or preview with --dry-run)."""
    path = manifest_path(args.date)
    if not path.exists():
        raise FileNotFoundError(f"No manifest at {path}.")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    ids = select_clump_ids(manifest, args.clumps)
    if args.dry_run:
        print(f"[dry-run] would mark {len(ids)} threads read: {ids}")
        return
    ok, failures = mark_threads_read(ids)
    print(f"Marked {ok}/{len(ids)} threads read.")
    if failures:
        # Fail loud: a partial clear must be visible, not swallowed. The
        # successful threads above are already applied; surface the rest and
        # exit non-zero so a caller / job notices.
        for r in failures:
            print(f"  FAILED {r.get('threadId')}: {r.get('error')}", file=sys.stderr)
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="search unread, persist + print candidates")
    p_fetch.add_argument("--date", required=True, help="triage date, YYYY-MM-DD")
    p_fetch.add_argument("--query", action="append",
                         help="Gmail query (repeatable; default 'is:unread')")
    p_fetch.add_argument("--max", type=int, default=DEFAULT_MAX_RESULTS)
    p_fetch.set_defaults(func=cmd_fetch)

    p_write = sub.add_parser("write", help="resolve row indices to ids, write manifest")
    p_write.add_argument("--date", required=True, help="triage date, YYYY-MM-DD")
    p_write.add_argument("--decisions", help="inline JSON: {clump: {label, rows:[...]}}")
    p_write.add_argument("--decisions-file", help="path to the same JSON")
    p_write.set_defaults(func=cmd_write)

    p_clear = sub.add_parser("clear", help="mark approved clumps' threads read")
    p_clear.add_argument("--date", required=True, help="triage date, YYYY-MM-DD")
    p_clear.add_argument("--clumps", required=True, help='"all" or e.g. "1,3"')
    p_clear.add_argument("--dry-run", action="store_true")
    p_clear.set_defaults(func=cmd_clear)

    return parser


def main(argv: Optional[list] = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
