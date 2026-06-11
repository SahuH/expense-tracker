"""
pdfplumber-based deterministic PDF extraction.
Returns a dict with keys: transactions, metadata, confidence, method.
"""
import re
import logging
from datetime import datetime
from typing import Optional
import pdfplumber
from dateutil import parser as dateutil_parser

logger = logging.getLogger(__name__)

# Regex patterns for common bank statement layouts
DATE_PATTERNS = [
    r"\d{2}/\d{2}/\d{4}",
    r"\d{2}-\d{2}-\d{4}",
    r"\d{2}\s+\w{3}\s+\d{4}",
    r"\d{4}-\d{2}-\d{2}",
]

AMOUNT_PATTERN = re.compile(r"[\d,]+\.\d{2}")
COMBINED_DATE_RE = re.compile("|".join(DATE_PATTERNS))


def _parse_amount(raw: str) -> float:
    return float(raw.replace(",", ""))


def _parse_date(raw: str) -> Optional[str]:
    try:
        dt = dateutil_parser.parse(raw, dayfirst=True)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def _extract_tables(pdf_path: str, account_name: str) -> dict:
    transactions = []
    metadata = {}
    pages_with_tables = 0

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            tables = page.extract_tables()
            if not tables:
                continue

            pages_with_tables += 1
            for table in tables:
                if not table or len(table) < 2:
                    continue
                header = [str(c).lower().strip() if c else "" for c in table[0]]
                for row in table[1:]:
                    if not row or all(c is None or str(c).strip() == "" for c in row):
                        continue
                    txn = _map_row_to_transaction(header, row, account_name)
                    if txn:
                        transactions.append(txn)

    return transactions, metadata, pages_with_tables


def _map_row_to_transaction(header: list, row: list, account_name: str) -> Optional[dict]:
    """Best-effort mapping from a table row to the standard transaction schema."""
    row_str = [str(c).strip() if c is not None else "" for c in row]

    date_val = None
    description = ""
    debit_amount = None
    credit_amount = None
    balance = None

    for i, cell in enumerate(row_str):
        col = header[i] if i < len(header) else ""
        if not date_val and COMBINED_DATE_RE.search(cell):
            date_val = _parse_date(COMBINED_DATE_RE.search(cell).group())
        if any(k in col for k in ("desc", "narr", "particular", "detail", "remark")):
            description = cell
        if any(k in col for k in ("debit", "withdrawal", "dr")):
            m = AMOUNT_PATTERN.search(cell)
            if m:
                debit_amount = _parse_amount(m.group())
        if any(k in col for k in ("credit", "deposit", "cr")):
            m = AMOUNT_PATTERN.search(cell)
            if m:
                credit_amount = _parse_amount(m.group())
        if "balance" in col:
            m = AMOUNT_PATTERN.search(cell)
            if m:
                balance = _parse_amount(m.group())

    # If column headers didn't help, fall back to positional heuristics
    if date_val is None:
        for cell in row_str:
            if COMBINED_DATE_RE.search(cell):
                date_val = _parse_date(COMBINED_DATE_RE.search(cell).group())
                break

    if not description:
        # Pick the longest non-numeric cell as description
        candidates = [c for c in row_str if c and not AMOUNT_PATTERN.fullmatch(c)]
        if candidates:
            description = max(candidates, key=len)

    if date_val is None or (debit_amount is None and credit_amount is None):
        return None

    txn_type = "debit" if debit_amount is not None else "credit"
    amount = debit_amount if debit_amount is not None else credit_amount

    return {
        "date": date_val,
        "description": description,
        "amount": amount,
        "type": txn_type,
        "account_name": account_name,
        "currency": "AED",
        "balance_after": balance,
    }


def _extract_metadata_from_text(text: str) -> dict:
    meta = {}
    # Opening balance
    ob = re.search(r"opening\s+balance[:\s]+([\d,]+\.\d{2})", text, re.IGNORECASE)
    if ob:
        meta["opening_balance"] = float(ob.group(1).replace(",", ""))
    # Closing balance
    cb = re.search(r"closing\s+balance[:\s]+([\d,]+\.\d{2})", text, re.IGNORECASE)
    if not cb:
        cb = re.search(r"closing\s+balance\s*\n[^0-9]*([\d,]+\.\d{2})", text, re.IGNORECASE)
    if cb:
        meta["closing_balance"] = float(cb.group(1).replace(",", ""))
    return meta


def extract_with_pdfplumber(pdf_path: str, account_name: str) -> dict:
    try:
        transactions, metadata, pages_with_tables = _extract_tables(pdf_path, account_name)

        with pdfplumber.open(pdf_path) as pdf:
            full_text = "\n".join(
                page.extract_text() or "" for page in pdf.pages
            )

        metadata.update(_extract_metadata_from_text(full_text))

        # Confidence heuristic
        if not transactions:
            confidence = 0.0
        elif pages_with_tables == 0:
            confidence = 0.3
        else:
            # Higher confidence when dates and amounts were clearly parsed
            valid = sum(
                1 for t in transactions
                if t.get("date") and t.get("amount") and t.get("description")
            )
            confidence = min(0.95, valid / len(transactions)) if transactions else 0.0

        return {
            "transactions": transactions,
            "metadata": metadata,
            "confidence": confidence,
            "method": "pdfplumber",
        }

    except Exception as exc:
        logger.exception("pdfplumber extraction failed: %s", exc)
        return {"transactions": [], "metadata": {}, "confidence": 0.0, "method": "pdfplumber"}
