"""
Pushes transactions directly to the Firefly III core API (/api/v1/transactions).
Bypasses the Data Importer — simpler, and automatically applies keyword rules.

Transfer detection
──────────────────
Before importing, the client fetches the full asset-account list from Firefly.
If a transaction's description contains the name of another asset account
(e.g. "Fixed Saving Space" matches the "Wio Fixed Saving Space" account),
it is imported as type "transfer" between the two asset accounts instead of
as a withdrawal/deposit.  This prevents Firefly from auto-creating expense or
revenue account shadows and keeps the transfer out of income/expense reports.
"""
import os
import json
import logging
import requests

logger = logging.getLogger(__name__)

FIREFLY_URL = os.getenv("FIREFLY_URL", "http://firefly:8080")
FIREFLY_ACCESS_TOKEN = os.getenv("FIREFLY_ACCESS_TOKEN", "")

# Bank-name prefixes that are stripped when matching account names against
# transaction descriptions (e.g. "Wio Fixed Saving Space" → "Fixed Saving Space").
_BANK_PREFIXES = (
    "wio ", "fab ", "first abu dhabi bank ", "adcb ", "enbd ",
    "mashreq ", "hsbc ", "citibank ", "rakbank ",
)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {FIREFLY_ACCESS_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


# ─── Asset-account helpers ────────────────────────────────────────────────────

def _get_asset_accounts() -> list[dict]:
    """Return all asset accounts from Firefly (empty list on failure)."""
    try:
        resp = requests.get(
            f"{FIREFLY_URL}/api/v1/accounts?type=asset&limit=100",
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception as exc:
        logger.warning("Could not fetch asset accounts: %s", exc)
        return []


def _check_transfer_rules(
    description: str, account_name: str, transfer_rules: dict
) -> str | None:
    """Return the other-account name if a user-defined keyword rule matches."""
    desc_lower = description.lower()
    for rule in transfer_rules.get(account_name, []):
        if rule.get("keyword", "").lower() in desc_lower:
            return rule["other_account"]
    return None


def _find_transfer_account(
    description: str, current_account: str, asset_accounts: list
) -> str | None:
    """
    Return the name of another asset account if the description references it.
    Tries the full account name first, then the name with leading bank prefixes
    removed (e.g. "Wio Fixed Saving Space" → "Fixed Saving Space").
    Returns None if no match is found.
    """
    desc_lower = description.lower()
    current_lower = current_account.lower()

    for acct in asset_accounts:
        name: str = acct.get("attributes", {}).get("name", "")
        name_lower = name.lower()

        if name_lower == current_lower:
            continue  # skip the account we're importing into

        # Full name match
        if name_lower in desc_lower:
            return name

        # Match after stripping a leading bank prefix
        for prefix in _BANK_PREFIXES:
            if name_lower.startswith(prefix):
                stripped = name_lower[len(prefix):]
                if len(stripped) > 3 and stripped in desc_lower:
                    return name
                break

    return None


# ─── Payload builder ──────────────────────────────────────────────────────────

def _build_payload(
    txn: dict, asset_accounts: list | None = None, transfer_rules: dict | None = None
) -> dict:
    import math
    txn_type = txn.get("type", "debit")
    amount = str(round(float(txn.get("amount", 0)), 2))
    raw_account = txn.get("account_name")
    if raw_account is None or (isinstance(raw_account, float) and math.isnan(raw_account)):
        raw_account = ""
    account_name = str(raw_account).strip()
    description = txn.get("description", "")
    date = txn.get("date", "")
    if date and len(date) > 10:
        date = date[:10]
    currency = txn.get("currency", "AED")

    # ── Transfer detection ────────────────────────────────────────────────────
    # Keyword rules take priority (explicit user config), then account-name detection.
    transfer_account = None
    if transfer_rules and description:
        transfer_account = _check_transfer_rules(description, account_name, transfer_rules)
    if not transfer_account and asset_accounts and description:
        transfer_account = _find_transfer_account(description, account_name, asset_accounts)

    if transfer_account:
        # debit  = money leaves current account → goes to transfer_account
        # credit = money arrives in current account ← comes from transfer_account
        if txn_type in ("debit", "withdrawal"):
            source_name, destination_name = account_name, transfer_account
        else:
            source_name, destination_name = transfer_account, account_name

        logger.info(
            "Detected transfer: %s → %s (%s)",
            source_name, destination_name, description[:60],
        )
        entry = {
            "type": "transfer",
            "date": date,
            "amount": amount,
            "description": description,
            "source_name": source_name,
            "destination_name": destination_name,
            "currency_code": currency,
        }

    elif txn_type in ("debit", "withdrawal"):
        entry = {
            "type": "withdrawal",
            "date": date,
            "amount": amount,
            "description": description,
            "source_name": account_name,
            "currency_code": currency,
        }
    else:
        entry = {
            "type": "deposit",
            "date": date,
            "amount": amount,
            "description": description,
            "destination_name": account_name,
            "currency_code": currency,
        }

    if txn.get("category"):
        entry["category_name"] = txn["category"]

    return {
        "apply_rules": True,
        "fire_webhooks": False,
        "transactions": [entry],
    }


# ─── Main entry point ─────────────────────────────────────────────────────────

def _load_transfer_rules_from_disk() -> dict:
    """Load user-defined keyword transfer rules (written by the parser API)."""
    import os
    from pathlib import Path
    rules_path = Path(os.getenv("TRANSFER_RULES_FILE", "/data/transfer_rules.json"))
    try:
        return json.loads(rules_path.read_text())
    except Exception:
        return {}


def push_to_firefly(transactions: list[dict]) -> dict:
    # Fetch asset accounts once so transfer detection works for the whole batch
    asset_accounts = _get_asset_accounts()
    transfer_rules = _load_transfer_rules_from_disk()
    logger.info(
        "Loaded %d asset accounts and transfer rules for %d accounts",
        len(asset_accounts), len(transfer_rules),
    )

    imported = 0
    errors = []

    for txn in transactions:
        try:
            payload = _build_payload(txn, asset_accounts, transfer_rules)
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
            if hasattr(exc, "response") and exc.response is not None:
                msg += f" | body: {exc.response.text[:300]}"
            logger.warning("Failed txn payload: %s", payload)
            logger.warning("Failed to import transaction %s: %s", txn.get("description"), msg)
            errors.append(msg)

    return {
        "success": len(errors) == 0,
        "imported_count": imported,
        "error_count": len(errors),
        "errors": errors[:5],
    }
