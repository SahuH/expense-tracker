import streamlit as st
import pandas as pd
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

from auth import require_login
from sidebar import render_sidebar
from utils import apply_theme
from firefly_api import (
    get_transactions, get_accounts, get_categories,
    update_transaction, create_transaction, delete_transaction,
)

st.set_page_config(
    page_title="Transactions — Household Finance",
    page_icon="💰",
    layout="wide",
)

apply_theme()

_, _, authentication_status = require_login()
if not authentication_status:
    st.stop()

render_sidebar()

st.title("Transactions")


def _flatten(raw: list) -> pd.DataFrame:
    rows = []
    for t in raw:
        tid = t.get("id")
        for split in t.get("attributes", {}).get("transactions", []):
            rows.append({
                "id":          tid,
                "date":        split.get("date", "")[:10],
                "description": split.get("description", ""),
                "amount":      float(split.get("amount", 0)),
                "type":        split.get("type", ""),
                "category":    split.get("category_name") or "Uncategorised",
                "account":     (
                    split.get("destination_name")
                    if split.get("type") == "deposit"
                    else split.get("source_name")
                ) or "",
                "currency":    split.get("currency_code", "AED"),
            })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


# ── Date presets ──────────────────────────────────────────────────────────────
today = date.today()

PRESETS = {
    "This Month": (today.replace(day=1), today),
    "Last Month": (
        today.replace(day=1) - relativedelta(months=1),
        today.replace(day=1) - timedelta(days=1),
    ),
    "Last 3M":  (today.replace(day=1) - relativedelta(months=2), today),
    "YTD":      (today.replace(month=1, day=1), today),
    "Last 12M": (today - relativedelta(months=12), today),
}

if "txn_start" not in st.session_state:
    st.session_state.txn_start = today.replace(day=1)
if "txn_end" not in st.session_state:
    st.session_state.txn_end = today
if "txn_preset" not in st.session_state:
    st.session_state.txn_preset = "This Month"

preset_cols = st.columns(len(PRESETS) + 4)
for i, (label, (ps, pe)) in enumerate(PRESETS.items()):
    is_active = st.session_state.txn_preset == label
    if preset_cols[i].button(
        label,
        type="primary" if is_active else "secondary",
        use_container_width=True,
    ):
        st.session_state.txn_start = ps
        st.session_state.txn_end = pe
        st.session_state.txn_preset = label
        st.rerun()

# ── Date range + account ──────────────────────────────────────────────────────
col1, col2, col3 = st.columns(3)
with col1:
    start_date = st.date_input("From", key="txn_start")
with col2:
    end_date = st.date_input("To", key="txn_end")

with st.spinner("Loading accounts & categories…"):
    try:
        raw_accounts = get_accounts()
        account_map = {"All accounts": None}
        account_map.update({a["attributes"]["name"]: a["id"] for a in raw_accounts})
    except Exception:
        account_map = {"All accounts": None}

    try:
        _raw_cats = get_categories()
        _cat_names = sorted(c["attributes"]["name"] for c in _raw_cats)
    except Exception:
        _cat_names = []

with col3:
    selected_account_name = st.selectbox("Account", list(account_map.keys()))

account_id = account_map[selected_account_name]
_asset_account_names = [k for k in account_map if k != "All accounts"]

# ── Load data ─────────────────────────────────────────────────────────────────
with st.spinner("Loading transactions…"):
    try:
        raw = get_transactions(
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            account_id=account_id,
            limit=1000,
        )
        df_full = _flatten(raw)
    except Exception as exc:
        st.error(f"Failed to load transactions: {exc}")
        st.stop()

# ── Add new transaction ───────────────────────────────────────────────────────
with st.expander("➕ Add new transaction"):
    _fa1, _fa2, _fa3 = st.columns(3)
    _new_date     = _fa1.date_input("Date", value=today, key="add_date")
    _new_amount   = _fa2.number_input("Amount (AED)", min_value=0.0, step=0.01, format="%.2f", key="add_amount")
    _new_type     = _fa3.selectbox("Type", ["Expense", "Income"], key="add_type")
    _new_desc     = st.text_input("Description", key="add_desc")
    _fb1, _fb2, _fb3 = st.columns(3)
    _new_cat      = _fb1.selectbox("Category", ["— none —"] + _cat_names, key="add_cat")
    _new_account  = _fb2.selectbox("Account", _asset_account_names or [""], key="add_acct")
    _new_currency = _fb3.selectbox("Currency", ["AED", "USD", "EUR", "GBP", "INR"], key="add_curr")

    if st.button("Add transaction", type="primary", key="add_txn_btn"):
        if not _new_desc.strip() or _new_amount <= 0:
            st.error("Description and amount are required.")
        else:
            _txn_type = "withdrawal" if _new_type == "Expense" else "deposit"
            _fields = {
                "type":          _txn_type,
                "date":          _new_date.isoformat() + "T00:00:00+00:00",
                "amount":        str(round(_new_amount, 2)),
                "description":   _new_desc.strip(),
                "currency_code": _new_currency,
            }
            if _new_cat != "— none —":
                _fields["category_name"] = _new_cat
            if _txn_type == "withdrawal":
                _fields["source_name"] = _new_account
            else:
                _fields["destination_name"] = _new_account
            try:
                create_transaction(_fields)
                st.success("Transaction added!")
                for _k in ("add_desc", "add_amount", "add_cat", "add_acct", "add_curr", "add_type", "add_date"):
                    st.session_state.pop(_k, None)
                st.rerun()
            except Exception as _exc:
                st.error(f"Failed to add: {_exc}")

st.markdown("---")

if df_full.empty:
    st.info("No transactions found for the selected period.")
    st.stop()

# ── Inline client-side filters ────────────────────────────────────────────────
f1, f2, f3, f4 = st.columns([2, 3, 2, 1])

with f1:
    type_filter = st.selectbox("Type", ["All", "Expense", "Income"], key="txn_type")
with f2:
    search = st.text_input("Search description", placeholder="e.g. Carrefour, Noon", key="txn_search")
with f3:
    cat_options_filter = ["All"] + sorted(df_full["category"].dropna().unique().tolist())
    cat_filter = st.selectbox("Category", cat_options_filter, key="txn_cat")
with f4:
    st.markdown("<div style='margin-top:1.75rem'></div>", unsafe_allow_html=True)
    if st.button("Reset", use_container_width=True, help="Clear all client-side filters"):
        for k in ("txn_type", "txn_search", "txn_cat", "txn_amt"):
            st.session_state.pop(k, None)
        st.rerun()

# Amount range — derived from actual data so it's always valid
amt_min = float(df_full["amount"].min())
amt_max = float(df_full["amount"].max())
if amt_max > amt_min:
    amount_range = st.slider(
        "Amount range (AED)",
        min_value=amt_min,
        max_value=amt_max,
        value=(amt_min, amt_max),
        format="AED %.0f",
        key="txn_amt",
    )
else:
    amount_range = (amt_min, amt_max)

# ── Apply filters ─────────────────────────────────────────────────────────────
df = df_full.copy()

if type_filter == "Expense":
    df = df[df["type"] == "withdrawal"]
elif type_filter == "Income":
    df = df[df["type"] == "deposit"]

if search:
    df = df[df["description"].str.contains(search, case=False, na=False)]

if cat_filter != "All":
    df = df[df["category"] == cat_filter]

df = df[(df["amount"] >= amount_range[0]) & (df["amount"] <= amount_range[1])]

# ── Active filter summary ─────────────────────────────────────────────────────
active_chips = []
if type_filter != "All":
    active_chips.append(type_filter)
if search:
    active_chips.append(f'"{search}"')
if cat_filter != "All":
    active_chips.append(cat_filter)
if amount_range != (amt_min, amt_max):
    active_chips.append(f"AED {amount_range[0]:,.0f} – {amount_range[1]:,.0f}")

summary = f"**{len(df):,}** of {len(df_full):,} transactions"
if active_chips:
    summary += "  ·  Filters: " + "  ·  ".join(active_chips)
st.markdown(summary)

if df.empty:
    st.info("No transactions match the current filters.")
    st.stop()

# ── Map type labels and prepare display frame ─────────────────────────────────
df_sorted = df.sort_values("date", ascending=False).reset_index(drop=True)
display_df = df_sorted.drop(columns=["id"]).copy()
display_df["type"] = display_df["type"].map(
    {"withdrawal": "Expense", "deposit": "Income", "transfer": "Transfer"}
)
display_df["date"] = display_df["date"].dt.date

# Delete-checkbox column (leftmost)
display_df.insert(0, "🗑️", False)

# ── Account options (asset accounts + any extra seen in current data) ──────────
_extra_accounts = sorted(
    a for a in display_df["account"].dropna().unique()
    if a and a not in _asset_account_names
)
_all_account_options = _asset_account_names + _extra_accounts

# ── Category dropdown options ─────────────────────────────────────────────────
_NEW_CAT_SENTINEL = "✏️ New category…"
_extra_cats = sorted(
    c for c in display_df["category"].dropna().unique()
    if c and c != "Uncategorised" and c not in _cat_names
)
_cat_options = [_NEW_CAT_SENTINEL, "Uncategorised"] + _cat_names + _extra_cats

st.caption(
    "Tick **🗑️** to mark rows for deletion. "
    "Choose **✏️ New category…** and type below to create a new category. "
    "Click **Save changes** when done."
)

edited_df = st.data_editor(
    display_df,
    use_container_width=True,
    height=500,
    hide_index=True,
    column_config={
        "🗑️":        st.column_config.CheckboxColumn("Delete", width="small"),
        "date":        st.column_config.DateColumn("Date", format="YYYY-MM-DD", width="small"),
        "description": st.column_config.TextColumn("Description", width="large"),
        "amount":      st.column_config.NumberColumn("Amount", format="AED %.2f", min_value=0, width="small"),
        "type":        st.column_config.SelectboxColumn("Type", options=["Expense", "Income", "Transfer"], width="small"),
        "category":    st.column_config.SelectboxColumn("Category", options=_cat_options, width="medium"),
        "account":     st.column_config.SelectboxColumn("Account", options=_all_account_options, width="medium"),
        "currency":    st.column_config.SelectboxColumn("Currency", options=["AED", "USD", "EUR", "GBP", "INR"], width="small"),
    },
)

# ── New-category text input (shown only when sentinel is selected) ─────────────
_sentinel_rows = edited_df["category"] == _NEW_CAT_SENTINEL
_new_cat_name = ""
if _sentinel_rows.any():
    _new_cat_name = st.text_input(
        "New category name",
        placeholder="e.g. Subscriptions",
        key="txn_new_cat_input",
    )

# ── Detect deletions and modifications ────────────────────────────────────────
_delete_mask = edited_df["🗑️"] == True
_data_cols = [c for c in edited_df.columns if c != "🗑️"]
_display_data_cols = [c for c in display_df.columns if c != "🗑️"]

try:
    changed_mask = (
        ~(edited_df[_data_cols].astype(str) == display_df[_display_data_cols].astype(str)).all(axis=1)
        & ~_delete_mask
    )
except Exception:
    changed_mask = pd.Series([False] * len(edited_df))

n_changed = int(changed_mask.sum())
n_deleted = int(_delete_mask.sum())

btn_col, info_col = st.columns([1, 4])
with btn_col:
    save_clicked = st.button(
        "Save changes",
        type="primary",
        disabled=(n_changed == 0 and n_deleted == 0),
    )
with info_col:
    _parts = []
    if n_changed > 0:
        _parts.append(f"{n_changed} modified")
    if n_deleted > 0:
        _parts.append(f"{n_deleted} to delete")
    if _parts:
        st.caption(", ".join(_parts) + " — click Save to apply.")

if save_clicked:
    if _sentinel_rows.any():
        if not _new_cat_name.strip():
            st.error("Enter a name for the new category before saving.")
            st.stop()
        edited_df = edited_df.copy()
        edited_df.loc[_sentinel_rows, "category"] = _new_cat_name.strip()

    type_map = {"Expense": "withdrawal", "Income": "deposit", "Transfer": "transfer"}
    saved, del_saved, errors = 0, 0, []

    # Deletions
    for idx in edited_df[_delete_mask].index:
        txn_id = df_sorted.iloc[idx]["id"]
        try:
            delete_transaction(txn_id)
            del_saved += 1
        except Exception as exc:
            errors.append(f"Delete row {idx + 1}: {exc}")

    # Updates
    for idx in edited_df[changed_mask].index:
        row = edited_df.iloc[idx]
        txn_id = df_sorted.iloc[idx]["id"]
        d = row["date"]
        date_str = (d.isoformat() if hasattr(d, "isoformat") else str(d)) + "T00:00:00+00:00"
        txn_type = type_map.get(row["type"], "withdrawal")
        fields = {
            "description":   str(row["description"]),
            "date":          date_str,
            "amount":        str(row["amount"]),
            "type":          txn_type,
            "currency_code": str(row["currency"]),
        }
        acct = str(row.get("account", "") or "")
        if acct:
            if txn_type == "deposit":
                fields["destination_name"] = acct
            else:
                fields["source_name"] = acct
        cat = row["category"]
        if cat and cat != "Uncategorised":
            fields["category_name"] = cat
        try:
            update_transaction(txn_id, fields)
            saved += 1
        except Exception as exc:
            errors.append(f"Update row {idx + 1}: {exc}")

    msgs = []
    if del_saved:
        msgs.append(f"{del_saved} deleted")
    if saved:
        msgs.append(f"{saved} updated")
    if msgs:
        st.success(", ".join(msgs) + ".")
    if errors:
        st.error("\n".join(errors))
    if del_saved or saved:
        st.rerun()

# ── Export ────────────────────────────────────────────────────────────────────
st.download_button(
    "⬇ Export to CSV",
    data=display_df.drop(columns=["🗑️"]).to_csv(index=False).encode("utf-8"),
    file_name=f"transactions_{start_date}_{end_date}.csv",
    mime="text/csv",
)
