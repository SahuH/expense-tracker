"""
Pushes transactions directly to the Firefly III core API (/api/v1/transactions).
Bypasses the Data Importer — simpler, and automatically applies keyword rules.
"""
import os
import logging
import requests

logger = logging.getLogger(__name__)

FIREFLY_URL = os.getenv("FIREFLY_URL", "http://firefly:8080")
FIREFLY_ACCESS_TOKEN = os.getenv("FIREFLY_ACCESS_TOKEN", "")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {FIREFLY_ACCESS_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _build_payload(txn: dict) -> dict:
    txn_type = txn.get("type", "debit")
    amount = str(round(float(txn.get("amount", 0)), 2))
    account_name = txn.get("account_name", "")
    date = txn.get("date", "")
    # Firefly expects YYYY-MM-DD; handle common variants
    if date and len(date) > 10:
        date = date[:10]

    if txn_type == "debit":
        entry = {
            "type": "withdrawal",
            "date": date,
            "amount": amount,
            "description": txn.get("description", ""),
            "source_name": account_name,
            "currency_code": txn.get("currency", "AED"),
        }
    else:
        entry = {
            "type": "deposit",
            "date": date,
            "amount": amount,
            "description": txn.get("description", ""),
            "destination_name": account_name,
            "currency_code": txn.get("currency", "AED"),
        }

    if txn.get("category"):
        entry["category_name"] = txn["category"]

    return {
        "apply_rules": True,
        "fire_webhooks": False,
        "transactions": [entry],
    }


def push_to_firefly(transactions: list[dict]) -> dict:
    imported = 0
    errors = []

    for txn in transactions:
        try:
            payload = _build_payload(txn)
            resp = requests.post(
                f"{FIREFLY_URL}/api/v1/transactions",
                headers=_headers(),
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            imported += 1
        except requests.RequestException as exc:
            msg = str(exc)
            logger.warning("Failed to import transaction %s: %s", txn.get("description"), msg)
            errors.append(msg)

    return {
        "success": len(errors) == 0,
        "imported_count": imported,
        "error_count": len(errors),
        "errors": errors[:5],
    }
