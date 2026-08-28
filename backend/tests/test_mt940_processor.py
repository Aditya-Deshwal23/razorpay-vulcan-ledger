"""Tests for backend.parsers.mt940_processor.LegacyBankParser."""
from parsers.mt940_processor import LegacyBankParser


def test_hdfc_extracts_utr():
    narration = "/INF/NEFT CR:HDFC000000000001/ACME CORP"
    assert LegacyBankParser.extract_utr("HDFC", narration) == "HDFC000000000001"


def test_hdfc_case_insensitive_bank_name():
    narration = "/INF/NEFT CR:HDFC000000000001/ACME CORP"
    assert LegacyBankParser.extract_utr("hdfc", narration) == "HDFC000000000001"


def test_icici_extracts_utr():
    narration = "/TXT/NEFT-ICICIUTR000001234567"
    result = LegacyBankParser.extract_utr("ICICI", narration)
    assert result is not None and result.startswith("ICICIUTR")


def test_icici_chargeback_narration_has_no_utr():
    # doc2's "Previous cycle chargeback" scenario: lowercase, no real UTR.
    narration = "/TXT/NEFT-chargeback"
    assert LegacyBankParser.extract_utr("ICICI", narration) is None


def test_axis_extracts_utr():
    narration = "NEFT-AXISBANKUTR000012345"
    result = LegacyBankParser.extract_utr("AXIS", narration)
    assert result is not None and result.startswith("AXISBANKUTR")


def test_unknown_bank_falls_back_to_generic_pattern():
    narration = "some other bank format CITIUTR0000012345 trailing text"
    result = LegacyBankParser.extract_utr("CITI", narration)
    assert result == "CITIUTR0000012345"


def test_missing_utr_returns_none_not_raise():
    assert LegacyBankParser.extract_utr("ICICI", "random text with no utr") is None


def test_none_narration_returns_none():
    assert LegacyBankParser.extract_utr("HDFC", None) is None


def test_empty_narration_returns_none():
    assert LegacyBankParser.extract_utr("HDFC", "") is None


def test_whitespace_only_bank_name_falls_back():
    narration = "FALLBACKUTR00000012345 somewhere in text"
    assert LegacyBankParser.extract_utr("   ", narration) == "FALLBACKUTR00000012345"