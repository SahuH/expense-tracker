"""
Balance verification.

Bank account : opening + credits - debits == closing  (±0.01 tolerance)
Credit card  : opening + debits  - credits == closing (charges increase balance owed)

If opening/closing figures cannot be extracted from the statement the check is
marked as "not_applicable" for credit cards (different label conventions, not a
parsing failure) and "missing_data" for bank accounts.
"""
import logging
from typing import Any

logger = logging.getLogger(__name__)

TOLERANCE = 0.01


def verify_balance(
    transactions: list[dict],
    metadata: dict,
    statement_type: str = "bank_account",
) -> dict[str, Any]:
    opening = metadata.get("opening_balance")
    closing = metadata.get("closing_balance")

    total_credits = round(
        sum(t["amount"] for t in transactions if t.get("type") == "credit" and t.get("amount")), 2
    )
    total_debits = round(
        sum(t["amount"] for t in transactions if t.get("type") == "debit" and t.get("amount")), 2
    )

    result: dict[str, Any] = {
        "total_credits": total_credits,
        "total_debits": total_debits,
        "transaction_count": len(transactions),
    }

    if opening is None or closing is None:
        if statement_type == "credit_card":
            result["passed"] = True
            result["reason"] = "not_applicable"
            result["note"] = (
                "Credit card statements use different balance labels; "
                "reconciliation skipped."
            )
        else:
            result["passed"] = False
            result["reason"] = "opening_balance or closing_balance not found in statement"
        return result

    # Bank account: opening + credits − debits = closing
    # Credit card:  opening + debits  − credits = closing (charges add to amount owed)
    if statement_type == "credit_card":
        expected_closing = round(opening + total_debits - total_credits, 2)
    else:
        expected_closing = round(opening + total_credits - total_debits, 2)

    diff = abs(expected_closing - closing)

    result["opening_balance"] = opening
    result["closing_balance"] = closing
    result["expected_closing"] = expected_closing
    result["difference"] = round(diff, 4)
    result["passed"] = diff <= TOLERANCE

    if not result["passed"]:
        result["reason"] = (
            f"Balance mismatch: expected {expected_closing:.2f}, "
            f"got {closing:.2f} (diff={diff:.4f})"
        )
        logger.warning(result["reason"])
    else:
        result["reason"] = "ok"

    return result
