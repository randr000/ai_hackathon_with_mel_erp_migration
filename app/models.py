"""Domain models shared across the migration pipeline.

Everything the API returns is one of these, so the frontend always knows the
shape of a mapping, a candidate and a validation check.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Account(BaseModel):
    """One row of a chart of accounts."""

    account_number: str
    account_name: str
    type: str = ""
    detail_type: str = ""


class TransactionLine(BaseModel):
    """One side of a double-entry journal line.

    `amount` keeps its sign: debits are positive, credits negative. A balanced
    transaction therefore sums to zero across its lines.
    """

    transaction_id: str
    date: str
    account_number: str
    account_name: str
    amount: float
    cls: str = ""
    location: str = ""
    memo: str = ""


class MatchMethod(str, Enum):
    EXACT_NUMBER = "exact_number"
    EXACT_NAME = "exact_name"
    FUZZY = "fuzzy"
    UNMAPPED = "unmapped"


class MappingStatus(str, Enum):
    AUTO = "auto"
    NEEDS_REVIEW = "needs_review"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class Candidate(BaseModel):
    """A target account the matcher considered, with its score."""

    target_number: str
    target_name: str
    target_type: str
    score: float


class AccountMapping(BaseModel):
    source_number: str
    source_name: str
    source_type: str = ""
    source_detail_type: str = ""
    target_number: str | None = None
    target_name: str | None = None
    target_type: str | None = None
    confidence: float = 0.0
    method: MatchMethod = MatchMethod.UNMAPPED
    status: MappingStatus = MappingStatus.NEEDS_REVIEW
    # Human-readable justification, built from the scoring signals that fired.
    reasons: list[str] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)


class Check(BaseModel):
    """A single validation assertion with its outcome."""

    name: str
    passed: bool
    detail: str
    severity: str = "error"  # "error" fails the migration; "warning" is advisory


class ValidationReport(BaseModel):
    passed: bool
    checks: list[Check] = Field(default_factory=list)
    totals: dict[str, float] = Field(default_factory=dict)


class MigratedLine(BaseModel):
    transaction_id: str
    date: str
    source_account: str
    source_account_name: str
    target_account: str | None
    target_account_name: str | None
    amount: float
    cls: str = ""
    location: str = ""
    memo: str = ""
