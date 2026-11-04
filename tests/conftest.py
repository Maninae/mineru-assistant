"""Test-suite hooks: report how many operator banned literals the leak gates loaded.

The count is printed in the pytest header on every run, so a run on the operator's
machine that loaded zero private literals (file missing, wrong MINERU_HOME) is visible.
"""

import tempfile

import pytest

from leak_gate_literals import OPERATOR_BANNED_LITERALS, describe_operator_banned_literals


def pytest_report_header(config):
    """Add the leak-gate operator literal count (never the values) to the header."""
    return describe_operator_banned_literals(OPERATOR_BANNED_LITERALS)


@pytest.fixture
def system_tempdir_under_tmp_path(tmp_path, monkeypatch):
    """Route `tempfile` (in-process and child processes) into this test's tmp_path.

    `mineru cron run --dry-run` writes its transcript to a fresh `mkdtemp()` dir by design,
    so without this every suite run left `mineru-cron-dryrun-*` dirs in the real $TMPDIR.
    """
    isolated_tempdir = tmp_path / "system-tempdir"
    isolated_tempdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(isolated_tempdir))
    monkeypatch.setenv("TMPDIR", str(isolated_tempdir))
    return isolated_tempdir
