"""
All Firefly III API calls centralised here.
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


def _get(path: str, params: dict = None) -> dict:
    resp = requests.get(
        f"{FIREFLY_URL}/api/v1{path}",
        headers=_headers(),
        params=params or {},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _get_all_pages(path: str, params: dict = None) -> list:
    params = params or {}
    params["limit"] = 100
    params["page"] = 1
    all_data = []
    while True:
        data = _get(path, params)
        all_data.extend(data.get("data", []))
        meta = data.get("meta", {}).get("pagination", {})
        if params["page"] >= meta.get("total_pages", 1):
            break
        params["page"] += 1
    return all_data


def get_accounts() -> list:
    return _get_all_pages("/accounts", {"type": "asset"})


def get_transactions(
    start: str = None,
    end: str = None,
    account_id: str = None,
    limit: int = 500,
) -> list:
    params = {"limit": limit}
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    if account_id:
        return _get_all_pages(f"/accounts/{account_id}/transactions", params)
    return _get_all_pages("/transactions", params)


def get_categories() -> list:
    return _get_all_pages("/categories")


def get_summary(start: str, end: str) -> dict:
    return _get("/summary/basic", {"start": start, "end": end})


def get_budgets(start: str = None, end: str = None) -> list:
    params = {}
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    return _get_all_pages("/budgets", params)


def create_account(name: str, account_type: str, currency: str = "AED") -> dict:
    payload = {
        "name": name,
        "type": account_type,
        "currency_code": currency,
    }
    resp = requests.post(
        f"{FIREFLY_URL}/api/v1/accounts",
        headers=_headers(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def create_category(name: str) -> dict:
    resp = requests.post(
        f"{FIREFLY_URL}/api/v1/categories",
        headers=_headers(),
        json={"name": name},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def create_rule(name: str, trigger: str, action_value: str) -> dict:
    payload = {
        "title": name,
        "trigger": "store-journal",
        "active": True,
        "strict": False,
        "triggers": [
            {"type": "description_contains", "value": trigger, "active": True}
        ],
        "actions": [
            {"type": "set_category", "value": action_value, "active": True}
        ],
    }
    resp = requests.post(
        f"{FIREFLY_URL}/api/v1/rules",
        headers=_headers(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
