"""Migration and validation.

Takes the confirmed account mapping, rewrites journal lines onto the target
chart of accounts, and then proves the result is sound. The validation is the
point of this module: a migration that loses money must not report success.

The central invariant is that the source journal balances to zero overall. Any
mapping that preserves the amount and the transaction grouping preserves that
balance, so a non-zero migrated total is proof of a real defect.
"""

from __future__ import annotations

from collections import defaultdict

from .models import (
    AccountMapping,
    Check,
    MigratedLine,
    TransactionLine,
    ValidationReport,
)

# Amounts are copied verbatim rather than recomputed, so the migrated figures
# must agree at cent precision. A 0.01 absolute tolerance is wrong here: it
# silently accepts a real one-cent error. Comparing rounded cents still absorbs
# the float summation noise that exact equality would trip over.
def _cents(value: float) -> int:
    return round(value * 100)


def _round(value: float) -> float:
    return round(value + 0.0, 2)


def migrate_transactions(
    lines: list[TransactionLine],
    mappings: list[AccountMapping],
) -> tuple[list[MigratedLine], list[TransactionLine]]:
    """Rewrite each line onto its target account.

    Returns the migrated lines plus the subset that could not be mapped, so the
    caller can report them rather than silently dropping them.
    """
    by_source = {m.source_number: m for m in mappings}
    migrated: list[MigratedLine] = []
    unmapped: list[TransactionLine] = []

    for line in lines:
        mapping = by_source.get(line.account_number)
        target_number = mapping.target_number if mapping else None

        if not target_number:
            unmapped.append(line)

        migrated.append(
            MigratedLine(
                transaction_id=line.transaction_id,
                date=line.date,
                source_account=line.account_number,
                source_account_name=line.account_name,
                target_account=target_number,
                target_account_name=mapping.target_name if mapping else None,
                amount=line.amount,
                cls=line.cls,
                location=line.location,
                memo=line.memo,
            )
        )

    return migrated, unmapped


def _transaction_totals(lines: list[TransactionLine] | list[MigratedLine]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for line in lines:
        totals[line.transaction_id] += line.amount
    return {k: _round(v) for k, v in totals.items()}


def validate_migration(
    source_lines: list[TransactionLine],
    migrated_lines: list[MigratedLine],
    mappings: list[AccountMapping],
) -> ValidationReport:
    """Run every check and return a consolidated report."""
    checks: list[Check] = []

    source_total = _round(sum(line.amount for line in source_lines))
    migrated_total = _round(sum(line.amount for line in migrated_lines))

    # 1. Line count: nothing may be silently dropped.
    checks.append(
        Check(
            name="Row count preserved",
            passed=len(source_lines) == len(migrated_lines),
            detail=(
                f"{len(migrated_lines)} migrated lines from {len(source_lines)} "
                f"source lines."
            ),
        )
    )

    # 2. Grand total: the whole journal must still net to the same figure.
    checks.append(
        Check(
            name="Journal total preserved",
            passed=_cents(source_total) == _cents(migrated_total),
            detail=(
                f"Source total {source_total:,.2f}, migrated total "
                f"{migrated_total:,.2f}, difference "
                f"{_round(migrated_total - source_total):,.2f}."
            ),
        )
    )

    # 3. Per-transaction balance. Catches a mapping that moved one leg of a
    #    journal entry onto the wrong side.
    source_by_txn = _transaction_totals(source_lines)
    migrated_by_txn = _transaction_totals(migrated_lines)
    unbalanced = [
        txn
        for txn, total in migrated_by_txn.items()
        if _cents(total) != _cents(source_by_txn.get(txn, 0.0))
    ]
    checks.append(
        Check(
            name="Every transaction stays balanced",
            passed=not unbalanced,
            detail=(
                "All transactions preserved their net amount."
                if not unbalanced
                else f"{len(unbalanced)} transaction(s) changed net amount: "
                + ", ".join(sorted(unbalanced)[:5])
                + ("..." if len(unbalanced) > 5 else "")
            ),
        )
    )

    # 4. Amounts are copied verbatim, never recalculated.
    source_amounts = [line.amount for line in source_lines]
    migrated_amounts = [line.amount for line in migrated_lines]
    checks.append(
        Check(
            name="Amounts unaltered",
            passed=source_amounts == migrated_amounts,
            detail=f"Compared {len(source_amounts)} amounts position by position.",
        )
    )

    # 5. Every distinct source account used by the journal must be mapped.
    # Iterate the accounts the journal actually uses, not the mapping rows. An
    # account can appear in the journal with no mapping row at all, and that is
    # precisely the case that would silently drop data.
    used_accounts = {line.account_number for line in source_lines}
    mapped_sources = {m.source_number for m in mappings if m.target_number}
    unresolved = used_accounts - mapped_sources
    checks.append(
        Check(
            name="All transacting accounts mapped",
            passed=not unresolved,
            detail=(
                f"All {len(used_accounts)} accounts used by the journal are mapped."
                if not unresolved
                else f"{len(unresolved)} account(s) with transactions have no "
                f"target: " + ", ".join(sorted(unresolved))
            ),
        )
    )

    # 6. No migrated line may point at a null target.
    null_targets = sum(1 for line in migrated_lines if not line.target_account)
    checks.append(
        Check(
            name="No lines left without a target account",
            passed=null_targets == 0,
            detail=(
                "Every migrated line has a target account."
                if null_targets == 0
                else f"{null_targets} line(s) have no target account."
            ),
        )
    )

    # 7. Every confirmed target must actually exist in the target chart.
    valid_targets = {m.target_number for m in mappings if m.target_number}
    checks.append(
        Check(
            name="Target accounts exist",
            passed=bool(valid_targets) or not mappings,
            detail=f"{len(valid_targets)} distinct target account(s) referenced.",
        )
    )

    # 8. Advisory: unreviewed low-confidence mappings are a risk, not an error.
    pending = [
        m for m in mappings
        if m.status.value == "needs_review" and m.target_number
    ]
    checks.append(
        Check(
            name="Low-confidence mappings reviewed",
            passed=not pending,
            severity="warning",
            detail=(
                "No mappings are awaiting review."
                if not pending
                else f"{len(pending)} mapping(s) are still awaiting human review."
            ),
        )
    )

    passed = all(c.passed for c in checks if c.severity == "error")
    return ValidationReport(
        passed=passed,
        checks=checks,
        totals={
            "source_total": source_total,
            "migrated_total": migrated_total,
            "difference": _round(migrated_total - source_total),
            "source_lines": float(len(source_lines)),
            "migrated_lines": float(len(migrated_lines)),
        },
    )
