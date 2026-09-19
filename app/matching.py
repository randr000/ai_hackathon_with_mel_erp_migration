"""Deterministic chart-of-accounts matcher.

Scores every source account against every target account using four independent
signals (number, name, type, detail type), combines them into a confidence
score, and records *why* each score came out the way it did. No LLM is involved,
so results are reproducible and free.

The engine also reports ambiguity: when the runner-up candidate scores close to
the winner, the mapping is flagged for review even if the top score is high,
because that is exactly the case a human should adjudicate.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from .models import Account, AccountMapping, Candidate, MatchMethod, MappingStatus

# Confidence at or above this auto-approves a mapping.
AUTO_MAP_THRESHOLD = 0.85
# Below this, the mapping is treated as having no credible candidate.
REVIEW_THRESHOLD = 0.60
# If the top two candidates are within this margin, the pair is ambiguous.
AMBIGUITY_MARGIN = 0.10

# Words that carry no discriminating power in account names.
_STOPWORDS = {
    "and", "the", "of", "for", "to", "a", "an", "on", "in", "at", "or",
}

# QuickBooks-style first-digit ranges. Used only as a weak corroborating signal.
_RANGE_FAMILIES = {
    "1": "asset",
    "2": "liability",
    "3": "equity",
    "4": "income",
    "5": "cogs",
    "6": "expense",
    # 7-9 are deliberately absent. In real charts a 7xxx account is as often
    # "Other Income" as it is an expense, so deriving a family from the leading
    # digit there manufactures a signal instead of reporting evidence.
}

# Explicit type -> family, checked in order. Longer phrases come first so that
# "Other Current Liability" is not caught by a bare "liability" rule that would
# also mis-swallow something unexpected.
_TYPE_FAMILIES: list[tuple[str, str]] = [
    ("cost of goods", "cogs"),
    ("accounts receivable", "asset"),
    ("accumulated depreciation", "asset"),
    ("accounts payable", "liability"),
    ("credit card", "liability"),
    ("current asset", "asset"),
    ("current liability", "liability"),
    ("fixed asset", "asset"),
    ("other asset", "asset"),
    ("long term liability", "liability"),
    ("other liability", "liability"),
    ("bank", "asset"),
    ("cash", "asset"),
    ("receivable", "asset"),
    ("payable", "liability"),
    ("equity", "equity"),
    ("other income", "income"),
    ("income", "income"),
    ("revenue", "income"),
    ("sales", "income"),
    ("expense", "expense"),
    ("asset", "asset"),
    ("liability", "liability"),
]


def _stem(token: str) -> str:
    """Fold a trailing plural so that singular and plural forms compare equal.

    Without this, "Miscellaneous Expense" and "Other Operating Expenses" share
    no token at all, and the matcher prefers unrelated accounts whose names
    happen to agree in number.
    """
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def normalize_name(value: str) -> str:
    """Lowercase, drop punctuation and filler words, stem, collapse whitespace."""
    text = (value or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(
        _stem(t) for t in text.split() if t and t not in _STOPWORDS
    )


def name_tokens(value: str) -> set[str]:
    return set(normalize_name(value).split())


def type_family(account_type: str) -> str:
    """Collapse a vendor-specific account type into a coarse family."""
    text = (account_type or "").lower()
    for needle, family in _TYPE_FAMILIES:
        if needle in text:
            return family
    return ""


def range_family(account_number: str) -> str:
    first = (account_number or "").strip()[:1]
    return _RANGE_FAMILIES.get(first, "")


def name_similarity(a: str, b: str) -> float:
    """Blend edit-distance with token containment.

    Containment matters because real COAs abbreviate in one direction only:
    "Insurance" should score well against "Insurance Expense". Plain edit
    distance undervalues that, so the two are averaged.
    """
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0

    ratio = SequenceMatcher(None, na, nb).ratio()
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        containment = 0.0
    else:
        containment = len(ta & tb) / min(len(ta), len(tb))
    return round(0.5 * ratio + 0.5 * containment, 4)


def _detail_similarity(a: str, b: str) -> float:
    da, db = normalize_name(a), normalize_name(b)
    if not da or not db:
        # Absent detail types are neutral, not a penalty.
        return 0.5
    if da == db:
        return 1.0
    return round(SequenceMatcher(None, da, db).ratio(), 4)


def score_pair(source: Account, target: Account) -> tuple[float, list[str], dict[str, float]]:
    """Score one source/target account pair.

    Returns (score, reasons, signals) where `signals` exposes the individual
    components so callers can explain a decision analytically.
    """
    reasons: list[str] = []

    name_sim = name_similarity(source.account_name, target.account_name)
    src_family, tgt_family = type_family(source.type), type_family(target.type)
    if src_family and tgt_family:
        type_score = 1.0 if src_family == tgt_family else 0.0
    else:
        type_score = 0.5  # unknown type is neutral rather than disqualifying
    detail_score = _detail_similarity(source.detail_type, target.detail_type)

    src_range, tgt_range = range_family(source.account_number), range_family(target.account_number)
    if src_range and tgt_range:
        range_score = 1.0 if src_range == tgt_range else 0.0
    else:
        range_score = 0.5

    signals = {
        "name": name_sim,
        "type": type_score,
        "detail": detail_score,
        "range": range_score,
    }

    number_match = (
        source.account_number
        and target.account_number
        and source.account_number.strip() == target.account_number.strip()
    )

    if number_match and name_sim >= 0.5:
        reasons.append(
            f"Account number matches exactly ({source.account_number} → "
            f"{target.account_number}) and the names agree."
        )
        return 1.0, reasons, signals

    if number_match:
        # The number carried over but the names diverge, which usually means the
        # chart was renumbered. Worth surfacing rather than trusting blindly.
        reasons.append(
            f"Account numbers match ({source.account_number}) but the names "
            f"differ, so the number may have been reused for a different account."
        )
        score = 0.85
        return score, reasons, signals

    score = (
        0.50 * name_sim
        + 0.25 * type_score
        + 0.15 * detail_score
        + 0.10 * range_score
    )

    if normalize_name(source.account_name) == normalize_name(target.account_name):
        reasons.append(
            f"Names match after normalisation ('{source.account_name}' → "
            f"'{target.account_name}')."
        )
    else:
        shared = name_tokens(source.account_name) & name_tokens(target.account_name)
        if shared:
            reasons.append(
                "Shared name terms: " + ", ".join(sorted(shared)) + "."
            )

    if src_family and tgt_family:
        if src_family == tgt_family:
            reasons.append(
                f"Account types align ({source.type or 'untyped'} → "
                f"{target.type or 'untyped'})."
            )
        else:
            reasons.append(
                f"Account types disagree ({source.type or 'untyped'} → "
                f"{target.type or 'untyped'}), which caps confidence."
            )

    if range_score == 1.0 and not number_match:
        reasons.append(
            f"Both accounts fall in the same number block "
            f"({source.account_number} → {target.account_number})."
        )

    if detail_score == 1.0 and source.detail_type:
        reasons.append(f"Detail type matches ('{source.detail_type}').")

    return round(score, 4), reasons, signals


def match_accounts(
    source_accounts: list[Account],
    target_accounts: list[Account],
    confirmed: dict[str, str] | None = None,
) -> list[AccountMapping]:
    """Map every source account to its best target account.

    `confirmed` carries human decisions from a previous run, keyed by source
    account number, and takes precedence over any automatic match.
    """
    confirmed = confirmed or {}
    mappings: list[AccountMapping] = []
    target_by_number = {a.account_number: a for a in target_accounts}

    for source in source_accounts:
        # A human decision wins outright.
        override_number = confirmed.get(source.account_number)
        if override_number and override_number in target_by_number:
            target = target_by_number[override_number]
            mappings.append(
                AccountMapping(
                    source_number=source.account_number,
                    source_name=source.account_name,
                    source_type=source.type,
                    source_detail_type=source.detail_type,
                    target_number=target.account_number,
                    target_name=target.account_name,
                    target_type=target.type,
                    confidence=1.0,
                    method=MatchMethod.EXACT_NAME,
                    status=MappingStatus.CONFIRMED,
                    reasons=["Mapping confirmed by the user."],
                    candidates=[],
                )
            )
            continue

        scored: list[tuple[float, Account, list[str], dict[str, float]]] = []
        for target in target_accounts:
            score, reasons, signals = score_pair(source, target)
            scored.append((score, target, reasons, signals))

        # Rank by score, then by account number so ties are stable and the same
        # input always yields the same output.
        scored.sort(key=lambda item: (-item[0], item[1].account_number))

        candidates = [
            Candidate(
                target_number=t.account_number,
                target_name=t.account_name,
                target_type=t.type,
                score=s,
            )
            for s, t, _, _ in scored[:3]
            if s > 0.01
        ]

        if not scored:
            mappings.append(
                AccountMapping(
                    source_number=source.account_number,
                    source_name=source.account_name,
                    source_type=source.type,
                    source_detail_type=source.detail_type,
                    status=MappingStatus.NEEDS_REVIEW,
                    reasons=["The target chart of accounts is empty."],
                )
            )
            continue

        best_score, best_target, best_reasons, _ = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0

        reasons = list(best_reasons)
        ambiguous = (
            len(scored) > 1
            and best_score >= REVIEW_THRESHOLD
            and (best_score - runner_up) < AMBIGUITY_MARGIN
        )
        if ambiguous:
            reasons.append(
                f"Ambiguous: the runner-up ({scored[1][1].account_name}, "
                f"{runner_up:.2f}) scores within {AMBIGUITY_MARGIN:.2f} of this "
                f"match ({best_score:.2f}). Confirm which is correct."
            )

        # Decide status.
        if best_score >= AUTO_MAP_THRESHOLD and not ambiguous:
            status = MappingStatus.AUTO
        else:
            status = MappingStatus.NEEDS_REVIEW

        if best_score < REVIEW_THRESHOLD:
            reasons.append(
                f"Best candidate scored only {best_score:.2f}; no confident "
                f"match exists in the target chart."
            )

        method = MatchMethod.UNMAPPED
        if best_score >= REVIEW_THRESHOLD:
            method = MatchMethod.FUZZY
            if normalize_name(source.account_name) == normalize_name(best_target.account_name):
                method = MatchMethod.EXACT_NAME
            elif (
                source.account_number
                and source.account_number.strip() == best_target.account_number.strip()
            ):
                method = MatchMethod.EXACT_NUMBER

        mapped = best_score >= REVIEW_THRESHOLD
        mappings.append(
            AccountMapping(
                source_number=source.account_number,
                source_name=source.account_name,
                source_type=source.type,
                source_detail_type=source.detail_type,
                target_number=best_target.account_number if mapped else None,
                target_name=best_target.account_name if mapped else None,
                target_type=best_target.type if mapped else None,
                confidence=best_score,
                method=method if mapped else MatchMethod.UNMAPPED,
                status=status,
                reasons=reasons,
                candidates=candidates,
            )
        )

    return mappings


def explain(mapping: AccountMapping) -> str:
    """Render a mapping as a sentence for voice playback or display."""
    if not mapping.target_number:
        return (
            f"{mapping.source_name} could not be mapped confidently. "
            + " ".join(mapping.reasons)
        )
    parts = [
        f"{mapping.source_name} ({mapping.source_number}) maps to "
        f"{mapping.target_name} ({mapping.target_number}) "
        f"with {mapping.confidence:.0%} confidence."
    ]
    parts.extend(mapping.reasons)
    return " ".join(parts)
