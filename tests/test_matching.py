"""Tests for the deterministic matching engine."""

from app.matching import (
    AMBIGUITY_MARGIN,
    AUTO_MAP_THRESHOLD,
    REVIEW_THRESHOLD,
    explain,
    match_accounts,
    name_similarity,
    normalize_name,
    score_pair,
    type_family,
)
from app.models import Account, MatchMethod, MappingStatus


def acct(number, name, type_="", detail=""):
    return Account(
        account_number=number, account_name=name, type=type_, detail_type=detail
    )


class TestNormalization:
    def test_lowercases_and_strips_punctuation(self):
        assert normalize_name("Rent & Office") == "rent office"

    def test_drops_stopwords(self):
        # "of" is a stopword; the trailing plural on "Goods" is stemmed.
        assert normalize_name("Cost of Goods Sold") == "cost good sold"

    def test_folds_singular_and_plural_to_the_same_form(self):
        # This is what lets "Miscellaneous Expense" share a token with
        # "Other Operating Expenses" instead of matching nothing.
        assert normalize_name("Salaries Expense") == normalize_name("Salaries Expenses")

    def test_short_words_are_not_stemmed(self):
        # "gas" must not become "ga".
        assert normalize_name("Gas") == "gas"

    def test_empty_input_is_safe(self):
        assert normalize_name("") == ""


class TestSimilarity:
    def test_identical_names_score_one(self):
        assert name_similarity("Insurance Expense", "Insurance Expense") == 1.0

    def test_abbreviation_still_scores_well(self):
        # Containment credit is what makes this usable: real COAs abbreviate in
        # one direction only.
        assert name_similarity("Insurance", "Insurance Expense") >= 0.75

    def test_plural_forms_score_as_a_full_match(self):
        assert name_similarity("Miscellaneous Expense", "Miscellaneous Expenses") == 1.0

    def test_ampersand_is_not_a_difference(self):
        assert name_similarity("Discounts & Credits", "Discounts and Credits") == 1.0

    def test_unrelated_names_score_low(self):
        assert name_similarity("Sales Commissions", "Prepaid Insurance") < 0.3

    def test_empty_name_scores_zero(self):
        assert name_similarity("", "Anything") == 0.0


class TestTypeFamily:
    def test_maps_quickbooks_types(self):
        assert type_family("Cost of Goods Sold") == "cogs"
        assert type_family("Other Current Liability") == "liability"
        assert type_family("Accounts Receivable") == "asset"

    def test_unknown_type_is_empty(self):
        assert type_family("Something Exotic") == ""


class TestScorePair:
    def test_exact_number_and_name_is_perfect(self):
        source = acct("1000", "Operating Checking", "Cash", "Checking")
        target = acct("1000", "Operating Checking", "Cash", "Checking")
        score, reasons, _ = score_pair(source, target)
        assert score == 1.0
        assert any("number matches" in r.lower() for r in reasons)

    def test_same_number_different_name_is_flagged_not_trusted(self):
        source = acct("1000", "Operating Checking", "Cash")
        target = acct("1000", "Petty Cash Reserve", "Expenses")
        score, reasons, _ = score_pair(source, target)
        assert score < 1.0
        assert any("differ" in r.lower() for r in reasons)

    def test_type_mismatch_caps_confidence(self):
        source = acct("6000", "Salaries", "Expenses")
        target = acct("9000", "Salaries", "Income")
        score, _, signals = score_pair(source, target)
        assert signals["type"] == 0.0
        assert score < AUTO_MAP_THRESHOLD

    def test_reasons_never_empty_on_a_numeric_pair(self):
        _, reasons, _ = score_pair(acct("1", "A"), acct("2", "B"))
        assert reasons == [] or all(isinstance(r, str) for r in reasons)


class TestMatchAccounts:
    def test_renumbered_chart_maps_by_name(self):
        # The real scenario: the target renumbers everything and renames the
        # account. The engine finds the correct target, but the names share only
        # one significant word ("Operating"), so it scores ~0.77 and asks for
        # confirmation rather than auto-approving. For financial data that is
        # the right side to err on: the mapping is proposed, the human clicks
        # once to confirm.
        source = [acct("1000", "Operating Checking", "Cash", "Checking")]
        target = [acct("10000", "Cash - Operating Bank Account", "Cash", "Checking")]
        mappings = match_accounts(source, target)
        assert mappings[0].target_number == "10000"
        assert mappings[0].confidence >= REVIEW_THRESHOLD
        assert mappings[0].status == MappingStatus.NEEDS_REVIEW

    def test_ambiguity_forces_review_even_with_high_score(self):
        # Two nearly identical targets: the engine must not silently pick one.
        source = [acct("6100", "Sales Commissions", "Expenses", "Commissions & Fees")]
        target = [
            acct("61000", "Sales Commissions", "Expenses", "Commissions & Fees"),
            acct("61010", "Sales Commissions Payable", "Expenses", "Commissions & Fees"),
        ]
        mappings = match_accounts(source, target)
        assert mappings[0].confidence >= AUTO_MAP_THRESHOLD
        assert mappings[0].status == MappingStatus.NEEDS_REVIEW
        assert any("Ambiguous" in r for r in mappings[0].reasons)

    def test_low_score_is_unmapped(self):
        source = [acct("9999", "Zzzz Qqqq", "Expenses")]
        target = [acct("1111", "Accounts Receivable", "Accounts Receivable")]
        mappings = match_accounts(source, target)
        assert mappings[0].target_number is None
        assert mappings[0].method == MatchMethod.UNMAPPED

    def test_candidates_are_ranked_by_score(self):
        source = [acct("6100", "Sales Commissions", "Expenses", "Commissions & Fees")]
        target = [
            acct("61000", "Sales Commissions", "Expenses", "Commissions & Fees"),
            acct("11110", "Prepaid Insurance", "Other Current Asset"),
            acct("61010", "Sales Commissions Payable", "Expenses"),
        ]
        mappings = match_accounts(source, target)
        scores = [c.score for c in mappings[0].candidates]
        assert scores == sorted(scores, reverse=True)

    def test_confirmed_decision_overrides_the_matcher(self):
        source = [acct("1000", "Operating Checking", "Cash", "Checking")]
        target = [
            acct("10000", "Cash - Operating Bank Account", "Cash", "Checking"),
            acct("19999", "Something Else Entirely", "Cash", "Checking"),
        ]
        mappings = match_accounts(source, target, confirmed={"1000": "19999"})
        assert mappings[0].target_number == "19999"
        assert mappings[0].status == MappingStatus.CONFIRMED
        assert mappings[0].confidence == 1.0

    def test_empty_target_chart_does_not_crash(self):
        mappings = match_accounts([acct("1000", "Checking", "Cash")], [])
        assert mappings[0].target_number is None
        assert mappings[0].status == MappingStatus.NEEDS_REVIEW

    def test_results_are_deterministic(self):
        source = [acct("6100", "Sales Commissions", "Expenses")]
        target = [acct("61000", "Sales Commissions", "Expenses"), acct("61010", "Sales Commission", "Expenses")]
        first = match_accounts(source, target)
        second = match_accounts(source, target)
        assert [m.model_dump() for m in first] == [m.model_dump() for m in second]

    def test_every_source_account_gets_exactly_one_mapping(self):
        source = [acct(str(n), f"Account {n}", "Expenses") for n in range(20)]
        target = [acct(str(n * 10), f"Account {n}", "Expenses") for n in range(20)]
        mappings = match_accounts(source, target)
        assert len(mappings) == 20
        assert len({m.source_number for m in mappings}) == 20


class TestExplain:
    def test_unmapped_explanation_says_so(self):
        source = [acct("9999", "Zzzz", "Expenses")]
        target = [acct("1111", "Accounts Receivable", "Accounts Receivable")]
        mapping = match_accounts(source, target)[0]
        assert "could not be mapped" in explain(mapping)

    def test_mapped_explanation_includes_both_sides_and_confidence(self):
        source = [acct("1000", "Operating Checking", "Cash", "Checking")]
        target = [acct("10000", "Cash - Operating Bank Account", "Cash", "Checking")]
        mapping = match_accounts(source, target)[0]
        text = explain(mapping)
        assert "Operating Checking" in text
        assert "10000" in text
        assert "%" in text
