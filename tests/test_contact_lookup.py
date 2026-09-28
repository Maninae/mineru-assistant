"""Tests for scripts/lookup_contact.py and scripts/build_contact_map.py"""
import json
import pytest
from pathlib import Path
from unittest.mock import patch
import sys
import tempfile

# Add scripts to path
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from lookup_contact import ContactResolver


class TestPhoneNormalization:
    """Tests for phone number normalization."""
    
    def test_normalize_with_plus(self):
        """Phone with + should keep only digits after +."""
        resolver = ContactResolver(mapping_path=Path("/nonexistent"))
        assert resolver._normalize_phone("+14155550123") == "+14155550123"
    
    def test_normalize_strips_formatting(self):
        """Dashes, spaces, parens should be stripped."""
        resolver = ContactResolver(mapping_path=Path("/nonexistent"))
        assert resolver._normalize_phone("(415) 555-0123") == "4155550123"
    
    def test_normalize_handles_spaces(self):
        """Spaces in number should be stripped."""
        resolver = ContactResolver(mapping_path=Path("/nonexistent"))
        assert resolver._normalize_phone("+1 415 555 0123") == "+14155550123"
    
    def test_normalize_empty_string(self):
        """Empty string should return empty."""
        resolver = ContactResolver(mapping_path=Path("/nonexistent"))
        assert resolver._normalize_phone("") == ""


class TestContactLookup:
    """Tests for contact lookup functionality."""
    
    @pytest.fixture
    def resolver_with_contacts(self, tmp_path):
        """Create a resolver with a mock contact mapping."""
        contact_map = {
            "+14155550123": "Test Person",
            "+16505551234": "Test Contact",
            "test@example.com": "Email Person"
        }
        cache_file = tmp_path / "contact_mapping.json"
        cache_file.write_text(json.dumps(contact_map))
        return ContactResolver(mapping_path=cache_file)
    
    def test_lookup_known_phone(self, resolver_with_contacts):
        """Known phone number should resolve to name."""
        result = resolver_with_contacts.get_name("+14155550123")
        assert result == "Test Person"
    
    def test_lookup_known_email(self, resolver_with_contacts):
        """Known email should resolve to name."""
        result = resolver_with_contacts.get_name("test@example.com")
        assert result == "Email Person"
    
    def test_lookup_unknown_returns_none(self, resolver_with_contacts):
        """Unknown handle should return None from get_name."""
        result = resolver_with_contacts.get_name("+19995551234")
        assert result is None
    
    def test_resolve_unknown_returns_original(self, resolver_with_contacts):
        """Unknown handle should return original value from resolve."""
        result = resolver_with_contacts.resolve("+19995551234")
        assert result == "+19995551234"
    
    def test_resolve_known_returns_name(self, resolver_with_contacts):
        """Known handle should return name from resolve."""
        result = resolver_with_contacts.resolve("+14155550123")
        assert result == "Test Person"
    
    def test_lookup_group_chat_hash(self, resolver_with_contacts):
        """Group chat identifiers (hashes) should return as-is from resolve."""
        hash_id = "bd2599f5042c4d1f804687fcc9c5d53c"
        result = resolver_with_contacts.resolve(hash_id)
        assert result == hash_id
    
    def test_lookup_phone_without_plus(self, resolver_with_contacts):
        """Phone number without +1 should still match if 10 digits."""
        # The mapping has +14155550123, lookup with 4155550123 should find it
        result = resolver_with_contacts.get_name("4155550123")
        assert result == "Test Person"


class TestReverseMapping:
    """Tests for name -> handle lookup."""
    
    @pytest.fixture
    def resolver_with_contacts(self, tmp_path):
        contact_map = {
            "+14155550123": "Test Person",
            "+16505551234": "Matt Johnson",
        }
        cache_file = tmp_path / "contact_mapping.json"
        cache_file.write_text(json.dumps(contact_map))
        return ContactResolver(mapping_path=cache_file)
    
    def test_get_handle_exact(self, resolver_with_contacts):
        """Exact name match should return handle."""
        result = resolver_with_contacts.get_handle("Test Person")
        assert result == "+14155550123"
    
    def test_get_handle_case_insensitive(self, resolver_with_contacts):
        """Name lookup should be case-insensitive."""
        result = resolver_with_contacts.get_handle("test person")
        assert result == "+14155550123"
    
    def test_get_handle_first_name_only(self, resolver_with_contacts):
        """First name should match."""
        result = resolver_with_contacts.get_handle("Matt")
        assert result == "+16505551234"


class TestSearch:
    """Tests for contact search functionality."""
    
    @pytest.fixture
    def resolver_with_contacts(self, tmp_path):
        contact_map = {
            "+14155550123": "Test Person",
            "+16505551234": "Matt Johnson",  # Fake
            "+16505559999": "Matthew Smith",  # Fake
        }
        cache_file = tmp_path / "contact_mapping.json"
        cache_file.write_text(json.dumps(contact_map))
        return ContactResolver(mapping_path=cache_file)
    
    def test_search_partial_match(self, resolver_with_contacts):
        """Partial name match should return results."""
        results = resolver_with_contacts.search("Matt")
        names = [name for _, name in results]
        assert "Matt Johnson" in names
        assert "Matthew Smith" in names
    
    def test_search_no_match(self, resolver_with_contacts):
        """Non-matching query should return empty list."""
        results = resolver_with_contacts.search("zzzznonexistent")
        assert results == []


class TestMissingMapping:
    """Tests for behavior when contact mapping doesn't exist."""
    
    def test_no_mapping_file(self):
        """Missing mapping file should not crash."""
        resolver = ContactResolver(mapping_path=Path("/nonexistent/path/mapping.json"))
        assert resolver._forward_map == {}
    
    def test_resolve_without_mapping(self):
        """Resolve should return original handle when no mapping."""
        resolver = ContactResolver(mapping_path=Path("/nonexistent/path/mapping.json"))
        result = resolver.resolve("+14155550123")
        assert result == "+14155550123"
