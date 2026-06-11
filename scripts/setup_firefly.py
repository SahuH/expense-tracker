#!/usr/bin/env python3
"""
One-time setup: creates accounts, categories, and keyword-mapping rules in Firefly III.
Run after first login and token generation:
    python3 scripts/setup_firefly.py
"""
import os
import sys
import time
import requests

FIREFLY_URL = os.getenv("FIREFLY_URL", "http://localhost:8080")
ACCESS_TOKEN = os.getenv("FIREFLY_ACCESS_TOKEN", "")

if not ACCESS_TOKEN:
    print("ERROR: Set FIREFLY_ACCESS_TOKEN in your environment or .env file")
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

ASSET_ACCOUNTS = [
    "Joint Current Account",
    "Harsh Savings",
    "Wife Savings",
    "Harsh Credit Card",
    "Wife Credit Card",
]

CATEGORIES = [
    "Groceries",
    "Dining",
    "Transport",
    "Utilities",
    "Rent/Mortgage",
    "Shopping",
    "Healthcare",
    "Entertainment",
    "Travel",
    "Transfers",
    "Other",
]

# (keyword_in_description, category_name)
KEYWORD_RULES = [
    # Groceries
    ("Carrefour", "Groceries"),
    ("Spinneys", "Groceries"),
    ("Waitrose", "Groceries"),
    ("Lulu", "Groceries"),
    ("Union Coop", "Groceries"),
    ("Choithrams", "Groceries"),
    ("Geant", "Groceries"),
    # Transport
    ("Uber", "Transport"),
    ("Careem", "Transport"),
    ("Dubai Taxi", "Transport"),
    ("RTA", "Transport"),
    ("Salik", "Transport"),
    ("ENOC", "Transport"),
    ("ADNOC", "Transport"),
    ("EMARAT", "Transport"),
    # Entertainment / Subscriptions
    ("Netflix", "Entertainment"),
    ("Spotify", "Entertainment"),
    ("Apple Music", "Entertainment"),
    ("Disney+", "Entertainment"),
    ("OSN", "Entertainment"),
    ("Cinemas", "Entertainment"),
    ("Reel", "Entertainment"),
    # Utilities
    ("DEWA", "Utilities"),
    ("ADDC", "Utilities"),
    ("Etisalat", "Utilities"),
    ("du ", "Utilities"),
    ("Emaar", "Utilities"),
    # Dining
    ("Restaurant", "Dining"),
    ("Cafe", "Dining"),
    ("Coffee", "Dining"),
    ("McDonalds", "Dining"),
    ("KFC", "Dining"),
    ("Pizza", "Dining"),
    ("Deliveroo", "Dining"),
    ("Talabat", "Dining"),
    # Shopping
    ("Amazon", "Shopping"),
    ("Noon", "Shopping"),
    ("H&M", "Shopping"),
    ("Zara", "Shopping"),
    ("IKEA", "Shopping"),
    ("ACE", "Shopping"),
    # Healthcare
    ("Pharmacy", "Healthcare"),
    ("Hospital", "Healthcare"),
    ("Clinic", "Healthcare"),
    ("Medical", "Healthcare"),
    # Travel
    ("Emirates", "Travel"),
    ("flydubai", "Travel"),
    ("AirArabia", "Travel"),
    ("Booking.com", "Travel"),
    ("Airbnb", "Travel"),
    ("Hotel", "Travel"),
]


def _post(path: str, payload: dict) -> dict:
    resp = requests.post(f"{FIREFLY_URL}/api/v1{path}", headers=HEADERS, json=payload, timeout=30)
    if resp.status_code in (200, 201, 422):
        return resp.json()
    resp.raise_for_status()


def create_accounts():
    print("\n── Creating asset accounts ───────────────────────────────")
    for name in ASSET_ACCOUNTS:
        try:
            result = _post(
                "/accounts",
                {
                    "name": name,
                    "type": "asset",
                    "currency_code": "AED",
                    "account_role": "defaultAsset",
                },
            )
            if result.get("errors"):
                print(f"  SKIP (exists?): {name}")
            else:
                acct_id = result.get("data", {}).get("id", "?")
                print(f"  Created: {name} (id={acct_id})")
        except Exception as exc:
            print(f"  ERROR creating {name}: {exc}")
        time.sleep(0.3)


def create_categories():
    print("\n── Creating categories ───────────────────────────────────")
    for name in CATEGORIES:
        try:
            result = _post("/categories", {"name": name})
            if result.get("errors"):
                print(f"  SKIP (exists?): {name}")
            else:
                cat_id = result.get("data", {}).get("id", "?")
                print(f"  Created: {name} (id={cat_id})")
        except Exception as exc:
            print(f"  ERROR creating {name}: {exc}")
        time.sleep(0.3)


def create_rules():
    print("\n── Creating keyword → category rules ────────────────────")
    for keyword, category in KEYWORD_RULES:
        rule_name = f"Auto: {keyword} → {category}"
        payload = {
            "title": rule_name,
            "trigger": "store-journal",
            "active": True,
            "strict": False,
            "stop_processing": False,
            "triggers": [
                {
                    "type": "description_contains",
                    "value": keyword,
                    "active": True,
                    "stop_processing": False,
                }
            ],
            "actions": [
                {
                    "type": "set_category",
                    "value": category,
                    "active": True,
                    "stop_processing": False,
                }
            ],
        }
        try:
            result = _post("/rules", payload)
            if result.get("errors"):
                print(f"  SKIP: {rule_name}")
            else:
                print(f"  Created rule: {keyword!r} → {category}")
        except Exception as exc:
            print(f"  ERROR creating rule {keyword}: {exc}")
        time.sleep(0.2)


if __name__ == "__main__":
    print(f"Connecting to Firefly at {FIREFLY_URL}")
    create_accounts()
    create_categories()
    create_rules()
    print("\nSetup complete.")
