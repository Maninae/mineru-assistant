"""Contract tests for `mineru_cli._stub.not_yet_implemented`.

The stub helper is invoked from 50+ verb bodies whose engines aren't wired
yet. It must:

  1. Fail loud with a non-zero exit code so scripted callers (launchd
     cron jobs, shell pipelines checking `$?`) never treat an
     un-implemented verb as a successful no-op. Prior to this contract
     the helper returned normally and every stub verb exited 0, silently
     hiding un-wired verbs from cron-monitoring.
  2. Route the notice to stderr so stdout stays clean for downstream
     `| jq` / `| grep` pipelines.
  3. Include the verb name in the notice so an operator eyeballing a
     shell transcript can tell which verb landed on the stub.
"""

from __future__ import annotations

import pytest
import typer

from mineru_cli._stub import STUB_EXIT_CODE, not_yet_implemented


def test_stub_exit_code_is_non_zero() -> None:
    """`STUB_EXIT_CODE` is a distinct, non-zero, POSIX-style code."""
    assert STUB_EXIT_CODE != 0
    # Distinct from the wrapper-missing 127 code so operators can tell
    # "engine not on disk" apart from "verb not yet wired."
    assert STUB_EXIT_CODE != 127


def test_not_yet_implemented_raises_typer_exit_with_stub_code() -> None:
    """`not_yet_implemented('memory reindex')` fails loud with the shared code."""
    with pytest.raises(typer.Exit) as exc_info:
        not_yet_implemented("memory reindex")
    assert exc_info.value.exit_code == STUB_EXIT_CODE


def test_not_yet_implemented_prints_verb_name_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The stderr notice mentions the exact verb, and stdout stays clean."""
    with pytest.raises(typer.Exit):
        not_yet_implemented("secrets set")
    captured = capsys.readouterr()
    assert "secrets set" in captured.err
    assert "not yet implemented" in captured.err.lower()
    # stdout must stay clean so downstream `| jq` / `| grep` doesn't see junk.
    assert captured.out == ""
