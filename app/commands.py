"""Turn a spoken transcript into a structured command.

This is deliberately deterministic rather than an LLM call: the vocabulary of a
migration review is small and closed, so regexes over a normalised transcript
are predictable, free, and debuggable. The transcript is normalised with the
same rules as account names, so "confirm 1,000" and "approve one thousand"
resolve the same way.
"""

from __future__ import annotations

import re

from .models import AccountMapping

# Order matters: more specific intents are matched first.
# Each rule is (intent, regex). `intent` is the canonical action name.
_INTENTS: list[tuple[str, str]] = [
    ("download_mappings", r"\b(download|export)\b.*\bmapping"),
    ("download_migrated", r"\b(download|export)\b.*\b(migrat|transact|journal|file)"),
    ("migrate", r"\bmigrat"),
    ("approve_top", r"\b(accept|approve|confirm|use)\b.*\b(top|best|first|suggested)"),
    ("map", r"\b(map|match|assign|put|set)\b.*\b(to|as|onto)\b"),
    ("approve", r"\b(approve|accept|confirm|yes|good|keep)\b"),
    ("clear", r"\b(reset|clear|undo|discard)\b.*\b(decision|mapping|approval|all)\b"),
    ("validate", r"\b(validate|verify|check|show)\b.*\b(report|result|valid)"),
    ("explain", r"\b(explain|why|reason)\b"),
]

# Words that make transcripts noisy but carry no intent.
_FILLER = re.compile(
    r"\b(please|hey|um|uh|ok|okay|the|a|an|can you|could you|would you|i want to|"
    r"go ahead and|for me|now)\b"
)


def _normalize(text: str) -> str:
    text = (text or "").lower()
    # "one thousand" -> "1000" so spoken numbers resolve to account numbers.
    text = _numbers_to_digits(text)
    return _FILLER.sub(" ", text)


_WORD_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100, "thousand": 1000,
}


def _numbers_to_digits(text: str) -> str:
    """Replace number words in sequence with digits.

    "one thousand" -> "1000", "six hundred" -> "600". Handles only simple
    sequences; anything else is left untouched.
    """
    tokens = text.split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        word = tokens[i].strip(".,")
        if word in _WORD_NUMBERS:
            total = _WORD_NUMBERS[word]
            # Multiplicative grouping: "one thousand" = 1000.
            j = i + 1
            while j < len(tokens) and tokens[j].strip(".,") in _WORD_NUMBERS:
                next_word = tokens[j].strip(".,")
                next_val = _WORD_NUMBERS[next_word]
                if next_val >= 100 and total < 100:
                    total *= next_val
                else:
                    total += next_val
                j += 1
            out.append(str(total))
            i = j
        else:
            out.append(tokens[i])
            i += 1
    return " ".join(out)


def _find_account(value: str, mappings: list[AccountMapping]) -> str | None:
    """Resolve a spoken account reference to a source account number.

    Matches by exact source number, then by significant name tokens. Returns the
    source account number, or None if nothing matches unambiguously.
    """
    if not value:
        return None
    stripped = value.strip()

    # Exact number match first.
    for mapping in mappings:
        if mapping.source_number == stripped:
            return mapping.source_number

    # Name-token match: every significant word in the spoken reference must
    # appear in the account name, or vice versa for very short references.
    from .matching import name_tokens

    spoken = name_tokens(stripped)
    if not spoken:
        return None

    matches = [
        m for m in mappings
        if spoken.issubset(name_tokens(m.source_name))
        or name_tokens(m.source_name).issubset(spoken)
    ]
    if len(matches) == 1:
        return matches[0].source_number
    return None


def _extract_reference(
    normalized: str, spoken_number: str | None, mappings: list[AccountMapping]
) -> str | None:
    """Find the account a command refers to.

    Tries, in order: a spoken number, then a name phrase following the verb.
    Returns the source account number if resolvable, otherwise the raw number
    (for explicit digits) or None (for names that don't unambiguously match).
    """
    if spoken_number:
        return _find_account(spoken_number, mappings) or spoken_number

    # "approve sales commissions" / "explain operating checking" — take the
    # words after the leading verb and resolve them as an account name.
    for verb in ("approve", "accept", "confirm", "explain", "map", "why"):
        marker = re.search(rf"\b{verb}\b\s*(.+)", normalized)
        if marker:
            phrase = marker.group(1).strip()
            # Stop at a trailing "to X" clause for "map X to Y".
            phrase = re.split(r"\s+(?:to|as|onto)\b", phrase)[0].strip()
            if phrase:
                resolved = _find_account(phrase, mappings)
                if resolved:
                    return resolved
            break
    return None


def parse_command(text: str, mappings: list[AccountMapping]) -> dict[str, object]:
    """Parse a transcript into a command dict.

    Returns {"intent": str, "source_number": str|None, "target_number": str|None,
    "ok": bool, "message": str}. `ok` is False when the transcript could not be
    resolved, in which case `message` explains what to say instead.
    """
    if not text or not text.strip():
        return {"intent": "unknown", "ok": False,
                "message": "I didn't hear anything. Try again."}

    normalized = _normalize(text)

    # Extract a source account number if one was spoken explicitly.
    number_match = re.search(r"\b(\d{2,6})\b", normalized)
    spoken_number = number_match.group(1) if number_match else None

    intent = "unknown"
    for candidate_intent, pattern in _INTENTS:
        if re.search(pattern, normalized):
            intent = candidate_intent
            break

    # The account referenced — either a spoken number, or the name that follows
    # the command verb ("approve sales commissions").
    referenced = _extract_reference(normalized, spoken_number, mappings)

    result: dict[str, object] = {
        "intent": intent,
        "source_number": None,
        "target_number": None,
        "ok": True,
        "message": "",
    }

    if intent == "approve_top":
        # "approve the top match" / "accept the best choice" — applies to the
        # first mapping still awaiting review, or the account just spoken.
        if referenced:
            result["source_number"] = referenced
        result["message"] = "Approving the suggested match."

    elif intent == "approve":
        # "approve 1000" — confirm that source account's current suggestion.
        if referenced:
            result["source_number"] = referenced
        result["message"] = "Approving that mapping."

    elif intent == "map":
        # "map 1000 to 10000" — an explicit account number pair.
        pair = re.search(r"\b(\d{2,6})\b\s*(?:to|as|onto)\s*(\d{2,6})\b", normalized)
        if pair:
            result["source_number"] = pair.group(1)
            result["target_number"] = pair.group(2)
            result["message"] = "Mapping those accounts."
        else:
            # Try "map <name> to <number>".
            name_to_num = re.search(r"\bmap\s+(.+?)\s+(?:to|as|onto)\s+(\d{2,6})\b", normalized)
            if name_to_num:
                source = _find_account(name_to_num.group(1), mappings)
                if source:
                    result["source_number"] = source
                    result["target_number"] = name_to_num.group(2)
                    result["message"] = "Mapping those accounts."
                else:
                    result["ok"] = False
                    result["message"] = (
                        "I couldn't find that source account. Say the account "
                        "number instead, like 'map 1000 to 10000'."
                    )
            else:
                result["ok"] = False
                result["message"] = (
                    "Say it like 'map 1000 to 10000', or pick the target in the "
                    "dropdown."
                )

    elif intent == "migrate":
        result["message"] = "Running the migration."

    elif intent == "download_mappings":
        result["message"] = "Downloading the mappings file."

    elif intent == "download_migrated":
        result["message"] = "Downloading the migrated transactions."

    elif intent == "clear":
        result["message"] = "Clearing your review decisions."

    elif intent == "validate":
        result["message"] = "Showing the validation report."

    elif intent == "explain":
        if referenced:
            result["source_number"] = referenced
            result["intent"] = "explain"
            result["message"] = "Explaining that mapping."
        else:
            result["intent"] = "explain_report"
            result["message"] = "Explaining the validation report."

    else:
        result["ok"] = False
        result["message"] = (
            "I didn't understand. You can say: 'approve the top match', "
            "'map 1000 to 10000', 'run the migration', 'download the migrated "
            "file', or 'show the validation report'."
        )

    return result
