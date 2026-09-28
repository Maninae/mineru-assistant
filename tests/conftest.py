"""Test-suite hooks: report how many operator banned literals the leak gates loaded.

The count is printed in the pytest header on every run, so a run on the operator's
machine that loaded zero private literals (file missing, wrong MINERU_HOME) is visible.
"""

from leak_gate_literals import OPERATOR_BANNED_LITERALS, describe_operator_banned_literals


def pytest_report_header(config):
    """Add the leak-gate operator literal count (never the values) to the header."""
    return describe_operator_banned_literals(OPERATOR_BANNED_LITERALS)
