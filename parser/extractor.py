"""
pdfplumber-based deterministic PDF extraction.
Returns a dict with keys: transactions, metadata, confidence, method.

Two strategies:
1. Table extraction  — works when the PDF has drawn table lines or clear column structure.
2. Text-layout extraction — fallback that groups words by x/y position; handles statements
   with borderless tables and multi-line description cells (e.g. FAB, WIO).

Whichever strategy yields more transactions is used.

Statement profiles
──────────────────
bank_account : standard debit/credit columns; minimal description cleaning.
credit_card  : strips posting-date prefix, trailing "AED [amount]" suffix,
               (cid:N) font artifacts, and footer/rewards contamination.
               Column positions are carried forward across pages so type
               (debit vs credit) stays consistent across a multi-page statement.
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
# Matches an amount that is immediately followed by "CR" (credit indicator used by ENBD CC)
_CR_AMOUNT_RE = re.compile(r"([\d,]+\.\d{2})CR\b", re.IGNORECASE)
# Arabic and Arabic Presentation Forms scripts (appear in bilingual ENBD/FAB PDFs)
_ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]+")
COMBINED_DATE_RE = re.compile("|".join(DATE_PATTERNS))
# Matches exactly "DD MON YYYY" (3-word date with 3-letter month)
DATE_3W_RE = re.compile(r"^\d{2}\s+[A-Za-z]{3}\s+\d{4}$")

# ─── Description-cleaning regexes ─────────────────────────────────────────────

# Universal: remove (cid:N) font-encoding artifacts
_CID_RE = re.compile(r"\(cid:\d+\)")

# Universal: truncate at statement footer / rewards section
_FOOTER_RE = re.compile(
    r"(?:See\s+reverse\s+side"
    r"|(?:FAB\s+Al.Futtaim|Al.Futtaim\s+FAB)\s+Rewards"
    r"|Rewards\s+(?:Balance|Expiring)"
    r"|Total\s+\d+\s+FAB"
    r"|Emirates\s+NBD\s+Bank"             # ENBD footer (English half of bilingual note)
    r"|\blicensed\s+by\s+the\s+Central\s+Bank\b)",  # ENBD footer continuation
    re.IGNORECASE,
)

# Credit-card: posting date embedded at start of description (DD-MM-YYYY or DD/MM/YYYY)
_CC_DATE_PREFIX_RE = re.compile(r"^\d{2}[-/]\d{2}[-/]\d{4}\s+")

# Credit-card: trailing currency code (AED/USD/EUR/…) optionally followed by the
# original amount, printed inside the description column for foreign transactions.
_CC_CCY_SUFFIX_RE = re.compile(
    r"\s+(?:AED|USD|EUR|GBP|CAD|INR|SAR|QAR|KWD|BHD|OMR|SGD|AUD|CHF|JPY)"
    r"(?:\s+[\d,]+(?:\.\d{1,2})?)?\s*$",
    re.IGNORECASE,
)


# ─── Statement-type detection ──────────────────────────────────────────────────

_CC_TEXT_SIGNALS = ("credit card statement", "card statement", "credit card account")
_CC_NAME_SIGNALS = ("credit card", " cc", "cc ", "credit")


def _detect_statement_type(full_text: str, account_name: str) -> str:
    """Return 'credit_card' or 'bank_account' based on PDF content and account name."""
    tl = full_text.lower()
    nl = account_name.lower()
    if any(s in tl for s in _CC_TEXT_SIGNALS):
        return "credit_card"
    if any(s in nl for s in _CC_NAME_SIGNALS):
        return "credit_card"
    return "bank_account"


# ─── Description cleaner ──────────────────────────────────────────────────────

def _clean_description(desc: str, statement_type: str) -> str:
    if not desc:
        return desc
    # Universal: strip (cid:N) artifacts
    desc = _CID_RE.sub("", desc)
    # Universal: strip Arabic script (bilingual ENBD / FAB PDFs embed RTL footer text)
    desc = _ARABIC_RE.sub("", desc)
    # Universal: truncate at footer / rewards block
    m = _FOOTER_RE.search(desc)
    if m:
        desc = desc[: m.start()]
    # Credit-card specific
    if statement_type == "credit_card":
        desc = _CC_DATE_PREFIX_RE.sub("", desc)
        desc = _CC_CCY_SUFFIX_RE.sub("", desc)
    # Collapse any extra whitespace left by removals
    desc = re.sub(r" {2,}", " ", desc)
    return desc.strip()


def _parse_amount(raw: str) -> float:
    return float(raw.replace(",", ""))


def _parse_date(raw: str) -> Optional[str]:
    try:
        dt = dateutil_parser.parse(raw, dayfirst=True)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


# ─── Strategy 1: table extraction ─────────────────────────────────────────────

def _extract_tables(
    pdf_path: str, account_name: str, password: str = "", statement_type: str = "bank_account"
) -> tuple:
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

                def _norm_header(row) -> list:
                    """Lowercase header cell; replace Arabic column labels with English."""
                    result = []
                    for c in row:
                        h = str(c).lower().strip() if c else ""
                        for arabic, english in _ARABIC_COL_NORM.items():
                            if arabic in h:
                                h = h.replace(arabic, english + " ")
                        result.append(h)
                    return result

                header = _norm_header(table[0])

                # If the first row has no recognisable column keywords, it's likely
                # the Arabic-only half of a bilingual header — try the next row instead.
                _EN_KEYWORDS = {"date", "debit", "credit", "balance", "detail", "desc"}
                if not any(any(k in h for k in _EN_KEYWORDS) for h in header) and len(table) > 2:
                    header = _norm_header(table[1])
                    data_rows = table[2:]
                else:
                    data_rows = table[1:]

                for row in data_rows:
                    if not row or all(c is None or str(c).strip() == "" for c in row):
                        continue
                    txn = _map_row_to_transaction(header, row, account_name, statement_type)
                    if txn:
                        transactions.append(txn)

    return transactions, metadata, pages_with_tables


def _map_row_to_transaction(
    header: list, row: list, account_name: str, statement_type: str = "bank_account"
) -> Optional[dict]:
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
        elif any(k in col for k in ("credit", "deposit", "cr")):
            m = AMOUNT_PATTERN.search(cell)
            if m:
                credit_amount = _parse_amount(m.group())
        elif "amount" in col:
            # Single amount column (e.g. ENBD CC): "CR" suffix marks credits/refunds
            m = AMOUNT_PATTERN.search(cell)
            if m:
                if _CR_AMOUNT_RE.search(cell):
                    credit_amount = _parse_amount(m.group())
                else:
                    debit_amount = _parse_amount(m.group())
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

    description = _clean_description(description or "", statement_type)
    if not description:
        return None

    txn_type = "debit" if debit_amount is not None else "credit"
    amount = debit_amount if debit_amount is not None else credit_amount

    # Sanity: balance_after shouldn't equal the transaction amount (CC layout artifact)
    if balance is not None and abs(balance - amount) < 0.01:
        balance = None

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
    # Arabic column labels (bilingual ENBD / Emirates PDFs)
    "التاريخ": "date",
    "التفاصيل": "description",
    "مدين": "debit",
    "دائن": "credit",
    "الرصيد": "balance",
}
_HEADER_WORDS = set(_COL_ALIASES.keys())

# Mapping used to normalise Arabic header cells to English before column detection
_ARABIC_COL_NORM: dict = {
    "التاريخ": "date",
    "التفاصيل": "details",
    "مدين": "debit",
    "دائن": "credit",
    "الرصيد": "balance",
}


def _find_col_positions(lines: list) -> dict:
    """
    Return column-name → x0 by locating the header row.

    Fast path: a single line contains both 'debit' and 'credit'.
    Fallback:  bilingual / multi-row merged-cell headers (e.g. FAB CC) split
               'Debit' and 'Credit' across two y-buckets.  Scan all lines for
               each word individually; if both are found and credit_x > debit_x
               (right-to-left makes no sense for columns) return combined positions.
    """
    _DEBIT_WORDS  = {"debit", "withdrawal", "مدين"}
    _CREDIT_WORDS = {"credit", "deposit", "دائن"}

    # Fast path — single line with both keywords
    for line in lines:
        texts = [w["text"].lower() for w in line]
        has_debit  = any(t in _DEBIT_WORDS  for t in texts)
        has_credit = any(t in _CREDIT_WORDS for t in texts)
        if has_debit and has_credit:
            cols: dict = {}
            for w in line:
                canonical = _COL_ALIASES.get(w["text"].lower())
                if canonical:
                    cols.setdefault(canonical, w["x0"])
            return cols

    # Fallback — keywords split across lines (multi-row merged-cell header)
    debit_w = credit_w = None
    for line in lines:
        for w in line:
            wl = w["text"].lower()
            if wl in _DEBIT_WORDS and debit_w is None:
                debit_w = w
            elif wl in _CREDIT_WORDS and credit_w is None:
                credit_w = w

    if debit_w and credit_w and credit_w["x0"] > debit_w["x0"]:
        cols = {"debit": debit_w["x0"], "credit": credit_w["x0"]}
        for line in lines:
            for w in line:
                canonical = _COL_ALIASES.get(w["text"].lower())
                if canonical and canonical not in cols:
                    cols[canonical] = w["x0"]
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

# These phrases mark the start of a page footer / rewards section.
# When detected, the current transaction is finalised and collection stops —
# this prevents footer numbers (e.g. rewards totals) from being absorbed as
# transaction amounts.
_PAGE_FOOTER_PHRASES = {
    "rewards balance",
    "rewards expiring",
    "al-futtaim fab rewards",
    "futtaim fab rewards",
    "for more information on your fab",
    "see reverse side",
    "important information",
    "statement summary",   # ENBD: summary table at page bottom
    "upoints summary",     # ENBD: loyalty-points section
    "emirates nbd bank",   # ENBD: bilingual legal footer
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


def _extract_text_based(
    pdf_path: str, account_name: str, password: str = "", statement_type: str = "bank_account"
) -> tuple:
    """
    Word-position extraction for borderless-table bank statements.
    Groups display lines into transaction rows by detecting dates at the left margin,
    then separates description text from financial amounts by column x-position.

    Column positions detected on each page are carried forward to subsequent pages
    so that multi-page statements with headers only on page 1 stay consistent.
    """
    transactions: list = []
    metadata: dict = {}
    # Carry the last-known column layout across pages (critical for multi-page CC statements)
    last_known_col_pos: dict = {}

    with pdfplumber.open(pdf_path, password=password) as pdf:
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=3, y_tolerance=3)
            if not words:
                continue

            lines = _words_to_lines(words)

            # Update column layout only when the current page has a header row
            page_col_pos = _find_col_positions(lines)
            if page_col_pos:
                last_known_col_pos = page_col_pos
            col_pos = last_known_col_pos

            page_width = page.width

            # Amount columns start at the debit column (fall back to 65% of page)
            amount_x_min = col_pos.get("debit", page_width * 0.65)
            # Transaction dates live within the leftmost 20% of the page
            # (generous to handle wider left-margin layouts like ENBD savings)
            date_x_max = page_width * 0.20

            page_text = " ".join(w["text"] for line in lines for w in line)
            metadata.update(_extract_metadata_from_text(page_text))

            rows: list = []
            current = None

            for line in lines:
                if not line:
                    continue

                line_text_lower = " ".join(w["text"] for w in line).lower()

                # Footer section boundary — commit current transaction and stop collecting.
                # This prevents footer numbers (rewards totals, etc.) from being absorbed
                # as amount words for the last transaction on the page.
                if any(p in line_text_lower for p in _PAGE_FOOTER_PHRASES):
                    if current:
                        rows.append(current)
                        current = None
                    continue

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

            logger.info(
                "Page text-layout: detected %d transaction rows (col_pos=%s)",
                len(rows), col_pos,
            )

            for row in rows:
                txn = _parse_text_row(row, account_name, col_pos, amount_x_min, statement_type)
                if txn:
                    transactions.append(txn)

    return transactions, metadata


def _parse_text_row(
    row: dict,
    account_name: str,
    col_pos: dict,
    amount_x_min: float,
    statement_type: str = "bank_account",
) -> Optional[dict]:
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

    description = _clean_description(" ".join(desc_parts).strip(), statement_type)
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
        # No column info: position-based heuristic.
        # CC statements: all purchases are debits (they increase the balance owed).
        # "CR" suffix on an amount marks a credit/refund (ENBD CC convention).
        # Bank accounts: single-amount lines are assumed credits (default historical behaviour).
        debit = credit = balance = None
        vals: list[float] = []
        cr_flags: list[bool] = []
        for w in amount_words:
            text = w["text"].strip()
            is_cr = bool(_CR_AMOUNT_RE.search(text))
            # Strip "CR" suffix before float conversion so it doesn't raise ValueError
            clean = _CR_AMOUNT_RE.sub(r"\1", text) if is_cr else text
            try:
                vals.append(float(clean.replace(",", "")))
                cr_flags.append(is_cr)
            except ValueError:
                pass
        cc = statement_type == "credit_card"
        if len(vals) == 1:
            if cc:
                if cr_flags[0]:
                    credit = vals[0]   # e.g. "124.75CR" = refund / payment received
                else:
                    debit = vals[0]    # regular purchase
            else:
                credit = vals[0]
        elif len(vals) >= 2:
            if cc:
                # Rightmost amount = AED figure; preceding amounts are original-currency.
                if cr_flags[-1]:
                    credit = vals[-1]
                else:
                    debit = vals[-1]
            else:
                balance = vals[-1]
                credit = vals[-2]

    if debit is None and credit is None:
        return None

    txn_type = "debit" if debit is not None else "credit"
    amount = debit if debit is not None else credit

    # Sanity: balance_after shouldn't equal the transaction amount (CC layout artifact)
    if balance is not None and abs(balance - amount) < 0.01:
        balance = None

    return {
        "date": date_val,
        "description": description,
        "amount": round(amount, 2),
        "type": txn_type,
        "account_name": account_name,
        "currency": "AED",
        "balance_after": balance,
    }


# ─── Strategy 3: OCR extraction ───────────────────────────────────────────────

_OCR_COL_NAMES = ["date", "description", "debit", "credit", "balance"]


def _detect_ocr_col_positions(pdf_path: str, password: str = "") -> list:
    """
    Detect column x-boundaries from thin vertical rules in the PDF.
    Falls back to known ENBD savings account column x-positions (PDF pts) on failure.
    """
    _ENBD_DEFAULTS = [17.0, 82.0, 319.0, 405.0, 494.0, 595.0]
    try:
        with pdfplumber.open(pdf_path, password=password) as pdf:
            for page in pdf.pages[:4]:
                rects = page.rects or []
                ph = page.height
                xs = set()
                for r in rects:
                    width = r["x1"] - r["x0"]
                    height = r["bottom"] - r["top"]
                    if width < 3 and height > ph * 0.25:
                        xs.add(round(r["x0"], 1))
                if len(xs) >= 4:
                    return sorted(xs)
    except Exception as exc:
        logger.debug("OCR column detection from rects failed: %s", exc)
    return _ENBD_DEFAULTS


def _extract_with_ocr(
    pdf_path: str,
    account_name: str,
    password: str = "",
    statement_type: str = "bank_account",
) -> tuple:
    """
    Strategy 3: Render pages with PyMuPDF at 300 DPI and OCR each column via
    pytesseract.  Used as last-resort fallback when transaction text is encoded
    as Bezier vector paths (e.g. ENBD savings account statements).
    """
    try:
        import fitz  # PyMuPDF
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        logger.warning("OCR strategy unavailable — missing dependency: %s", exc)
        return [], {}

    col_xs = _detect_ocr_col_positions(pdf_path, password)
    if len(col_xs) < 3:
        return [], {}

    num_cols = min(len(_OCR_COL_NAMES), len(col_xs) - 1)
    col_defs = [(_OCR_COL_NAMES[i], col_xs[i], col_xs[i + 1]) for i in range(num_cols)]

    date_col  = next((c for c in col_defs if c[0] == "date"),        None)
    desc_col  = next((c for c in col_defs if c[0] == "description"), None)
    debit_col = next((c for c in col_defs if c[0] == "debit"),       None)
    cred_col  = next((c for c in col_defs if c[0] == "credit"),      None)

    if date_col is None:
        return [], {}

    DPI = 300
    SCALE = DPI / 72.0

    transactions: list = []
    metadata: dict = {}

    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        logger.warning("OCR: PyMuPDF failed to open PDF: %s", exc)
        return [], {}

    try:
        if password:
            rc = doc.authenticate(password)
            if rc == 0:
                logger.warning("OCR: PDF authentication failed")
                return [], {}

        for page_num in range(len(doc)):
            page = doc[page_num]
            ph_pts = page.rect.height

            # Scan 10-90% of page height; date-pattern filtering handles header/footer
            y_top_px    = int(ph_pts * 0.10 * SCALE)
            y_bottom_px = int(ph_pts * 0.90 * SCALE)

            mat = fitz.Matrix(SCALE, SCALE)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

            # Step 1 — OCR date column to discover per-transaction y-positions
            dx0 = int(date_col[1] * SCALE)
            dx1 = int(date_col[2] * SCALE)
            date_crop = img.crop((dx0, y_top_px, dx1, y_bottom_px))

            try:
                date_data = pytesseract.image_to_data(
                    date_crop,
                    config="--psm 4 -c tessedit_char_whitelist=0123456789/",
                    output_type=pytesseract.Output.DICT,
                )
            except Exception as exc:
                logger.warning("OCR page %d date column failed: %s", page_num + 1, exc)
                continue

            txn_rows: list = []
            for i, word in enumerate(date_data["text"]):
                word = (word or "").strip()
                if not word or int(date_data["conf"][i]) < 20:
                    continue
                m = COMBINED_DATE_RE.search(word)
                if not m:
                    continue
                date_val = _parse_date(m.group())
                if not date_val:
                    continue
                # top is relative to date_crop; convert back to full-image coords
                top_px = int(date_data["top"][i]) + y_top_px
                ht_px  = max(int(date_data["height"][i]), 15)
                txn_rows.append({
                    "date":    date_val,
                    "y_top":   top_px - 3,
                    "y_bottom": top_px + ht_px + 3,
                })

            if not txn_rows:
                continue

            # Expand each row's y_bottom to the next row's y_top
            for i in range(len(txn_rows) - 1):
                txn_rows[i]["y_bottom"] = txn_rows[i + 1]["y_top"] - 1
            txn_rows[-1]["y_bottom"] = y_bottom_px

            # Step 2 — OCR description and amount columns in each transaction's y-band
            page_count = 0
            for row_info in txn_rows:
                yt = max(row_info["y_top"],    0)
                yb = min(row_info["y_bottom"], img.height)
                if yb - yt < 4:
                    continue

                description = ""
                if desc_col:
                    crop = img.crop((int(desc_col[1] * SCALE), yt, int(desc_col[2] * SCALE), yb))
                    try:
                        description = pytesseract.image_to_string(crop, config="--psm 6").strip()
                    except Exception:
                        pass

                debit: Optional[float] = None
                if debit_col:
                    crop = img.crop((int(debit_col[1] * SCALE), yt, int(debit_col[2] * SCALE), yb))
                    try:
                        txt = pytesseract.image_to_string(
                            crop, config="--psm 6 -c tessedit_char_whitelist=0123456789,."
                        ).strip()
                        m_amt = AMOUNT_PATTERN.search(txt)
                        if m_amt:
                            debit = _parse_amount(m_amt.group())
                    except Exception:
                        pass

                credit: Optional[float] = None
                if cred_col:
                    crop = img.crop((int(cred_col[1] * SCALE), yt, int(cred_col[2] * SCALE), yb))
                    try:
                        txt = pytesseract.image_to_string(
                            crop, config="--psm 6 -c tessedit_char_whitelist=0123456789,."
                        ).strip()
                        m_amt = AMOUNT_PATTERN.search(txt)
                        if m_amt:
                            credit = _parse_amount(m_amt.group())
                    except Exception:
                        pass

                if debit is None and credit is None:
                    continue

                # Keep only the first non-empty OCR line; subsequent lines are often
                # Arabic barcode annotations that survive _clean_description.
                first_line = next((l.strip() for l in description.splitlines() if l.strip()), description)
                description = _clean_description(first_line, statement_type)
                if not description:
                    description = f"Transaction on {row_info['date']}"

                txn_type = "debit" if debit is not None else "credit"
                amount   = debit  if debit  is not None else credit

                transactions.append({
                    "date":         row_info["date"],
                    "description":  description,
                    "amount":       round(amount, 2),
                    "type":         txn_type,
                    "account_name": account_name,
                    "currency":     "AED",
                    "balance_after": None,
                })
                page_count += 1

            logger.info("OCR page %d: extracted %d transactions", page_num + 1, page_count)

    finally:
        doc.close()

    logger.info("OCR strategy total: %d transactions", len(transactions))
    return transactions, metadata


# ─── Metadata extraction ──────────────────────────────────────────────────────

def _extract_metadata_from_text(text: str) -> dict:
    meta = {}

    # ── Opening / previous balance ────────────────────────────────────────────
    ob = re.search(
        r"(?:opening|previous|prior)\s+(?:statement\s+)?balance[:\s]+([\d,]+\.\d{2})",
        text, re.IGNORECASE,
    )
    if ob:
        meta["opening_balance"] = float(ob.group(1).replace(",", ""))

    # ── Closing / new / current balance ──────────────────────────────────────
    cb = re.search(
        r"(?:closing|new|current)\s+(?:statement\s+)?balance[:\s]+([\d,]+\.\d{2})",
        text, re.IGNORECASE,
    )
    if not cb:
        cb = re.search(r"closing\s+balance\s*\n[^0-9]*([\d,]+\.\d{2})", text, re.IGNORECASE)
    # Credit card: "Total Amount Due" or "Statement Balance" as closing figure
    if not cb:
        cb = re.search(
            r"(?:total\s+amount\s+due|statement\s+balance)[:\s]+([\d,]+\.\d{2})",
            text, re.IGNORECASE,
        )
    if cb:
        meta["closing_balance"] = float(cb.group(1).replace(",", ""))

    return meta


# ─── Main entry point ─────────────────────────────────────────────────────────

def extract_with_pdfplumber(pdf_path: str, account_name: str, password: str = "") -> dict:
    try:
        with pdfplumber.open(pdf_path, password=password) as pdf:
            full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

        statement_type = _detect_statement_type(full_text, account_name)
        logger.info("Detected statement type: %s for account: %s", statement_type, account_name)

        table_txns, table_meta, pages_with_tables = _extract_tables(
            pdf_path, account_name, password, statement_type
        )

        table_meta.update(_extract_metadata_from_text(full_text))

        if not table_txns:
            table_conf = 0.0
        elif pages_with_tables == 0:
            table_conf = 0.3
        else:
            valid = sum(1 for t in table_txns if t.get("date") and t.get("amount") and t.get("description"))
            table_conf = min(0.95, valid / len(table_txns))

        text_txns, text_meta = _extract_text_based(
            pdf_path, account_name, password, statement_type
        )

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

        # Strategy 3: OCR fallback for PDFs whose transaction text is vector paths
        if not transactions:
            logger.info("Strategies 1 & 2 yielded 0 — trying OCR fallback")
            ocr_txns, ocr_meta = _extract_with_ocr(
                pdf_path, account_name, password, statement_type
            )
            if ocr_txns:
                transactions = ocr_txns
                metadata = {**table_meta, **text_meta, **ocr_meta}
                method = "ocr"

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
            "statement_type": statement_type,
        }

    except PDFPasswordIncorrect:
        error = "password_incorrect" if password else "password_required"
        logger.warning("PDF password error (%s) for %s", error, account_name)
        return {"transactions": [], "metadata": {}, "confidence": 0.0, "method": "pdfplumber", "error": error}

    except Exception as exc:
        logger.exception("pdfplumber extraction failed: %s", exc)
        return {"transactions": [], "metadata": {}, "confidence": 0.0, "method": "pdfplumber"}
