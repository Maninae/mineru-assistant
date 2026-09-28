# Tests

Unit tests for scripts in `/scripts` and `/bin`.

## Purpose

Verify that our operational scripts and binaries work correctly:
- Contact lookup and resolution
- Firewall wrappers (gog-firewall, imsg-firewall)
- Delivery scripts
- Any other utilities

## Running Tests

Using `uv` (recommended, no venv setup needed):

```bash
# Run all tests
uv run --with pytest pytest tests/ -v

# Run a specific test file
uv run --with pytest pytest tests/test_contact_lookup.py -v

# Run with coverage
uv run --with pytest --with pytest-cov pytest tests/ --cov=scripts --cov-report=term-missing
```

Or with a venv:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pytest pytest-cov
pytest tests/ -v
```

## Writing Tests

Each test file should:
1. Be named `test_<module>.py`
2. Use pytest conventions
3. Mock external dependencies (databases, APIs, network calls)

### Example Structure

```python
"""Tests for scripts/lookup_contact.py"""
import pytest
from pathlib import Path
import sys

# Add scripts to path
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


class TestContactLookup:
    """Tests for contact lookup functionality."""
    
    def test_normalize_phone_number(self):
        """Phone numbers should normalize to E.164 format."""
        # ...
    
    def test_lookup_known_contact(self):
        """Known contacts should resolve to names."""
        # ...
    
    def test_lookup_unknown_returns_original(self):
        """Unknown handles should return the original value."""
        # ...
```

## Test Categories

| Directory/File | Tests For |
|----------------|-----------|
| `test_contact_lookup.py` | `scripts/lookup_contact.py`, `scripts/build_contact_map.py` |
| `test_firewall.py` | `bin/imsg-firewall`, `bin/gog-firewall` |
| `test_delivery.py` | `scripts/deliver-output.py` |

## Requirements

Using uv (no install needed — it handles deps automatically):
```bash
brew install uv  # if not installed
```

Or manually:
```bash
pip install pytest pytest-cov
```

## CI Notes

Tests should be runnable without:
- Network access (mock external APIs)
- Full Disk Access (mock Contacts database reads)
- Active OpenClaw session (mock gateway calls)

Use fixtures to provide test data instead of hitting real systems.
