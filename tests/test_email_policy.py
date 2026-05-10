"""Tests for core.email_policy.validate_viewer_email."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from email_validator import EmailNotValidError

from core.email_policy import validate_viewer_email


def test_empty():
    n, e = validate_viewer_email("")
    assert n is None and e is not None


@patch("core.email_policy.validate_email")
def test_disposable_domain_rejected(mock_validate):
    mock_validate.return_value = SimpleNamespace(email="user@yopmail.com")
    n, e = validate_viewer_email("user@yopmail.com")
    assert n is None
    assert "jetables" in (e or "").lower() or "temporaires" in (e or "").lower()
    mock_validate.assert_called_once()


@patch("core.email_policy.validate_email")
def test_normalized_returned(mock_validate):
    mock_validate.return_value = SimpleNamespace(email="normalized@example.com")
    n, e = validate_viewer_email("  Normalized@Example.COM ")
    assert e is None
    assert n == "normalized@example.com"


@patch("core.email_policy.validate_email")
def test_invalid_or_unreachable_domain(mock_validate):
    mock_validate.side_effect = EmailNotValidError("domain failed")
    n, e = validate_viewer_email("a@nonexistent-domain-xyz.invalid")
    assert n is None
    assert e is not None
    assert "invalide" in e.lower() or "injoignable" in e.lower()


@pytest.mark.skip(reason="Requires DNS; run manually if needed")
def test_real_domain_gmail():
    n, e = validate_viewer_email("test.user@gmail.com")
    assert e is None
    assert n is not None
    assert "@" in n
