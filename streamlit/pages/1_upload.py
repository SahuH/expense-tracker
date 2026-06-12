import streamlit as st
import requests
import pandas as pd
import io
import os
from urllib.parse import quote

from auth import require_login
from sidebar import render_sidebar
from utils import apply_theme, step_indicator
from firefly_api import get_accounts

st.set_page_config(
    page_title="Upload & Import — Household Finance",
    page_icon="💰",
    layout="wide",
)

apply_theme()

_, _, authentication_status = require_login()
if not authentication_status:
    st.stop()

render_sidebar()

PARSER_URL = os.getenv("PARSER_URL", "http://parser:8000")

st.title("Upload & Import")

# ── 1. Account selector ───────────────────────────────────────────────────────
st.subheader("1. Select account")
with st.spinner("Loading accounts…"):
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

# ── PDF password ──────────────────────────────────────────────────────────────
_has_saved_pw = False
try:
    _pw_check = requests.get(
        f"{PARSER_URL}/passwords/{quote(selected_account, safe='')}",
        timeout=5,
    )
    if _pw_check.ok:
        _has_saved_pw = _pw_check.json().get("has_password", False)
except Exception:
    pass

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
        placeholder="Leave blank if not required — will be saved automatically on first success",
        key="pdf_password",
    )

# ── 2. File uploader ──────────────────────────────────────────────────────────
st.subheader("2. Upload statement")
uploaded_file = st.file_uploader("Choose a PDF or CSV file", type=["pdf", "csv"])

# ── Step indicator (position after file upload so we know current state) ──────
if "parse_result" in st.session_state:
    _step = 4
elif uploaded_file is not None:
    _step = 3
else:
    _step = 2

step_indicator(["Account", "Upload File", "Parse", "Review & Import"], _step)


# ── CSV parser ────────────────────────────────────────────────────────────────
def _parse_csv(file_bytes: bytes, account_name: str) -> dict:
    df = pd.read_csv(io.BytesIO(file_bytes))
    df.columns = [c.strip().lower() for c in df.columns]

    col_map = {}
    for c in df.columns:
        if any(k in c for k in ("date", "txn date", "value date", "transaction date")):
            col_map["date"] = c
            break
    for c in df.columns:
        if any(k in c for k in ("description", "narration", "particular", "details", "merchant", "remarks")):
            col_map["description"] = c
            break
    for c in df.columns:
        if any(k in c for k in ("debit", "withdrawal", "dr amount", "debit amount")):
            col_map["debit"] = c
            break
    for c in df.columns:
        if any(k in c for k in ("credit", "deposit", "cr amount", "credit amount")):
            col_map["credit"] = c
            break
    if "debit" not in col_map and "credit" not in col_map:
        for c in df.columns:
            if c in ("amount", "transaction amount", "txn amount"):
                col_map["amount"] = c
                break

    transactions = []
    for _, row in df.iterrows():
        date_val = str(row.get(col_map.get("date", ""), "")).strip()
        desc = str(row.get(col_map.get("description", ""), "")).strip()

        if "debit" in col_map or "credit" in col_map:
            debit_raw  = row.get(col_map.get("debit", ""),  None)
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
            amount, txn_type = (abs(raw), "debit") if raw < 0 else (raw, "credit")
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


# ── Parse button ──────────────────────────────────────────────────────────────
if uploaded_file is not None:
    c_info, c_parse = st.columns([4, 1])
    c_info.info(f"📄 **{uploaded_file.name}** — {uploaded_file.size / 1024:.1f} KB")
    is_csv = uploaded_file.name.lower().endswith(".csv")

    if c_parse.button("Parse", type="primary", use_container_width=True):
        with st.spinner("Extracting transactions…"):
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

                    if result.get("status") == "password_required":
                        st.error("🔒 This PDF is password-protected. Enter the password in the field above and try again.")
                        st.stop()
                    elif result.get("status") == "password_incorrect":
                        st.error("❌ Incorrect PDF password. Please check and try again.")
                        st.stop()

                    st.session_state["parse_result"] = result
            except Exception as exc:
                st.error(f"Parser error: {exc}")
                st.stop()
        st.rerun()

# ── 3. Verification result ────────────────────────────────────────────────────
if "parse_result" in st.session_state:
    result = st.session_state["parse_result"]
    transactions = result.get("transactions", [])
    balance_check = result.get("balance_check", {})
    status = result.get("status", "unknown")

    st.subheader("3. Verification")
    v1, v2, v3 = st.columns(3)
    v1.metric("Transactions found", len(transactions))
    v2.metric("Method", result.get("extraction_method", "—"))

    if balance_check.get("passed"):
        v3.success("✅ Balance check passed")
    elif result.get("extraction_method") == "csv":
        v3.info("ℹ️ CSV — balance check skipped")
    else:
        v3.error(f"⚠️ Balance check failed: {balance_check.get('reason', '')}")

    if balance_check and result.get("extraction_method") != "csv":
        with st.expander("Balance details"):
            st.json(balance_check)

    # ── 4. Editable transaction table ─────────────────────────────────────────
    st.subheader("4. Transactions")

    df = pd.DataFrame(transactions)
    if not df.empty and "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")

    _col_config = {
        "date":         st.column_config.DateColumn("Date",         format="YYYY-MM-DD"),
        "description":  st.column_config.TextColumn("Description",  width="large"),
        "amount":       st.column_config.NumberColumn("Amount",     format="AED %.2f", min_value=0),
        "type":         st.column_config.SelectboxColumn("Type",    options=["debit", "credit"]),
        "account_name": st.column_config.TextColumn("Account"),
        "currency":     st.column_config.SelectboxColumn("Currency",options=["AED", "USD", "EUR", "GBP", "INR"]),
        "balance_after":st.column_config.NumberColumn("Balance after", format="AED %.2f"),
    }

    if status == "needs_review":
        if result.get("extraction_method") != "csv":
            st.warning("Manual review required — edit any rows before importing.")
        edited_df = st.data_editor(
            df,
            use_container_width=True,
            num_rows="dynamic",
            column_config=_col_config,
        )
        # Convert date back to string for the import payload
        edited_df["date"] = edited_df["date"].apply(
            lambda d: str(d)[:10] if pd.notna(d) else ""
        )
        final_transactions = edited_df.to_dict(orient="records")
    else:
        st.dataframe(df, use_container_width=True, column_config=_col_config, hide_index=True)
        final_transactions = transactions

    # ── 5. Import ─────────────────────────────────────────────────────────────
    st.subheader("5. Import to Firefly")
    imp_col, _ = st.columns([2, 6])
    if imp_col.button("Import to Firefly", type="primary", use_container_width=True, disabled=len(final_transactions) == 0):
        for txn in final_transactions:
            if not txn.get("account_name"):
                txn["account_name"] = selected_account
        with st.spinner("Pushing to Firefly III…"):
            try:
                import_resp = requests.post(
                    f"{PARSER_URL}/import",
                    json={"transactions": final_transactions},
                    timeout=120,
                )
                import_resp.raise_for_status()
                import_result = import_resp.json()

                if import_result.get("success"):
                    st.success(f"✅ Imported {import_result.get('imported_count', 0)} transactions!")
                    history = st.session_state.get("import_history", [])
                    history.insert(0, {
                        "Account": selected_account,
                        "Imported": import_result.get("imported_count", 0),
                        "Status": "✅ Success",
                        "Method": result.get("extraction_method"),
                    })
                    st.session_state["import_history"] = history[:10]
                    del st.session_state["parse_result"]
                    st.rerun()
                else:
                    errors = import_result.get("errors") or [import_result.get("error", "Unknown error")]
                    st.error(f"Import failed ({import_result.get('error_count', '?')} errors). First: {errors[0] if errors else '?'}")
                    with st.expander("All errors"):
                        st.write(errors)
            except Exception as exc:
                st.error(f"Import error: {exc}")

# ── Import history ─────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("Import history (this session)")
history = st.session_state.get("import_history", [])
if history:
    st.dataframe(
        pd.DataFrame(history),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Account":  st.column_config.TextColumn("Account"),
            "Imported": st.column_config.NumberColumn("Imported", format="%d txns"),
            "Status":   st.column_config.TextColumn("Status"),
            "Method":   st.column_config.TextColumn("Method"),
        },
    )
else:
    st.caption("No imports yet this session.")
