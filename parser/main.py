import os
import json
import tempfile
import logging
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse

from extractor import extract_with_pdfplumber
from validator import verify_balance
from firefly_client import push_to_firefly

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="PDF Parser Service", version="1.0.0")

# Passwords are stored in a Docker volume so they survive container restarts.
# The file contains plaintext passwords — acceptable for a self-hosted household app
# on a private server where the operator already has full disk access.
_PASSWORDS_FILE = Path(os.getenv("PASSWORDS_FILE", "/data/passwords.json"))
_TRANSFER_RULES_FILE = Path(os.getenv("TRANSFER_RULES_FILE", "/data/transfer_rules.json"))


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
    """Append a transfer rule: {keyword: str, other_account: str}."""
    if not rule.get("keyword") or not rule.get("other_account"):
        raise HTTPException(status_code=400, detail="keyword and other_account are required")
    rules = _load_transfer_rules()
    account_rules = rules.setdefault(account_name, [])
    account_rules.append({"keyword": rule["keyword"], "other_account": rule["other_account"]})
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

    try:
        logger.info("Extracting transactions from %s", file.filename)
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
        }

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


@app.post("/import")
async def import_to_firefly(payload: dict):
    """Push a verified transactions payload directly to Firefly importer."""
    transactions = payload.get("transactions", [])
    if not transactions:
        raise HTTPException(status_code=400, detail="No transactions provided")

    result = push_to_firefly(transactions)
    return result
