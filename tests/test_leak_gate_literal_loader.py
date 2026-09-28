"""Tests for leak_gate_literals: operator banned literals load from a private file plus env."""

from pathlib import Path

from leak_gate_literals import (
    BANNED_LITERALS_ENV_VAR,
    BANNED_LITERALS_FILE_ENV_VAR,
    describe_operator_banned_literals,
    load_operator_banned_literals,
    parse_banned_literals_text,
    resolve_banned_literals_file_path,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_FILE_PATH = REPO_ROOT / "engine" / "config" / "banned-literals.example.txt"


def test_parse_skips_blanks_and_comments_and_strips_whitespace():
    parsed = parse_banned_literals_text("# header\n\n  Quillfeather Lane  \n#x\nNorthwind\n")
    assert parsed == ["Quillfeather Lane", "Northwind"]


def test_default_path_is_under_mineru_home(tmp_path):
    resolved = resolve_banned_literals_file_path({"MINERU_HOME": str(tmp_path)})
    assert resolved == tmp_path / "config" / "banned-literals.txt"


def test_explicit_file_env_overrides_mineru_home(tmp_path):
    explicit_path = tmp_path / "elsewhere.txt"
    resolved = resolve_banned_literals_file_path(
        {"MINERU_HOME": str(tmp_path / "home"), BANNED_LITERALS_FILE_ENV_VAR: str(explicit_path)}
    )
    assert resolved == explicit_path


def test_missing_file_and_no_env_loads_zero(tmp_path):
    loaded = load_operator_banned_literals({"MINERU_HOME": str(tmp_path)})
    assert loaded.literals == ()
    assert "0 operator banned literals loaded" in describe_operator_banned_literals(loaded)


def test_file_is_read_automatically_from_mineru_home(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "banned-literals.txt").write_text("Quillfeather Lane\nNorthwind Rowing Club\n")
    loaded = load_operator_banned_literals({"MINERU_HOME": str(tmp_path)})
    assert loaded.literals == ("Quillfeather Lane", "Northwind Rowing Club")
    assert loaded.count_from_file == 2


def test_file_and_env_merge_deduplicated_in_order(tmp_path):
    literals_file_path = tmp_path / "lits.txt"
    literals_file_path.write_text("Quillfeather Lane\nNorthwind Rowing Club\n")
    loaded = load_operator_banned_literals({
        BANNED_LITERALS_FILE_ENV_VAR: str(literals_file_path),
        BANNED_LITERALS_ENV_VAR: "Emberly Gazette, Quillfeather Lane,",
    })
    assert loaded.literals == ("Quillfeather Lane", "Northwind Rowing Club", "Emberly Gazette")
    assert (loaded.count_from_file, loaded.count_from_env) == (2, 2)


def test_description_never_contains_literal_values(tmp_path):
    literals_file_path = tmp_path / "lits.txt"
    literals_file_path.write_text("Quillfeather Lane\n")
    loaded = load_operator_banned_literals({BANNED_LITERALS_FILE_ENV_VAR: str(literals_file_path)})
    assert "Quillfeather" not in describe_operator_banned_literals(loaded)


def test_shipped_example_file_parses_to_two_fictional_literals():
    assert len(parse_banned_literals_text(EXAMPLE_FILE_PATH.read_text())) == 2
