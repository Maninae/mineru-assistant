"""Tests for scripts/lookup_contact.py ExclusionList: a missing or malformed opt-out file fails loud."""
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from lookup_contact import ALLOW_MISSING_EXCLUSIONS_ENV_VAR, ExclusionList


def test_missing_exclusion_file_raises_naming_the_path(tmp_path, monkeypatch):
    monkeypatch.delenv(ALLOW_MISSING_EXCLUSIONS_ENV_VAR, raising=False)
    missing_path = tmp_path / "excluded_imsg_contacts.json"
    with pytest.raises(FileNotFoundError) as excinfo:
        ExclusionList(missing_path)
    assert str(missing_path) in str(excinfo.value)
    assert ALLOW_MISSING_EXCLUSIONS_ENV_VAR in str(excinfo.value)


def test_missing_exclusion_file_allowed_by_explicit_env(tmp_path, monkeypatch):
    monkeypatch.setenv(ALLOW_MISSING_EXCLUSIONS_ENV_VAR, "1")
    exclusions = ExclusionList(tmp_path / "excluded_imsg_contacts.json")
    assert not exclusions.is_excluded("+15550100001")


def test_env_value_other_than_one_does_not_allow_missing(tmp_path, monkeypatch):
    monkeypatch.setenv(ALLOW_MISSING_EXCLUSIONS_ENV_VAR, "true")
    with pytest.raises(FileNotFoundError):
        ExclusionList(tmp_path / "excluded_imsg_contacts.json")


def test_malformed_exclusion_file_raises(tmp_path, monkeypatch):
    monkeypatch.setenv(ALLOW_MISSING_EXCLUSIONS_ENV_VAR, "1")
    malformed_path = tmp_path / "excluded_imsg_contacts.json"
    malformed_path.write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        ExclusionList(malformed_path)


def test_listed_identifier_is_excluded_after_normalization(tmp_path, monkeypatch):
    monkeypatch.delenv(ALLOW_MISSING_EXCLUSIONS_ENV_VAR, raising=False)
    exclusion_path = tmp_path / "excluded_imsg_contacts.json"
    exclusion_path.write_text(json.dumps({"identifiers": ["+1 (555) 010-0002", "Carol@Example.com"]}))
    exclusions = ExclusionList(exclusion_path)
    assert exclusions.is_excluded("+15550100002")
    assert exclusions.is_excluded("carol@example.com")
    assert not exclusions.is_excluded("+15550100003")
