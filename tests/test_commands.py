"""Tests for the spoken-command parser."""

from app.commands import parse_command
from app.models import AccountMapping, MappingStatus


def mapping(num, name):
    return AccountMapping(
        source_number=num, source_name=name, target_number=f"{num}0",
        target_name=name, confidence=0.9, status=MappingStatus.NEEDS_REVIEW,
    )


SAMPLE = [
    mapping("1000", "Operating Checking"),
    mapping("6100", "Sales Commissions"),
    mapping("2120", "Accrued Commissions"),
]


class TestIntentDetection:
    def test_approve_top(self):
        cmd = parse_command("please approve the top match", SAMPLE)
        assert cmd["intent"] == "approve_top"
        assert cmd["ok"] is True

    def test_map_by_number_pair(self):
        cmd = parse_command("map 1000 to 10000", SAMPLE)
        assert cmd["intent"] == "map"
        assert cmd["source_number"] == "1000"
        assert cmd["target_number"] == "10000"

    def test_approve_by_number(self):
        cmd = parse_command("approve account 6100", SAMPLE)
        assert cmd["intent"] == "approve"
        assert cmd["source_number"] == "6100"

    def test_download_mappings(self):
        cmd = parse_command("download the mappings file", SAMPLE)
        assert cmd["intent"] == "download_mappings"

    def test_download_migrated(self):
        cmd = parse_command("download the migrated transactions", SAMPLE)
        assert cmd["intent"] == "download_migrated"

    def test_migrate(self):
        cmd = parse_command("run the migration", SAMPLE)
        assert cmd["intent"] == "migrate"

    def test_clear(self):
        cmd = parse_command("clear all my decisions", SAMPLE)
        assert cmd["intent"] == "clear"

    def test_explain_by_number(self):
        cmd = parse_command("explain account 2120", SAMPLE)
        assert cmd["intent"] == "explain"
        assert cmd["source_number"] == "2120"

    def test_explain_report_when_no_account(self):
        cmd = parse_command("explain the results", SAMPLE)
        assert cmd["intent"] == "explain_report"

    def test_unknown_returns_ok_false(self):
        cmd = parse_command("make me a sandwich", SAMPLE)
        assert cmd["ok"] is False
        assert cmd["message"]


class TestNumberWords:
    def test_spoken_number_words_resolve(self):
        cmd = parse_command("map one thousand to ten thousand", SAMPLE)
        assert cmd["source_number"] == "1000"
        assert cmd["target_number"] == "10000"

    def test_spelled_account_name_resolves(self):
        cmd = parse_command("approve sales commissions", SAMPLE)
        assert cmd["intent"] == "approve"
        assert cmd["source_number"] == "6100"

    def test_ambiguous_name_resolves_to_none(self):
        # Two mappings share the token "commissions".
        cmd = parse_command("approve commissions", SAMPLE)
        assert cmd["source_number"] is None


class TestEmpty:
    def test_empty_transcript(self):
        cmd = parse_command("", SAMPLE)
        assert cmd["ok"] is False

    def test_whitespace_transcript(self):
        cmd = parse_command("   ", SAMPLE)
        assert cmd["ok"] is False
