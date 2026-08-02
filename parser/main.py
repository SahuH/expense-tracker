import os
import json
import uuid
import hashlib
import tempfile
import logging
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse

from extractor import extract_with_pdfplumber
from validator import verify_balance
from firefly_client import push_to_firefly, get_effective_transfer_rules, apply_rules_to_existing_transactions

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="PDF Parser Service", version="1.0.0")

_PASSWORDS_FILE = Path(os.getenv("PASSWORDS_FILE", "/data/passwords.json"))
_TRANSFER_RULES_FILE = Path(os.getenv("TRANSFER_RULES_FILE", "/data/transfer_rules.json"))
_CATEGORIES_FILE = Path(os.getenv("CATEGORIES_FILE", "/data/categories.json"))
_UPLOAD_HISTORY_FILE = Path(os.getenv("UPLOAD_HISTORY_FILE", "/data/upload_history.json"))


def _load_passwords() -> dict:
    try:
        return json.loads(_PASSWORDS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_password(account_name: str, password: str) -> None:
    passwords = _load_passwords()
    passwords[account_name] = password
    _PASSWORDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PASSWORDS_FILE.write_text(json.dumps(passwords, indent=2))


def _delete_password(account_name: str) -> None:
    passwords = _load_passwords()
    if account_name in passwords:
        passwords.pop(account_name)
        _PASSWORDS_FILE.write_text(json.dumps(passwords, indent=2))


# ── Upload-history helpers (Layer 1 dedup) ────────────────────────────────────

def _load_upload_history() -> list:
    try:
        return json.loads(_UPLOAD_HISTORY_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_upload_history(history: list) -> None:
    _UPLOAD_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _UPLOAD_HISTORY_FILE.write_text(json.dumps(history, indent=2))


def _find_in_history(file_hash: str) -> dict | None:
    """Return the history entry for this hash, or None if not seen before."""
    for entry in _load_upload_history():
        if entry.get("hash") == file_hash:
            return entry
    return None


def _record_upload(file_hash: str, filename: str, account: str, imported_count: int) -> None:
    history = _load_upload_history()
    history.append({
        "hash": file_hash,
        "filename": filename,
        "account": account,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "imported_count": imported_count,
    })
    _save_upload_history(history)


# ── Category-rule helpers ─────────────────────────────────────────────────────

_DEFAULT_CATEGORY_RULES: list = [
    {"id": "food-delivery-001", "category": "Food & Dining", "subcategory": "Food Delivery",
     "patterns": ["talabat", "deliveroo", "careem food", "hunger station", "noon food"]},
    {"id": "fast-food-001", "category": "Food & Dining", "subcategory": "Fast Food",
     "patterns": ["mcdonald", "kfc", "pizza hut", "subway", "burger king", "hardees", "popeyes",
                  "five guys", "shake shack", "little caesars", "dominos", "papa john"]},
    {"id": "cafe-coffee-001", "category": "Food & Dining", "subcategory": "Cafes & Coffee",
     "patterns": ["starbucks", "costa coffee", "tim hortons", "dunkin", "caribou coffee",
                  "paul cafe", "shakespeare and co", "tchibo", "coffee planet", "second cup"]},
    {"id": "restaurants-001", "category": "Food & Dining", "subcategory": "Restaurants",
     "patterns": ["veg world restaurant", "zaatar w zeit", "applebees", "chilis", "nandos",
                  "wagamama", "cheesecake factory", "pf chang", "ihop", "restaurant"]},
    {"id": "groceries-001", "category": "Food & Dining", "subcategory": "Groceries",
     "patterns": ["carrefour", "lulu hypermarket", "lulu express", "union coop", "spinneys",
                  "waitrose", "al maya", "choithrams", "geant", "west zone", "key pee mart",
                  "aswaaq", "nesto hypermarket", "viva supermarket", "grandiose", "spar",
                  "safari hypermarket"]},
    {"id": "bakeries-001", "category": "Food & Dining", "subcategory": "Bakeries",
     "patterns": ["paul bakery", "le pain quotidien", "cinnabon", "krispy kreme",
                  "baskin robbins", "magnolia bakery"]},
    {"id": "beverages-alcohol-001", "category": "Food & Dining", "subcategory": "Beverages & Alcohol",
     "patterns": ["mmi dubai", "mmi abu", "maritime mercantile", "african eastern", "le clos"]},
    {"id": "ride-hailing-001", "category": "Transportation", "subcategory": "Ride Hailing",
     "patterns": ["careem deliveries", "uber trip", "bolt ride", "indrive", "ola cab"]},
    {"id": "fuel-petrol-001", "category": "Transportation", "subcategory": "Fuel & Petrol",
     "patterns": ["adnoc distribution", "adnoc station", "enoc", "eppco", "emarat",
                  "total energies", "shell station", "caltex"]},
    {"id": "parking-tolls-001", "category": "Transportation", "subcategory": "Parking & Tolls",
     "patterns": ["salik", "mawaqif", "parkin", "rta parking", "dubai parking"]},
    {"id": "utilities-elec-water-001", "category": "Utilities", "subcategory": "Electricity & Water",
     "patterns": ["dewa", "sewa", "fewa", "addc", "aadc", "aquacool metering",
                  "empower", "emicool", "district cooling"]},
    {"id": "utilities-water-delivery-001", "category": "Utilities", "subcategory": "Water Delivery",
     "patterns": ["al ain water", "masafi delivery", "oasis water", "culligan",
                  "water delivery", "spring water"]},
    {"id": "utilities-telecom-001", "category": "Utilities", "subcategory": "Telecom",
     "patterns": ["etisalat", "du telecom", "du internet", "du mobile", "virgin mobile uae"]},
    {"id": "shopping-online-001", "category": "Shopping", "subcategory": "Online Shopping",
     "patterns": ["noon.com", "amazon ae", "amazon.ae", "souq.com", "namshi",
                  "shein", "6thstreet"]},
    {"id": "shopping-electronics-001", "category": "Shopping", "subcategory": "Electronics",
     "patterns": ["sharaf dg", "istore", "plug ins", "jumbo electronics", "emax",
                  "virgin megastore", "apple store", "axiom telecom"]},
    {"id": "shopping-clothing-001", "category": "Shopping", "subcategory": "Clothing",
     "patterns": ["zara", "mango", "gap store", "forever 21", "cotton on",
                  "marks and spencer", "centrepoint", "max fashion", "levis store",
                  "brands for less", "splash fashion"]},
    {"id": "shopping-home-001", "category": "Shopping", "subcategory": "Home & Furniture",
     "patterns": ["ikea", "pan emirates", "home centre", "pottery barn", "homes r us",
                  "danube home", "ace hardware"]},
    {"id": "entertainment-streaming-001", "category": "Entertainment", "subcategory": "Streaming Services",
     "patterns": ["netflix", "spotify", "apple.com/bill", "apple subscription",
                  "disney plus", "disneyplus", "starz play", "shahid vip",
                  "youtube premium", "osn streaming", "anghami"]},
    {"id": "entertainment-cinema-001", "category": "Entertainment", "subcategory": "Cinema",
     "patterns": ["vox cinemas", "reel cinemas", "novo cinemas", "cinepolis"]},
    {"id": "health-pharmacy-001", "category": "Health & Wellness", "subcategory": "Pharmacy",
     "patterns": ["aster pharmacy", "life pharmacy", "boots pharmacy", "al zahra pharmacy",
                  "binsina pharmacy", "tabib pharmacy"]},
    {"id": "health-gym-001", "category": "Health & Wellness", "subcategory": "Gym & Fitness",
     "patterns": ["fitness first", "golds gym", "ufc gym", "snap fitness", "anytime fitness",
                  "warehouse gym", "crossfit dubai", "gymnation"]},
    {"id": "health-salon-001", "category": "Health & Wellness", "subcategory": "Salons & Beauty",
     "patterns": ["sephora", "mac cosmetics", "nail studio", "toni and guy",
                  "tips and toes", "waxing company"]},
    {"id": "travel-hotels-001", "category": "Travel", "subcategory": "Hotels",
     "patterns": ["marriott", "hilton hotel", "hyatt hotel", "intercontinental",
                  "radisson", "rotana hotel", "address hotel", "jumeirah hotel",
                  "sofitel", "ibis hotel", "novotel", "ritz carlton",
                  "booking.com", "airbnb"]},
    {"id": "travel-airlines-001", "category": "Travel", "subcategory": "Airlines",
     "patterns": ["emirates airlines", "flydubai", "air arabia", "etihad airways",
                  "flynas", "indigo airlines", "british airways", "lufthansa",
                  "qatar airways", "turkish airlines", "jazeera airways"]},
    {"id": "government-services-001", "category": "Government", "subcategory": "Government Services",
     "patterns": ["rta dubai", "dubai land department", "amer center", "dnrd",
                  "gdrfa", "ica uae", "mohre", "municipality fee", "ejari",
                  "tasheel", "typing center"]},
    {"id": "government-fines-001", "category": "Government", "subcategory": "Traffic & Fines",
     "patterns": ["traffic fine", "rta fine", "police fine", "nol recharge",
                  "darb payment", "parking fine", "saaed fine"]},
    {"id": "financial-charges-001", "category": "Financial", "subcategory": "Bank Charges & Fees",
     "patterns": ["bank charge", "service fee", "annual fee", "processing fee",
                  "late payment fee", "cash advance fee", "card renewal fee"]},
    {"id": "shopping-trade-001", "category": "Shopping", "subcategory": "Trade Supplies",
     "patterns": ["besto trading"]},
]


def _load_category_rules() -> list:
    try:
        return json.loads(_CATEGORIES_FILE.read_text())
    except FileNotFoundError:
        # First run — seed with default rules and persist them
        _save_category_rules(_DEFAULT_CATEGORY_RULES)
        return _DEFAULT_CATEGORY_RULES
    except json.JSONDecodeError:
        return []


def _save_category_rules(rules: list) -> None:
    _CATEGORIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CATEGORIES_FILE.write_text(json.dumps(rules, indent=2))


def _all_patterns(
    rules: list,
    exclude_id: str = None,
    scoped_account: str = None,
    scoped_txn_type: str = None,
) -> dict[str, str]:
    """
    Return {pattern_lower: label} for patterns that would conflict when adding to a rule
    scoped to `scoped_account` (None = global) and `scoped_txn_type` (None = both types).

    Conflict rules:
      Same account scope AND overlapping transaction type → conflict
      global vs account-specific → no conflict (account-specific overrides global)
      account-A vs account-B    → no conflict
      withdrawal vs deposit     → no conflict (disjoint types)
      withdrawal vs None (both) → conflict (the "both" rule overlaps withdrawal)
    """
    norm = lambda s: (s or "").strip().lower() or None
    scoped = norm(scoped_account)
    scoped_tt = norm(scoped_txn_type)
    out = {}
    for rule in rules:
        if rule.get("id") == exclude_id:
            continue
        rule_acc = norm(rule.get("account"))
        # Same account-scope bucket check
        if scoped is None and rule_acc is not None:
            continue  # global vs account-specific → no conflict
        if scoped is not None and rule_acc is None:
            continue  # account-specific vs global → no conflict
        if scoped is not None and rule_acc is not None and rule_acc != scoped:
            continue  # different accounts → no conflict
        # Transaction-type overlap check
        rule_tt = norm(rule.get("transaction_type"))
        # No overlap only when both types are specific AND different
        if scoped_tt and rule_tt and scoped_tt != rule_tt:
            continue  # e.g. withdrawal vs deposit → no conflict
        label = f"{rule.get('category', '')} > {rule.get('subcategory', '')}".strip(" >")
        if rule_acc:
            label += f" [{rule.get('account')}]"
        for p in rule.get("patterns", []):
            out[p.lower().strip()] = label
    return out


def _load_transfer_rules() -> dict:
    try:
        return json.loads(_TRANSFER_RULES_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_transfer_rules(rules: dict) -> None:
    _TRANSFER_RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TRANSFER_RULES_FILE.write_text(json.dumps(rules, indent=2))


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/passwords/{account_name}")
def get_password_status(account_name: str):
    """Return whether a saved PDF password exists for this account (never returns the password itself)."""
    return {"has_password": account_name in _load_passwords()}


@app.delete("/passwords/{account_name}")
def remove_password(account_name: str):
    """Delete the saved PDF password for an account."""
    _delete_password(account_name)
    return {"success": True}


# ── Transfer-rule endpoints ───────────────────────────────────────────────────

@app.get("/transfer-rules/{account_name}")
def get_transfer_rules(account_name: str):
    """Return the keyword-based transfer rules for an account."""
    return {"rules": _load_transfer_rules().get(account_name, [])}


@app.post("/transfer-rules/{account_name}")
async def add_transfer_rule(account_name: str, rule: dict):
    """Append a rule: {keyword, other_account} for transfer or {keyword, action:"skip"} to drop."""
    if not rule.get("keyword"):
        raise HTTPException(status_code=400, detail="keyword is required")
    is_skip = rule.get("action") == "skip"
    if not is_skip and not rule.get("other_account"):
        raise HTTPException(status_code=400, detail="other_account is required for transfer rules")
    rules = _load_transfer_rules()
    entry: dict = {"keyword": rule["keyword"]}
    if is_skip:
        entry["action"] = "skip"
    else:
        entry["other_account"] = rule["other_account"]
    rules.setdefault(account_name, []).append(entry)
    _save_transfer_rules(rules)
    return {"success": True}


@app.delete("/transfer-rules/{account_name}/{rule_index}")
def delete_transfer_rule(account_name: str, rule_index: int):
    """Remove one transfer rule by index."""
    rules = _load_transfer_rules()
    account_rules = rules.get(account_name, [])
    if 0 <= rule_index < len(account_rules):
        account_rules.pop(rule_index)
        rules[account_name] = account_rules
        _save_transfer_rules(rules)
    return {"success": True}


# ── Category-rule CRUD ────────────────────────────────────────────────────────

@app.get("/category-rules")
def list_category_rules():
    return {"rules": _load_category_rules()}


@app.post("/category-rules")
async def create_category_rule(rule: dict):
    category = (rule.get("category") or "").strip()
    subcategory = (rule.get("subcategory") or "").strip()
    account = (rule.get("account") or "").strip() or None
    transaction_type = (rule.get("transaction_type") or "").strip().lower() or None
    patterns = [p.strip().lower() for p in rule.get("patterns", []) if p.strip()]
    if not category:
        raise HTTPException(status_code=400, detail="category is required")
    if not patterns:
        raise HTTPException(status_code=400, detail="at least one pattern is required")
    if transaction_type and transaction_type not in ("withdrawal", "deposit"):
        raise HTTPException(status_code=400, detail="transaction_type must be 'withdrawal', 'deposit', or null")

    existing = _load_category_rules()
    taken = _all_patterns(existing, scoped_account=account, scoped_txn_type=transaction_type)
    conflicts = [{"pattern": p, "rule": taken[p]} for p in patterns if p in taken]
    if conflicts:
        raise HTTPException(status_code=409, detail={"conflicts": conflicts})

    new_rule = {
        "id": str(uuid.uuid4()),
        "category": category,
        "subcategory": subcategory,
        "patterns": patterns,
    }
    if account:
        new_rule["account"] = account
    if transaction_type:
        new_rule["transaction_type"] = transaction_type
    existing.append(new_rule)
    _save_category_rules(existing)
    return {"success": True, "rule": new_rule}


@app.patch("/category-rules/{rule_id}")
async def update_category_rule(rule_id: str, body: dict):
    """Rename category/subcategory/account/transaction_type of an existing rule (does not touch patterns)."""
    rules = _load_category_rules()
    for rule in rules:
        if rule.get("id") == rule_id:
            if "category" in body:
                rule["category"] = body["category"].strip()
            if "subcategory" in body:
                rule["subcategory"] = body["subcategory"].strip()
            if "account" in body:
                acc = (body["account"] or "").strip() or None
                if acc:
                    rule["account"] = acc
                else:
                    rule.pop("account", None)
            if "transaction_type" in body:
                tt = (body["transaction_type"] or "").strip().lower() or None
                if tt:
                    rule["transaction_type"] = tt
                else:
                    rule.pop("transaction_type", None)
            break
    else:
        raise HTTPException(status_code=404, detail="rule not found")
    _save_category_rules(rules)
    return {"success": True}


@app.delete("/category-rules/{rule_id}")
def delete_category_rule(rule_id: str):
    rules = _load_category_rules()
    rules = [r for r in rules if r.get("id") != rule_id]
    _save_category_rules(rules)
    return {"success": True}


@app.post("/category-rules/{rule_id}/patterns")
async def add_pattern(rule_id: str, body: dict):
    pattern = (body.get("pattern") or "").strip().lower()
    if not pattern:
        raise HTTPException(status_code=400, detail="pattern is required")

    rules = _load_category_rules()
    # Find the rule's own account and transaction_type so conflict check uses the right scope
    target = next((r for r in rules if r.get("id") == rule_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="rule not found")
    rule_account = (target.get("account") or "").strip() or None
    rule_txn_type = (target.get("transaction_type") or "").strip() or None

    taken = _all_patterns(rules, exclude_id=rule_id, scoped_account=rule_account, scoped_txn_type=rule_txn_type)
    if pattern in taken:
        raise HTTPException(
            status_code=409,
            detail={"conflicts": [{"pattern": pattern, "rule": taken[pattern]}]},
        )

    if pattern not in [p.lower() for p in target.get("patterns", [])]:
        target.setdefault("patterns", []).append(pattern)

    _save_category_rules(rules)
    return {"success": True}


@app.delete("/category-rules/{rule_id}/patterns/{pattern_index}")
def delete_pattern(rule_id: str, pattern_index: int):
    rules = _load_category_rules()
    for rule in rules:
        if rule.get("id") == rule_id:
            patterns = rule.get("patterns", [])
            if 0 <= pattern_index < len(patterns):
                patterns.pop(pattern_index)
            break
    _save_category_rules(rules)
    return {"success": True}


@app.post("/category-rules/apply")
async def apply_category_rules():
    from firefly_client import apply_category_rules_to_transactions
    rules = _load_category_rules()
    result = apply_category_rules_to_transactions(rules)
    return result


@app.post("/category-rules/seed")
async def seed_category_rules(body: dict):
    """Replace all category rules with the provided seed list (for initial setup)."""
    rules = body.get("rules", [])
    if not isinstance(rules, list):
        raise HTTPException(status_code=400, detail="rules must be a list")
    _save_category_rules(rules)
    return {"success": True, "count": len(rules)}


@app.post("/parse")
async def parse_statement(
    file: UploadFile = File(...),
    account_name: str = Form(...),
    password: str = Form(default=""),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    # Use caller-supplied password; fall back to saved password for this account
    effective_password = password or _load_passwords().get(account_name, "")

    # Write upload to a temp file; delete immediately after processing
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = tmp.name
        content = await file.read()
        tmp.write(content)

    file_hash = hashlib.sha256(content).hexdigest()

    try:
        logger.info("Extracting transactions from %s (hash=%s…)", file.filename, file_hash[:12])
        result = extract_with_pdfplumber(tmp_path, account_name, effective_password)

        # PDF is encrypted and the password was wrong or missing — bubble up to UI
        if result.get("error") in ("password_required", "password_incorrect"):
            return JSONResponse(content={
                "status": result["error"],
                "extraction_method": "pdfplumber",
                "confidence": 0.0,
                "metadata": {},
                "transactions": [],
                "balance_check": {},
                "file_hash": file_hash,
            })

        # A newly-supplied password worked — save it for future uploads of this account
        if password and password != _load_passwords().get(account_name):
            _save_password(account_name, password)
            logger.info("Saved PDF password for account: %s", account_name)

        verification = verify_balance(
            result["transactions"],
            result.get("metadata", {}),
            statement_type=result.get("statement_type", "bank_account"),
        )

        response = {
            "extraction_method": result["method"],
            "statement_type": result.get("statement_type", "bank_account"),
            "confidence": result["confidence"],
            "metadata": result.get("metadata", {}),
            "transactions": result["transactions"],
            "balance_check": verification,
            "file_hash": file_hash,
        }

        # Layer 1: warn if this exact file was imported before
        prior = _find_in_history(file_hash)
        if prior:
            response["duplicate_file"] = prior

        balance_confirmed = verification.get("passed") and verification.get("reason") == "ok"
        if balance_confirmed and result["confidence"] >= 0.7:
            response["status"] = "verified"
        else:
            response["status"] = "needs_review"

        return JSONResponse(content=response)

    finally:
        # Always delete the PDF immediately
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@app.get("/rules/effective")
def get_rules_effective():
    """Return all active transfer rules (user-defined + built-in) tagged with origin."""
    return {"rules": get_effective_transfer_rules()}


@app.put("/rules/user")
async def replace_user_rules(body: dict):
    """Replace all user-defined transfer rules wholesale."""
    rules = body.get("rules", {})
    if not isinstance(rules, dict):
        raise HTTPException(status_code=400, detail="rules must be a dict keyed by account name")
    for account, rule_list in rules.items():
        if not isinstance(rule_list, list):
            raise HTTPException(status_code=400, detail=f"Rules for '{account}' must be a list")
        for rule in rule_list:
            if not rule.get("keyword"):
                raise HTTPException(status_code=400, detail=f"Rule for '{account}' missing keyword")
            if rule.get("action") != "skip" and not rule.get("other_account"):
                raise HTTPException(status_code=400, detail=f"Transfer rule for '{account}' missing other_account")
    _save_transfer_rules(rules)
    return {"success": True, "accounts": len(rules)}


@app.post("/rules/apply")
async def apply_rules():
    """Retroactively apply transfer rules to all existing Firefly transactions."""
    result = apply_rules_to_existing_transactions()
    return result


@app.get("/upload-history/{file_hash}")
def check_upload_history(file_hash: str):
    """Return the prior upload record for this SHA-256 hash, or 404 if new."""
    entry = _find_in_history(file_hash)
    if entry:
        return {"duplicate": True, "prior_upload": entry}
    return {"duplicate": False}


@app.post("/import")
async def import_to_firefly(payload: dict):
    """Push a verified transactions payload directly to Firefly importer."""
    transactions = payload.get("transactions", [])
    if not transactions:
        raise HTTPException(status_code=400, detail="No transactions provided")

    result = push_to_firefly(transactions)

    # Record the file in upload history so Layer 1 can detect re-uploads
    file_hash = payload.get("file_hash")
    filename = payload.get("filename", "")
    account = payload.get("account", "")
    if file_hash and result.get("imported_count", 0) + result.get("duplicate_count", 0) > 0:
        _record_upload(file_hash, filename, account, result.get("imported_count", 0))

    return result
