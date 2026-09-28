"""Banned-literal sets for the public-engine leak gates.

The public repo ships only GENERIC banned literals (third-party product tokens a
generic template must parameterize). The operator's own fingerprints (names,
affiliations, addresses, thread ids, personal examples) never ship; they load at
test time from a private file plus an env var:

- `MINERU_BANNED_LITERALS_FILE`: path to a file with one literal per line; blank
  lines and `#` comments are skipped. Default `$MINERU_HOME/config/banned-literals.txt`
  (`MINERU_HOME` defaults to `~/.mineru`), so the operator's machine runs the full
  check with no env setup. A missing file means zero operator literals.
- `MINERU_BANNED_LITERALS`: comma-separated literals, merged on top of the file.

`engine/config/banned-literals.example.txt` shows the file format.
"""

import os
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

BANNED_LITERALS_FILE_ENV_VAR = "MINERU_BANNED_LITERALS_FILE"
BANNED_LITERALS_ENV_VAR = "MINERU_BANNED_LITERALS"
DEFAULT_BANNED_LITERALS_FILE_NAME = "banned-literals.txt"

# Product tokens an engine template or shipping artifact must not hard-code: a
# template naming a vendor instead of `{{FINANCE_CLI_PRODUCT}}` has slipped a
# specific install into a generic recipe. Code is allowed to name products (the
# Monarch wrapper names Monarch), so the code gate uses the narrower set below.
GENERIC_TEMPLATE_BANNED_LITERALS: Tuple[str, ...] = ("Monarch", "Tesla", "Roblox")
GENERIC_CODE_BANNED_LITERALS: Tuple[str, ...] = ("Roblox", "roblox")


class OperatorBannedLiterals(NamedTuple):
    """The operator's private banned literals and where they came from."""

    literals: Tuple[str, ...]
    literals_file_path: Path
    count_from_file: int
    count_from_env: int


def resolve_banned_literals_file_path(environ: Optional[dict] = None) -> Path:
    """Return the operator literals file path from the env, or the MINERU_HOME default."""
    environ = os.environ if environ is None else environ
    explicit_path = environ.get(BANNED_LITERALS_FILE_ENV_VAR)
    if explicit_path:
        return Path(explicit_path).expanduser()
    mineru_home = Path(environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
    return mineru_home / "config" / DEFAULT_BANNED_LITERALS_FILE_NAME


def parse_banned_literals_text(text: str) -> List[str]:
    """One literal per line; strips surrounding whitespace, skips blanks and `#` comments."""
    literals = []
    for line in text.splitlines():
        stripped_line = line.strip()
        if stripped_line and not stripped_line.startswith("#"):
            literals.append(stripped_line)
    return literals


def load_operator_banned_literals(environ: Optional[dict] = None) -> OperatorBannedLiterals:
    """Merge the operator's file literals with the comma-separated env literals, deduplicated."""
    environ = os.environ if environ is None else environ
    literals_file_path = resolve_banned_literals_file_path(environ)
    file_literals: List[str] = []
    if literals_file_path.is_file():
        file_literals = parse_banned_literals_text(literals_file_path.read_text(encoding="utf-8"))
    env_literals = [
        literal.strip()
        for literal in environ.get(BANNED_LITERALS_ENV_VAR, "").split(",")
        if literal.strip()
    ]
    merged_literals = tuple(dict.fromkeys(file_literals + env_literals))
    return OperatorBannedLiterals(
        literals=merged_literals,
        literals_file_path=literals_file_path,
        count_from_file=len(file_literals),
        count_from_env=len(env_literals),
    )


def describe_operator_banned_literals(operator_literals: OperatorBannedLiterals) -> str:
    """One log line with counts and the file path; never the literal values."""
    return (
        f"leak gate: {len(operator_literals.literals)} operator banned literals loaded "
        f"({operator_literals.count_from_file} from {operator_literals.literals_file_path}, "
        f"{operator_literals.count_from_env} from ${BANNED_LITERALS_ENV_VAR})"
    )


OPERATOR_BANNED_LITERALS = load_operator_banned_literals()

# The fictional lines in the shipped example file. The gates allow them in the
# example file and the loader tests, so an operator who starts from a verbatim
# copy of the example does not trip the gate on the example itself.
EXAMPLE_BANNED_LITERALS_FILE_PATH = (
    Path(__file__).resolve().parent.parent / "engine" / "config" / "banned-literals.example.txt"
)
FICTIONAL_EXAMPLE_BANNED_LITERALS: Tuple[str, ...] = tuple(
    parse_banned_literals_text(EXAMPLE_BANNED_LITERALS_FILE_PATH.read_text(encoding="utf-8"))
)


def describe_banned_literal_for_failure(banned_literal: str) -> str:
    """Name a banned literal in a failure message without echoing private values.

    Generic product tokens are public, so they print as-is. Operator literals print
    as their position in the merged private list, so a failure log shared or kept
    in CI does not re-leak the value it caught.
    """
    if banned_literal in GENERIC_TEMPLATE_BANNED_LITERALS + GENERIC_CODE_BANNED_LITERALS:
        return repr(banned_literal)
    literal_position = OPERATOR_BANNED_LITERALS.literals.index(banned_literal) + 1
    return (
        f"operator literal #{literal_position} of {len(OPERATOR_BANNED_LITERALS.literals)} "
        f"(file entries first, then ${BANNED_LITERALS_ENV_VAR})"
    )
