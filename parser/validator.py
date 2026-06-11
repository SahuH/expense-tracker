"""
Balance verification: opening + credits - debits == closing (±0.01 tolerance).
"""
import logging
from typing import Any

logger = logging.getLogger(__name__)

TOLERANCE = 0.01


def verify_balance(transactions: list[dict], metadata: dict) -> dict[str, Any]:
    opening = metadata.get("opening_balance")
    closing = metadata.get("closing_balance")

    total_credits = sum(
        t["amount"] for t in transactions if t.get("type") == "credit" and t.get("amount")
    )
    total_debits = sum(
        t["amount"] for t in transactions if t.get("type") == "debit" and t.get("amount")
    )

    result: dict[str, Any] = {
        "total_credits": round(total_credits, 2),
        "total_debits": round(total_debits, 2),
        "transaction_count": len(transactions),
    }

    if opening is None or closing is None:
        result["passed"] = False
        result["reason"] = "opening_balance or closing_balance not found in statement"
        return result

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
