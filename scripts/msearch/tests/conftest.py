"""Shared test fixtures."""

import sys
from pathlib import Path

import pytest

# Ensure src/ is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "test_fixtures"
DAILY_DIR = FIXTURES_DIR / "daily"
REPORTS_DIR = FIXTURES_DIR / "reports"


@pytest.fixture
def fixtures_dir():
    return FIXTURES_DIR


@pytest.fixture
def daily_dir():
    return DAILY_DIR


@pytest.fixture
def reports_dir():
    return REPORTS_DIR


@pytest.fixture
def all_dirs():
    return [DAILY_DIR, REPORTS_DIR]
