"""Matching rules for the indirect probes. No network access."""

import pytest

from app.probes import _normalize


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Airbnb, Inc.", "airbnb"),
        ("Bluesky PBLLC", "bluesky"),
        ("Discord Inc.", "discord"),
        ("Anysphere", "anysphere"),
        ("Shopify Inc.", "shopify"),
        ("X Corp.", "x"),
        ("  Spaced   Out  Ltd  ", "spaced out"),
    ],
)
def test_normalize_strips_corporate_noise(raw, expected):
    assert _normalize(raw) == expected


def test_normalize_is_case_and_punctuation_insensitive():
    assert _normalize("EXPO, INC.") == _normalize("expo inc")


def test_different_companies_do_not_collide():
    """The Grok Bot case: Anysphere's app must not match X Corp's app."""
    assert _normalize("Anysphere") != _normalize("X Corp.")
    assert _normalize("Anysphere") not in _normalize("X Corp. builds Grok")
