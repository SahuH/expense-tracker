"""
pdfplumber-based deterministic PDF extraction.
Returns a dict with keys: transactions, metadata, confidence, method.

Two strategies:
1. Table extraction  — works when the PDF has drawn table lines or clear column structure.
2. Text-layout extraction — fallback that groups words by x/y position; handles statements
   with borderless tables and multi-line description cells (e.g. FAB, WIO).

Whichever strategy yields more transactions is used.
"""
import re
import logging
from typing import Optional
import pdfplumber
from dateutil import parser as dateutil_parser
from pdfminer.pdfdocument import PDFPasswordIncorrect

logger = logging.getLogger(__name__)

DATE_PATTERNS = [
    r"\d{2}/\d{2}/\d{4}",
    r"\d{2}-\d{2}-\d{4}",
    r"\d{2}\s+\w{3}\s+\d{4}",
    r"\d{4}-\d{2}-\d{2}",
]
AMOUNT_PATTERN = re.compile(r"[\d,]+\.\d{2}")
COMBINED_DATE_RE = re.compile("|".join(DATE_PATTERNS))
# Matches exactly "DD MON YYYY" (3-word date with 3-letter month)
DATE_3W_RE = re.compile(r"^\d{2}\s+[A-Za-z]{3}\s+\d{4}$")


def _parse_amount(raw: str) -> float:
    return float(raw.replace(",", ""))


def _parse_date(raw: str) -> Optional[str]:
    try:
        dt = dateutil_parser.parse(raw, dayfirst=True)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


# ─── Strategy 1: table extraction ─────────────────────────────────────────────

def _extract_tables(pdf_path: str, account_name: str, password: str = "") -> tuple:
    transactions = []
    metadata = {}
    pages_with_tables = 0

    with pdfplumber.open(pdf_path, password=password) as pdf:
        for page in pdf.pages:
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
    row_str = [str(c).strip() if c is not None else "" for c in row]
    date_val = description = None
    debit_amount = credit_amount = balance = None

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

    if date_val is None:
        for cell in row_str:
            if COMBINED_DATE_RE.search(cell):
                date_val = _parse_date(COMBINED_DATE_RE.search(cell).group())
                break

    if not description:
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


# ─── Strategy 2: text-layout extraction ───────────────────────────────────────

def _words_to_lines(words: list, y_tol: float = 3.0) -> list:
    """Group word dicts into display lines, sorted top-to-bottom then left-to-right."""
    if not words:
        return []
    buckets: dict = {}
    for w in words:
        key = round(w["top"] / y_tol)
        buckets.setdefault(key, []).append(w)
    return [sorted(ws, key=lambda w: w["x0"]) for _, ws in sorted(buckets.items())]


_COL_ALIASES = {
    "date": "date",
    "debit": "debit", "withdrawal": "debit", "dr": "debit",
    "credit": "credit", "deposit": "credit", "cr": "credit",
    "balance": "balance",
    "description": "description", "narration": "description", "particulars": "description",
}
_HEADER_WORDS = set(_COL_ALIASES.keys())


def _find_col_positions(lines: list) -> dict:
    """Return column-name → x0 by locating the header row (must contain debit + credit)."""
    for line in lines:
        texts = [w["text"].lower() for w in line]
        has_debit = any(t in ("debit", "withdrawal") for t in texts)
        has_credit = any(t in ("credit", "deposit") for t in texts)
        if has_debit and has_credit:
            cols: dict = {}
            for w in line:
                canonical = _COL_ALIASES.get(w["text"].lower())
                if canonical:
                    cols.setdefault(canonical, w["x0"])
            return cols
    return {}


def _assign_col(x: float, debit_x, credit_x, balance_x) -> Optional[str]:
    """Map an amount word's x-position to debit / credit / balance using column left-edges."""
    if debit_x is None or credit_x is None:
        return None
    if balance_x is not None and x >= balance_x:
        return "balance"
    if x >= credit_x:
        return "credit"
    if x >= debit_x:
        return "debit"
    return None


_SKIP_PHRASES = {
    "opening balance", "closing balance", "closing statement",
    "statement balance", "brought forward", "carried forward",
}


def _col_amount(words: list, x_left: float, x_right: float = None) -> Optional[float]:
    """
    Extract a financial amount from words within a column's x-range.

    Concatenates all tokens in the range (sorted left-to-right), then applies
    AMOUNT_PATTERN (r"[\d,]+\\.\\d{2}") to find valid amounts.  Handles every
    split that pdfplumber produces:
      - comma-split:   "34,"  + "975.00" → "34,975.00"  → 34975.0
      - decimal-split: "34."  + "95"     → "34.95"      → 34.95
      - no split:      "34,975.00"       → 34975.0

    Stray non-numeric tokens (description text bleeding into the column) are
    harmless — AMOUNT_PATTERN skips them automatically.  The rightmost match is
    returned because amounts are right-aligned; any description leaks appear at
    smaller x positions and match earlier in the string.
    """
    in_col = sorted(
        [w for w in words if w["x0"] >= x_left and (x_right is None or w["x0"] < x_right)],
        key=lambda w: w["x0"],
    )
    if not in_col:
        return None
    combined = "".join(w["text"] for w in in_col)
    matches = AMOUNT_PATTERN.findall(combined)   # r"[\d,]+\.\d{2}"
    if not matches:
        return None
    return _parse_amount(matches[-1])            # rightmost = actual right-aligned amount


def _extract_text_based(pdf_path: str, account_name: str, password: str = "") -> tuple:
    """
    Word-position extraction for borderless-table bank statements.
    Groups display lines into transaction rows by detecting dates at the left margin,
    then separates description text from financial amounts by column x-position.
    """
    transactions: list = []
    metadata: dict = {}

    with pdfplumber.open(pdf_path, password=password) as pdf:
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=3, y_tolerance=3)
            if not words:
                continue

            lines = _words_to_lines(words)
            col_pos = _find_col_positions(lines)
            page_width = page.width

            # Amount columns start at the debit column (fall back to 65% of page)
            amount_x_min = col_pos.get("debit", page_width * 0.65)
            # Transaction dates live within the leftmost 15% of the page
            date_x_max = page_width * 0.15

            page_text = " ".join(w["text"] for line in lines for w in line)
            metadata.update(_extract_metadata_from_text(page_text))

            rows: list = []
            current = None

            for line in lines:
                if not line:
                    continue

                line_text_lower = " ".join(w["text"] for w in line).lower()

                # Skip summary and header lines
                if any(p in line_text_lower for p in _SKIP_PHRASES):
                    continue
                if sum(1 for w in line if w["text"].lower() in _HEADER_WORDS) >= 2:
                    continue

                # Detect transaction start: a date at the left margin
                first_x = line[0]["x0"]
                date_val = None
                date_end_idx = 0

                if first_x <= date_x_max:
                    first_3 = " ".join(w["text"] for w in line[:3])
                    if DATE_3W_RE.match(first_3):
                        date_val = _parse_date(first_3)
                        date_end_idx = 3
                    elif COMBINED_DATE_RE.match(line[0]["text"]):
                        date_val = _parse_date(line[0]["text"])
                        date_end_idx = 1

                if date_val:
                    if current:
                        rows.append(current)
                    current = {"date": date_val, "date_end_idx": date_end_idx, "lines": [line]}
                elif current is not None:
                    current["lines"].append(line)

            if current:
                rows.append(current)

            logger.info("Page text-layout: detected %d transaction rows (col_pos=%s)", len(rows), col_pos)

            for row in rows:
                txn = _parse_text_row(row, account_name, col_pos, amount_x_min)
                if txn:
                    transactions.append(txn)

    return transactions, metadata


def _parse_text_row(row: dict, account_name: str, col_pos: dict, amount_x_min: float) -> Optional[dict]:
    first_line = row["lines"][0]
    date_val = row["date"]
    idx = row.get("date_end_idx", 3)

    # Skip a second (value) date if present immediately after the transaction date
    if idx + 3 <= len(first_line):
        cand = " ".join(w["text"] for w in first_line[idx: idx + 3])
        if DATE_3W_RE.match(cand):
            idx += 3
    elif idx < len(first_line) and COMBINED_DATE_RE.match(first_line[idx]["text"]):
        idx += 1

    # Split words into description (left) and amounts (right) by x-position
    desc_parts = [w["text"] for w in first_line[idx:] if w["x0"] < amount_x_min]
    for cont in row["lines"][1:]:
        desc_parts.extend(w["text"] for w in cont if w["x0"] < amount_x_min)

    amount_words = sorted(
        [w for line in row["lines"] for w in line if w["x0"] >= amount_x_min],
        key=lambda w: w["x0"],
    )

    description = " ".join(desc_parts).strip()
    if not description:
        return None

    debit_x = col_pos.get("debit")
    credit_x = col_pos.get("credit")
    balance_x = col_pos.get("balance")

    logger.debug(
        "Row date=%s amount_words=%s col_pos=%s",
        row["date"],
        [(w["text"], round(w["x0"], 1)) for w in amount_words],
        {k: round(v, 1) for k, v in col_pos.items()},
    )

    if debit_x is not None and credit_x is not None:
        # Column-range approach: collect all tokens in each column's x-range and join them.
        # This correctly handles numbers that pdfplumber split at a comma or decimal point.
        debit = _col_amount(amount_words, debit_x, credit_x)
        credit = _col_amount(amount_words, credit_x, balance_x)
        balance = _col_amount(amount_words, balance_x) if balance_x is not None else None
    else:
        # No column info: last amount = balance, penultimate = transaction (default credit)
        debit = credit = balance = None
        vals = []
        for w in amount_words:
            try:
                vals.append(float(w["text"].replace(",", "")))
            except ValueError:
                pass
        if len(vals) == 1:
            credit = vals[0]
        elif len(vals) >= 2:
            balance = vals[-1]
            credit = vals[-2]

    if debit is None and credit is None:
        return None

    txn_type = "debit" if debit is not None else "credit"
    amount = debit if debit is not None else credit

    return {
        "date": date_val,
        "description": description,
        "amount": round(amount, 2),
        "type": txn_type,
        "account_name": account_name,
        "currency": "AED",
        "balance_after": balance,
    }


# ─── Metadata extraction ──────────────────────────────────────────────────────

def _extract_metadata_from_text(text: str) -> dict:
    meta = {}
    ob = re.search(r"opening\s+balance[:\s]+([\d,]+\.\d{2})", text, re.IGNORECASE)
    if ob:
        meta["opening_balance"] = float(ob.group(1).replace(",", ""))
    cb = re.search(
        r"closing\s+(?:statement\s+)?balance[:\s]+([\d,]+\.\d{2})",
        text, re.IGNORECASE,
    )
    if not cb:
        cb = re.search(r"closing\s+balance\s*\n[^0-9]*([\d,]+\.\d{2})", text, re.IGNORECASE)
    if cb:
        meta["closing_balance"] = float(cb.group(1).replace(",", ""))
    return meta


# ─── Main entry point ─────────────────────────────────────────────────────────

def extract_with_pdfplumber(pdf_path: str, account_name: str, password: str = "") -> dict:
    try:
        table_txns, table_meta, pages_with_tables = _extract_tables(pdf_path, account_name, password)

        with pdfplumber.open(pdf_path, password=password) as pdf:
            full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

        table_meta.update(_extract_metadata_from_text(full_text))

        if not table_txns:
            table_conf = 0.0
        elif pages_with_tables == 0:
            table_conf = 0.3
        else:
            valid = sum(1 for t in table_txns if t.get("date") and t.get("amount") and t.get("description"))
            table_conf = min(0.95, valid / len(table_txns))

        text_txns, text_meta = _extract_text_based(pdf_path, account_name, password)

        logger.info(
            "Extraction results — table: %d txns (conf=%.2f), text-layout: %d txns",
            len(table_txns), table_conf, len(text_txns),
        )

        if len(text_txns) > len(table_txns):
            transactions = text_txns
            metadata = {**table_meta, **text_meta}
            method = "text_layout"
        else:
            transactions = table_txns
            metadata = table_meta
            method = "pdfplumber"

        if not transactions:
            confidence = 0.0
        else:
            valid = sum(1 for t in transactions if t.get("date") and t.get("amount") and t.get("description"))
            confidence = min(0.95, valid / len(transactions))

        return {
            "transactions": transactions,
            "metadata": metadata,
            "confidence": confidence,
            "method": method,
        }

    except PDFPasswordIncorrect:
        error = "password_incorrect" if password else "password_required"
        logger.warning("PDF password error (%s) for %s", error, account_name)
        return {"transactions": [], "metadata": {}, "confidence": 0.0, "method": "pdfplumber", "error": error}

    except Exception as exc:
        logger.exception("pdfplumber extraction failed: %s", exc)
        return {"transactions": [], "metadata": {}, "confidence": 0.0, "method": "pdfplumber"}
