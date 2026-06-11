import streamlit as st
import requests
import pandas as pd
import io
import os
from urllib.parse import quote

from auth import require_login
from firefly_api import get_accounts

st.set_page_config(page_title="Upload & Import — Household Finance", page_icon="💰", layout="wide")

_, _, authentication_status = require_login()
if not authentication_status:
    st.stop()

PARSER_URL = os.getenv("PARSER_URL", "http://parser:8000")

st.title("Upload & Import")

# ── Account selector ──────────────────────────────────────────────────────────
st.subheader("1. Select account")
try:
    raw_accounts = get_accounts()
    account_options = {a["attributes"]["name"]: a["id"] for a in raw_accounts}
except Exception as exc:
    st.warning(f"Could not fetch accounts from Firefly: {exc}")
    account_options = {
        "Joint Current Account": "0",
        "Harsh Savings": "1",
        "Wife Savings": "2",
        "Harsh Credit Card": "3",
        "Wife Credit Card": "4",
    }

selected_account = st.selectbox("Account", list(account_options.keys()))

# ── PDF password ───────────────────────────────────────────────────────────────
# Check whether a password has already been saved for this account.
_has_saved_pw = False
try:
    _pw_check = requests.get(
        f"{PARSER_URL}/passwords/{quote(selected_account, safe='')}",
        timeout=5,
    )
    if _pw_check.ok:
        _has_saved_pw = _pw_check.json().get("has_password", False)
except Exception:
    pass  # parser unavailable — treat as no saved password

if _has_saved_pw:
    _col_pw, _col_btn = st.columns([5, 1])
    with _col_pw:
        st.info(f"🔒 PDF password saved for **{selected_account}**")
    with _col_btn:
        if st.button("Remove", key="remove_pw", help="Forget the saved password for this account"):
            try:
                requests.delete(
                    f"{PARSER_URL}/passwords/{quote(selected_account, safe='')}",
                    timeout=5,
                )
                st.rerun()
            except Exception as _e:
                st.warning(f"Could not remove saved password: {_e}")
    pdf_password = st.text_input(
        "Override password (optional)",
        type="password",
        placeholder="Leave blank to use saved password, or enter a new one to replace it",
        key="pdf_password",
    )
else:
    pdf_password = st.text_input(
        "PDF password (if protected)",
        type="password",
        placeholder="Leave blank if not required — entered password will be saved for this account",
        key="pdf_password",
    )

# ── File uploader ─────────────────────────────────────────────────────────────
st.subheader("2. Upload statement")
uploaded_file = st.file_uploader("Choose a PDF or CSV file", type=["pdf", "csv"])


def _parse_csv(file_bytes: bytes, account_name: str) -> dict:
    """
    Read a bank CSV and map it to the standard transaction schema.
    Handles common column name variants from UAE banks (WIO, ENBD, ADCB, FAB, etc.)
    """
    df = pd.read_csv(io.BytesIO(file_bytes))
    df.columns = [c.strip().lower() for c in df.columns]

    col_map = {}

    # Date
    for c in df.columns:
        if any(k in c for k in ("date", "txn date", "value date", "transaction date")):
            col_map["date"] = c
            break

    # Description
    for c in df.columns:
        if any(k in c for k in ("description", "narration", "particular", "details", "merchant", "remarks")):
            col_map["description"] = c
            break

    # Debit / credit as separate columns (most UAE banks)
    for c in df.columns:
        if any(k in c for k in ("debit", "withdrawal", "dr amount", "debit amount")):
            col_map["debit"] = c
            break
    for c in df.columns:
        if any(k in c for k in ("credit", "deposit", "cr amount", "credit amount")):
            col_map["credit"] = c
            break

    # Single amount column (WIO / some banks)
    if "debit" not in col_map and "credit" not in col_map:
        for c in df.columns:
            if c in ("amount", "transaction amount", "txn amount"):
                col_map["amount"] = c
                break

    transactions = []
    for _, row in df.iterrows():
        date_val = str(row.get(col_map.get("date", ""), "")).strip()
        desc = str(row.get(col_map.get("description", ""), "")).strip()

        # Determine amount and type
        if "debit" in col_map or "credit" in col_map:
            debit_raw = row.get(col_map.get("debit", ""), None)
            credit_raw = row.get(col_map.get("credit", ""), None)
            try:
                debit = float(str(debit_raw).replace(",", "")) if pd.notna(debit_raw) and str(debit_raw).strip() not in ("", "-") else 0.0
            except ValueError:
                debit = 0.0
            try:
                credit = float(str(credit_raw).replace(",", "")) if pd.notna(credit_raw) and str(credit_raw).strip() not in ("", "-") else 0.0
            except ValueError:
                credit = 0.0

            if debit > 0:
                amount, txn_type = debit, "debit"
            elif credit > 0:
                amount, txn_type = credit, "credit"
            else:
                continue
        elif "amount" in col_map:
            try:
                raw = float(str(row[col_map["amount"]]).replace(",", ""))
            except ValueError:
                continue
            if raw < 0:
                amount, txn_type = abs(raw), "debit"
            else:
                amount, txn_type = raw, "credit"
        else:
            continue

        if not date_val or not desc or amount == 0:
            continue

        transactions.append({
            "date": date_val,
            "description": desc,
            "amount": round(amount, 2),
            "type": txn_type,
            "account_name": account_name,
            "currency": "AED",
            "balance_after": None,
        })

    return {
        "status": "needs_review",
        "extraction_method": "csv",
        "confidence": 1.0,
        "metadata": {},
        "transactions": transactions,
        "balance_check": {"passed": False, "reason": "CSV import — balance check not applicable"},
    }


if uploaded_file is not None:
    st.info(f"File: {uploaded_file.name} ({uploaded_file.size / 1024:.1f} KB)")
    is_csv = uploaded_file.name.lower().endswith(".csv")

    if st.button("Parse Statement", type="primary"):
        with st.spinner("Extracting transactions..."):
            try:
                if is_csv:
                    result = _parse_csv(uploaded_file.getvalue(), selected_account)
                    st.session_state["parse_result"] = result
                else:
                    resp = requests.post(
                        f"{PARSER_URL}/parse",
                        files={"file": (uploaded_file.name, uploaded_file.getvalue(), "application/pdf")},
                        data={"account_name": selected_account, "password": pdf_password or ""},
                        timeout=180,
                    )
                    resp.raise_for_status()
                    result = resp.json()

                    # Handle password errors before storing the result
                    if result.get("status") == "password_required":
                        st.error(
                            "🔒 This PDF is password-protected. "
                            "Enter the password in the **PDF password** field above and try again."
                        )
                        st.stop()
                    elif result.get("status") == "password_incorrect":
                        st.error(
                            "❌ Incorrect PDF password. "
                            "Please check the password and try again."
                        )
                        st.stop()

                    st.session_state["parse_result"] = result
            except Exception as exc:
                st.error(f"Parser error: {exc}")
                st.stop()

# ── Show parse result ─────────────────────────────────────────────────────────
if "parse_result" in st.session_state:
    result = st.session_state["parse_result"]
    transactions = result.get("transactions", [])
    balance_check = result.get("balance_check", {})
    status = result.get("status", "unknown")

    st.subheader("3. Verification result")
    col1, col2, col3 = st.columns(3)
    col1.metric("Transactions found", len(transactions))
    col2.metric("Method", result.get("extraction_method", "—"))

    if balance_check.get("passed"):
        col3.success("Balance check PASSED")
    else:
        reason = balance_check.get("reason", "")
        if result.get("extraction_method") == "csv":
            col3.info("CSV — balance check skipped")
        else:
            col3.error(f"Balance check FAILED: {reason}")

    if balance_check and result.get("extraction_method") != "csv":
        with st.expander("Balance details"):
            st.json(balance_check)

    # ── Editable transaction table ─────────────────────────────────────────────
    st.subheader("4. Transactions")
    df = pd.DataFrame(transactions)

    if status == "needs_review":
        if result.get("extraction_method") != "csv":
            st.warning("Manual review required — edit transactions before importing.")
        edited_df = st.data_editor(df, use_container_width=True, num_rows="dynamic")
        final_transactions = edited_df.to_dict(orient="records")
    else:
        st.dataframe(df, use_container_width=True)
        final_transactions = transactions

    # ── Import button ──────────────────────────────────────────────────────────
    st.subheader("5. Import to Firefly")
    if st.button("Import to Firefly", type="primary", disabled=len(final_transactions) == 0):
        # Re-stamp account_name — data_editor can silently lose it
        for txn in final_transactions:
            if not txn.get("account_name"):
                txn["account_name"] = selected_account
        with st.spinner("Pushing to Firefly III..."):
            try:
                import_resp = requests.post(
                    f"{PARSER_URL}/import",
                    json={"transactions": final_transactions},
                    timeout=120,
                )
                import_resp.raise_for_status()
                import_result = import_resp.json()

                if import_result.get("success"):
                    st.success(
                        f"Successfully imported {import_result.get('imported_count', 0)} transactions!"
                    )
                    history = st.session_state.get("import_history", [])
                    history.insert(0, {
                        "account": selected_account,
                        "count": import_result.get("imported_count", 0),
                        "status": "success",
                        "method": result.get("extraction_method"),
                    })
                    st.session_state["import_history"] = history[:10]
                    del st.session_state["parse_result"]
                else:
                    errors = import_result.get("errors") or [import_result.get("error", "Unknown error")]
                    st.error(f"Import failed ({import_result.get('error_count', '?')} errors). First error: {errors[0] if errors else 'Unknown'}")
                    with st.expander("All errors"):
                        st.write(errors)
            except Exception as exc:
                st.error(f"Import error: {exc}")

# ── Import history ─────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("Import history (this session)")
history = st.session_state.get("import_history", [])
if history:
    st.dataframe(pd.DataFrame(history), use_container_width=True)
else:
    st.caption("No imports yet in this session.")
