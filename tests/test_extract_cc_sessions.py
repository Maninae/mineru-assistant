"""extract-cc-sessions: which sessions survive the too-short filter."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "extract-cc-sessions.py"
spec = importlib.util.spec_from_file_location("extract_cc_sessions", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def msgs(*texts):
    return [{"role": "user" if i % 2 == 0 else "assistant", "text": t} for i, t in enumerate(texts)]


def test_recurring_prompt_is_recognized_with_relative_and_absolute_paths():
    assert mod.is_recurring_job_session(msgs("Read recurring/daily-transactions.md and execute the job."))
    assert mod.is_recurring_job_session(msgs("Read /Users/x/.mineru/recurring/morning-brief.md and execute the job.", "done"))


def test_interactive_and_custom_prompt_sessions_are_not_recurring():
    assert not mod.is_recurring_job_session([])
    assert not mod.is_recurring_job_session(msgs("hey can you check my calendar"))
    # The consolidation job builds its own prompt from the instruction file; it is long anyway.
    assert not mod.is_recurring_job_session(msgs("# Consolidate Daily Memories\n\nYou are ..."))
    assert not mod.is_recurring_job_session(msgs("Please Read recurring/x.md and execute the job."))


def test_short_filter_keeps_recurring_sessions_only():
    short_job = msgs("Read recurring/daily-transactions.md and execute the job.", "Brief written.")
    short_chat = msgs("thanks!", "Anytime.")
    assert len(short_job) < mod.MIN_SESSION_MESSAGES and mod.is_recurring_job_session(short_job)
    assert len(short_chat) < mod.MIN_SESSION_MESSAGES and not mod.is_recurring_job_session(short_chat)
