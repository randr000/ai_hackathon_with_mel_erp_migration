"""Tests for migration and the validation checks.

The important property under test: validation must FAIL when data is actually
corrupted. A validator that only ever passes is worse than none.
"""

from app.matching import match_accounts
from app.migration import migrate_transactions, validate_migration
from app.models import Account, AccountMapping, MappingStatus, TransactionLine


def acct(number, name, type_="", detail=""):
    return Account(
        account_number=number, account_name=name, type=type_, detail_type=detail
    )


def line(txn, number, name, amount, date="2026-01-01"):
    return TransactionLine(
        transaction_id=txn, date=date, account_number=number,
        account_name=name, amount=amount,
    )


def mapping(source_number, source_name, target_number, target_name):
    return AccountMapping(
        source_number=source_number,
        source_name=source_name,
        target_number=target_number,
        target_name=target_name,
        confidence=0.95,
        status=MappingStatus.AUTO,
    )


def sample_journal():
    """Two balanced double-entry transactions."""
    return [
        line("T1", "6100", "Sales Commissions", 100.0),
        line("T1", "2120", "Accrued Commissions", -100.0),
        line("T2", "1000", "Operating Checking", 250.50),
        line("T2", "1100", "Accounts Receivable", -250.50),
    ]


def sample_mappings():
    return [
        mapping("6100", "Sales Commissions", "61000", "Sales Commissions"),
        mapping("2120", "Accrued Commissions", "21200", "Accrued Commissions"),
        mapping("1000", "Operating Checking", "10000", "Cash - Operating"),
        mapping("1100", "Accounts Receivable", "11000", "Trade Receivables"),
    ]


class TestMigration:
    def test_rewrites_lines_onto_target_accounts(self):
        migrated, unmapped = migrate_transactions(sample_journal(), sample_mappings())
        assert len(migrated) == 4
        assert unmapped == []
        assert migrated[0].target_account == "61000"
        assert migrated[0].target_account_name == "Sales Commissions"

    def test_amounts_are_copied_verbatim(self):
        journal = sample_journal()
        migrated, _ = migrate_transactions(journal, sample_mappings())
        assert [m.amount for m in migrated] == [line.amount for line in journal]

    def test_unmapped_lines_are_collected_not_dropped(self):
        # "2999" has no mapping at all.
        journal = sample_journal() + [line("T3", "2999", "Mystery Account", 5.0)]
        migrated, unmapped = migrate_transactions(journal, sample_mappings())
        assert len(migrated) == 5        # nothing vanished
        assert len(unmapped) == 1
        assert unmapped[0].account_number == "2999"
        assert migrated[-1].target_account is None

    def test_source_provenance_is_preserved(self):
        migrated, _ = migrate_transactions(sample_journal(), sample_mappings())
        assert migrated[0].source_account == "6100"
        assert migrated[0].source_account_name == "Sales Commissions"
        assert migrated[0].transaction_id == "T1"


class TestValidationPasses:
    def test_clean_migration_passes_every_error_check(self):
        journal = sample_journal()
        migrated, _ = migrate_transactions(journal, sample_mappings())
        report = validate_migration(journal, migrated, sample_mappings())
        assert report.passed is True
        assert all(c.passed for c in report.checks if c.severity == "error")

    def test_totals_are_reported_and_match(self):
        journal = sample_journal()
        migrated, _ = migrate_transactions(journal, sample_mappings())
        report = validate_migration(journal, migrated, sample_mappings())
        assert report.totals["source_total"] == 0.0
        assert report.totals["migrated_total"] == 0.0
        assert report.totals["difference"] == 0.0

    def test_all_eight_checks_are_present(self):
        journal = sample_journal()
        migrated, _ = migrate_transactions(journal, sample_mappings())
        report = validate_migration(journal, migrated, sample_mappings())
        assert len(report.checks) == 8


class TestValidationCatchesCorruption:
    def test_dropped_line_fails_the_report(self):
        journal = sample_journal()
        migrated, _ = migrate_transactions(journal, sample_mappings())
        report = validate_migration(journal, migrated[:-1], sample_mappings())
        assert report.passed is False
        row_check = next(c for c in report.checks if "Row count" in c.name)
        assert row_check.passed is False

    def test_altered_amount_fails_the_report(self):
        journal = sample_journal()
        migrated, _ = migrate_transactions(journal, sample_mappings())
        # Simulate a rounding-style defect.
        migrated[0].amount = 100.01
        report = validate_migration(journal, migrated, sample_mappings())
        assert report.passed is False
        total_check = next(c for c in report.checks if "Journal total" in c.name)
        assert total_check.passed is False

    def test_flipped_leg_fails_the_balance_check(self):
        journal = sample_journal()
        migrated, _ = migrate_transactions(journal, sample_mappings())
        # Flip one leg of T1: the journal no longer sums to its source total.
        migrated[1].amount = 100.0
        report = validate_migration(journal, migrated, sample_mappings())
        assert report.passed is False
        balance_check = next(c for c in report.checks if "balanced" in c.name)
        assert balance_check.passed is False

    def test_unmapped_transacting_account_fails(self):
        journal = sample_journal() + [line("T3", "2999", "Mystery", 5.0)]
        migrated, _ = migrate_transactions(journal, sample_mappings())
        report = validate_migration(journal, migrated, sample_mappings())
        assert report.passed is False
        mapped_check = next(c for c in report.checks if "All transacting" in c.name)
        assert mapped_check.passed is False

    def test_null_target_fails(self):
        journal = sample_journal()
        migrated, _ = migrate_transactions(journal, sample_mappings())
        migrated[0].target_account = None
        report = validate_migration(journal, migrated, sample_mappings())
        assert report.passed is False
        null_check = next(c for c in report.checks if "without a target" in c.name)
        assert null_check.passed is False

    def test_unreviewed_mapping_is_a_warning_not_an_error(self):
        journal = sample_journal()
        mappings = sample_mappings()
        mappings[0].status = MappingStatus.NEEDS_REVIEW
        migrated, _ = migrate_transactions(journal, mappings)
        report = validate_migration(journal, migrated, mappings)
        # A warning must not fail the migration outright.
        assert report.passed is True
        warn = next(c for c in report.checks if c.severity == "warning")
        assert warn.passed is False


class TestEndToEnd:
    def test_full_pipeline_from_charts_to_validated_journal(self):
        """Charts -> match -> migrate -> validate, with no hand-written mapping."""
        source_chart = [
            acct("1000", "Operating Checking", "Cash", "Checking"),
            acct("6100", "Sales Commissions", "Expenses", "Commissions & Fees"),
            acct("2120", "Accrued Commissions", "Other Current Liability"),
        ]
        target_chart = [
            acct("10000", "Cash - Operating Bank Account", "Cash", "Checking"),
            acct("61000", "Sales Commissions", "Expenses", "Commissions & Fees"),
            acct("21200", "Accrued Commissions", "Other Current Liability"),
        ]
        journal = [
            line("T1", "6100", "Sales Commissions", 500.0),
            line("T1", "2120", "Accrued Commissions", -500.0),
            line("T2", "1000", "Operating Checking", 125.25),
            line("T2", "6100", "Sales Commissions", -125.25),
        ]

        mappings = match_accounts(source_chart, target_chart)
        assert all(m.target_number for m in mappings), "every account should map"

        migrated, unmapped = migrate_transactions(journal, mappings)
        assert unmapped == []

        report = validate_migration(journal, migrated, mappings)
        assert report.passed is True, [
            (c.name, c.detail) for c in report.checks if not c.passed
        ]
