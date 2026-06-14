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

# Built-in transfer rules derived from known account patterns in this household.
# Each rule has:
#   keyword     – substring (or exact string if match="exact") to find in description
#   match       – "contains" (default) or "exact"
#   action      – "skip": drop this transaction (it's the destination side of a transfer
#                          that will be imported from the source account's statement)
#               – omitted / "transfer": import as a Firefly transfer to other_account
#   other_account – Firefly asset-account name for the counterpart (required for transfer)
#
# User-defined rules (stored in /data/transfer_rules.json) are checked first and can
# override these defaults by matching the same keyword earlier in the list.
_DEFAULT_TRANSFER_RULES: dict = {
    # ── Wio bank account ──────────────────────────────────────────────────────
    "Wio bank account": [
        # Wio → Fixed Saving Space deposit (debit/source side)
        {"keyword": "to Fixed Saving Space", "other_account": "Wio Fixed Saving Space"},
        # FSS → Wio withdrawal arriving (credit/destination; FSS debit imports it)
        {"keyword": "Fixed Saving Space to Harsh", "action": "skip"},
        # Wio → FAB self-transfer (debit/source side)
        {"keyword": "To Harsh Sahu Shiv Kumar Sahu", "other_account": "First Abu Dhabi Bank (FAB)  bank account"},
        # FAB → Wio self-transfer arriving (credit/destination; FAB IPP debit imports it)
        {"keyword": "From Harsh Sahu Shiv Kumar Sahu", "action": "skip"},
    ],
    # ── Wio Fixed Saving Space ────────────────────────────────────────────────
    "Wio Fixed Saving Space": [
        # FSS → Wio withdrawal (debit/source side)
        {"keyword": "Fixed Saving Space to Harsh", "other_account": "Wio bank account"},
        # Wio → FSS deposit arriving (credit/destination; Wio bank debit imports it)
        {"keyword": "to Fixed Saving Space", "action": "skip"},
    ],
    # ── First Abu Dhabi Bank (FAB) bank account ───────────────────────────────
    "First Abu Dhabi Bank (FAB)  bank account": [
        # FAB → Wio via IPP/IPI (Wio IBAN AE160860000006198732111 appears in description)
        {"keyword": "AE160860000006198732111", "other_account": "Wio bank account"},
        # CC bill payment — description is exactly "Transfer" on FAB bank side
        {"keyword": "Transfer", "match": "exact", "other_account": "FAB Credit Card"},
        # Wio → FAB arriving (WIOBAEAD = Wio Bank SWIFT; credit/destination side)
        {"keyword": "WIOBAEAD", "action": "skip"},
    ],
    # ── FAB Credit Card ───────────────────────────────────────────────────────
    "FAB Credit Card": [
        # CC payment arriving (credit/destination; FAB bank "Transfer" debit imports it)
        {"keyword": "PAYMENT RECEIVED", "action": "skip"},
    ],
}


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
) -> tuple[str | None, str | None]:
    """
    Return (action, other_account) for the first matching rule.
    action is 'skip', 'transfer', or None (no match).
    Supports match="exact" for rules that must match the full description.
    """
    desc_lower = description.lower()
    for rule in transfer_rules.get(account_name, []):
        kw = rule.get("keyword", "").lower()
        if not kw:
            continue
        if rule.get("match") == "exact":
            matched = desc_lower == kw
        else:
            matched = kw in desc_lower
        if matched:
            if rule.get("action") == "skip":
                return "skip", None
            if rule.get("other_account"):
                return "transfer", rule["other_account"]
    return None, None


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
) -> dict | None:
    """Build a Firefly transaction payload. Returns None if the transaction should be skipped."""
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
    # Keyword rules take priority (user-defined + defaults), then account-name detection.
    transfer_account = None
    if transfer_rules and description:
        action, transfer_account = _check_transfer_rules(description, account_name, transfer_rules)
        if action == "skip":
            return None  # destination side of a transfer; source account's statement imports it
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

def get_effective_transfer_rules() -> dict:
    """
    Return all active rules (user-defined + built-in defaults) with an _origin tag.
    Used by the API to drive the Rules UI page.
    """
    from pathlib import Path
    rules_path = Path(os.getenv("TRANSFER_RULES_FILE", "/data/transfer_rules.json"))
    try:
        user_rules: dict = json.loads(rules_path.read_text())
    except Exception:
        user_rules = {}

    result: dict = {}
    for account in sorted(set(list(user_rules.keys()) + list(_DEFAULT_TRANSFER_RULES.keys()))):
        user_list = [{"_origin": "user", **r} for r in user_rules.get(account, [])]
        builtin_list = [{"_origin": "builtin", **r} for r in _DEFAULT_TRANSFER_RULES.get(account, [])]
        result[account] = user_list + builtin_list
    return result


def apply_rules_to_existing_transactions() -> dict:
    """
    Retroactively apply transfer rules to all existing withdrawal/deposit transactions
    in Firefly III.

    Pass 1 (withdrawals): source-side transactions matching a transfer rule are updated
                          to type=transfer.  Skip-action withdrawals are deleted.
    Pass 2 (deposits):    destination-side deposits matching a skip rule are deleted.
                          Deposit-side transfers (rare) are also updated.

    Updates run before deletes so source-side transfers exist before destinations are removed.
    """
    asset_accounts = _get_asset_accounts()
    transfer_rules = _load_transfer_rules_from_disk()

    to_update: list[tuple] = []   # (txn_id, description, date, amount, currency, src, dst)
    to_delete: list[str]  = []

    def _collect(txn_type: str) -> None:
        page = 1
        while True:
            resp = requests.get(
                f"{FIREFLY_URL}/api/v1/transactions",
                headers=_headers(),
                params={"type": txn_type, "limit": 100, "page": page},
                timeout=30,
            )
            if not resp.ok:
                logger.warning("Could not fetch %s transactions (page %d): %s", txn_type, page, resp.text[:200])
                break
            data = resp.json()
            items = data.get("data", [])
            if not items:
                break

            for item in items:
                txn_id: str = item["id"]
                splits = item.get("attributes", {}).get("transactions", [])
                if not splits:
                    continue
                split = splits[0]

                description: str = split.get("description", "")
                date_raw: str = split.get("date", "")
                date: str = date_raw[:10] if date_raw else ""
                amount: str = str(split.get("amount", "0"))
                currency: str = split.get("currency_code", "AED")

                account_name: str = (
                    split.get("source_name", "") if txn_type == "withdrawal"
                    else split.get("destination_name", "")
                )
                if not description or not account_name:
                    continue

                action, other_account = _check_transfer_rules(description, account_name, transfer_rules)

                if action == "skip":
                    to_delete.append(txn_id)
                elif action == "transfer" and other_account:
                    src, dst = (
                        (account_name, other_account) if txn_type == "withdrawal"
                        else (other_account, account_name)
                    )
                    to_update.append((txn_id, description, date, amount, currency, src, dst))
                elif not action:
                    # Fallback: auto-detect by account name in description
                    auto = _find_transfer_account(description, account_name, asset_accounts)
                    if auto:
                        src, dst = (
                            (account_name, auto) if txn_type == "withdrawal"
                            else (auto, account_name)
                        )
                        to_update.append((txn_id, description, date, amount, currency, src, dst))

            pagination = data.get("meta", {}).get("pagination", {})
            if page >= pagination.get("total_pages", 1):
                break
            page += 1

    _collect("withdrawal")
    _collect("deposit")

    updated, deleted = 0, 0
    errors: list[str] = []

    # Update source-side transactions to transfers first
    for txn_id, desc, date, amount, currency, src, dst in to_update:
        resp = requests.put(
            f"{FIREFLY_URL}/api/v1/transactions/{txn_id}",
            headers=_headers(),
            json={
                "apply_rules": False,
                "fire_webhooks": False,
                "transactions": [{
                    "type": "transfer",
                    "date": date,
                    "amount": amount,
                    "description": desc,
                    "source_name": src,
                    "destination_name": dst,
                    "currency_code": currency,
                }],
            },
            timeout=15,
        )
        if resp.ok:
            updated += 1
            logger.info("Converted to transfer: %s → %s (%s)", src, dst, desc[:60])
        else:
            errors.append(f"Update {txn_id} ({desc[:40]}): {resp.text[:200]}")

    # Then delete destination-side duplicates
    for txn_id in to_delete:
        resp = requests.delete(
            f"{FIREFLY_URL}/api/v1/transactions/{txn_id}",
            headers=_headers(),
            timeout=10,
        )
        if resp.ok:
            deleted += 1
        else:
            errors.append(f"Delete {txn_id}: {resp.text[:100]}")

    return {
        "success": len(errors) == 0,
        "updated_count": updated,
        "deleted_count": deleted,
        "error_count": len(errors),
        "errors": errors[:10],
    }


def apply_category_rules_to_transactions(rules: list) -> dict:
    """
    Apply category rules to all withdrawal/deposit transactions in Firefly III.
    First matching rule wins (exclusive — a transaction gets at most one category).
    Skips transfers. Only updates transactions whose category would actually change.
    """
    if not rules:
        return {"success": True, "updated_count": 0, "skipped_count": 0,
                "error_count": 0, "errors": [], "category_counts": {}}

    # Flatten rules into an ordered lookup list
    # tuple: (pattern, category_name, rule_account, rule_txn_type)
    # rule_account=None → global; rule_txn_type=None → both types
    lookup: list[tuple[str, str, str | None, str | None]] = []
    for rule in rules:
        subcat = (rule.get("subcategory") or "").strip()
        cat = (rule.get("category") or "").strip()
        category_name = subcat if subcat else cat
        rule_account = (rule.get("account") or "").strip().lower() or None
        rule_txn_type = (rule.get("transaction_type") or "").strip().lower() or None
        for p in rule.get("patterns", []):
            p = p.strip().lower()
            if p:
                lookup.append((p, category_name, rule_account, rule_txn_type))

    def _match(description: str, account_name: str, txn_type: str) -> str | None:
        desc_lower = description.lower()
        acc_lower = (account_name or "").lower()
        # Pass 1: account-specific rules take priority
        for pattern, category, rule_account, rule_txn_type in lookup:
            if rule_txn_type and rule_txn_type != txn_type:
                continue
            if rule_account and rule_account == acc_lower and pattern in desc_lower:
                return category
        # Pass 2: global rules
        for pattern, category, rule_account, rule_txn_type in lookup:
            if rule_txn_type and rule_txn_type != txn_type:
                continue
            if not rule_account and pattern in desc_lower:
                return category
        return None

    updated, skipped, errors = 0, 0, []
    category_counts: dict[str, int] = {}

    def _process_type(txn_type: str) -> None:
        nonlocal updated, skipped
        page = 1
        while True:
            resp = requests.get(
                f"{FIREFLY_URL}/api/v1/transactions",
                headers=_headers(),
                params={"type": txn_type, "limit": 100, "page": page},
                timeout=30,
            )
            if not resp.ok:
                errors.append(f"Fetch {txn_type} p{page}: {resp.text[:200]}")
                break
            data = resp.json()
            items = data.get("data", [])
            if not items:
                break

            for item in items:
                txn_id = item["id"]
                splits = item.get("attributes", {}).get("transactions", [])
                if not splits:
                    continue
                split = splits[0]
                description = split.get("description", "")
                account_name = (
                    split.get("source_name", "") if txn_type == "withdrawal"
                    else split.get("destination_name", "")
                )

                category = _match(description, account_name, txn_type)
                if not category:
                    skipped += 1
                    continue
                if split.get("category_name") == category:
                    skipped += 1
                    continue

                date = (split.get("date") or "")[:10]
                amount = str(split.get("amount", "0"))
                currency = split.get("currency_code", "AED")

                if txn_type == "withdrawal":
                    entry = {
                        "type": "withdrawal",
                        "date": date,
                        "amount": amount,
                        "description": description,
                        "source_name": account_name,
                        "currency_code": currency,
                        "category_name": category,
                    }
                else:
                    entry = {
                        "type": "deposit",
                        "date": date,
                        "amount": amount,
                        "description": description,
                        "destination_name": account_name,
                        "currency_code": currency,
                        "category_name": category,
                    }

                put_resp = requests.put(
                    f"{FIREFLY_URL}/api/v1/transactions/{txn_id}",
                    headers=_headers(),
                    json={"apply_rules": False, "fire_webhooks": False, "transactions": [entry]},
                    timeout=15,
                )
                if put_resp.ok:
                    updated += 1
                    category_counts[category] = category_counts.get(category, 0) + 1
                    logger.info("Categorized [%s] → %s", description[:60], category)
                else:
                    errors.append(f"{txn_id} ({description[:40]}): {put_resp.text[:200]}")

            pagination = data.get("meta", {}).get("pagination", {})
            if page >= pagination.get("total_pages", 1):
                break
            page += 1

    _process_type("withdrawal")
    _process_type("deposit")

    return {
        "success": len(errors) == 0,
        "updated_count": updated,
        "skipped_count": skipped,
        "error_count": len(errors),
        "errors": errors[:10],
        "category_counts": category_counts,
    }


def _load_transfer_rules_from_disk() -> dict:
    """
    Load transfer rules: user-defined rules (from disk) prepended to built-in defaults.
    User rules are checked first so they can override or extend defaults.
    """
    import os
    from pathlib import Path
    rules_path = Path(os.getenv("TRANSFER_RULES_FILE", "/data/transfer_rules.json"))
    try:
        user_rules: dict = json.loads(rules_path.read_text())
    except Exception:
        user_rules = {}

    merged: dict = {}
    for account in set(list(user_rules.keys()) + list(_DEFAULT_TRANSFER_RULES.keys())):
        merged[account] = user_rules.get(account, []) + _DEFAULT_TRANSFER_RULES.get(account, [])
    return merged


def push_to_firefly(transactions: list[dict]) -> dict:
    # Fetch asset accounts once so transfer detection works for the whole batch
    asset_accounts = _get_asset_accounts()
    transfer_rules = _load_transfer_rules_from_disk()
    logger.info(
        "Loaded %d asset accounts and transfer rules for %d accounts",
        len(asset_accounts), len(transfer_rules),
    )

    imported = 0
    skipped = 0
    errors = []

    for txn in transactions:
        try:
            payload = _build_payload(txn, asset_accounts, transfer_rules)
            if payload is None:
                logger.info(
                    "Skipping destination-side transfer: %s",
                    txn.get("description", "")[:80],
                )
                skipped += 1
                continue
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
        "skipped_count": skipped,
        "error_count": len(errors),
        "errors": errors[:5],
    }
