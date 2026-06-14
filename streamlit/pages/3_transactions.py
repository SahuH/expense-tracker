import streamlit as st
import pandas as pd
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

from auth import require_login
from sidebar import render_sidebar
from utils import apply_theme
from firefly_api import get_transactions, get_accounts, get_categories, update_transaction

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

with st.spinner("Loading accounts…"):
    try:
        raw_accounts = get_accounts()
        account_map = {"All accounts": None}
        account_map.update({a["attributes"]["name"]: a["id"] for a in raw_accounts})
    except Exception:
        account_map = {"All accounts": None}

with col3:
    selected_account_name = st.selectbox("Account", list(account_map.keys()))

account_id = account_map[selected_account_name]

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

if df_full.empty:
    st.info("No transactions found for the selected period.")
    st.stop()

# ── Inline client-side filters ────────────────────────────────────────────────
st.markdown("---")
f1, f2, f3, f4 = st.columns([2, 3, 2, 1])

with f1:
    type_filter = st.selectbox("Type", ["All", "Expense", "Income"], key="txn_type")
with f2:
    search = st.text_input("Search description", placeholder="e.g. Carrefour, Noon", key="txn_search")
with f3:
    cat_options = ["All"] + sorted(df_full["category"].dropna().unique().tolist())
    cat_filter = st.selectbox("Category", cat_options, key="txn_cat")
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
# df_sorted keeps "id" and raw "type"; display_df mirrors it without id, with friendly labels.
df_sorted = df.sort_values("date", ascending=False).reset_index(drop=True)
display_df = df_sorted.drop(columns=["id"]).copy()
display_df["type"] = display_df["type"].map(
    {"withdrawal": "Expense", "deposit": "Income", "transfer": "Transfer"}
)
display_df["date"] = display_df["date"].dt.date  # keep as date for data_editor DateColumn

# ── Load existing categories for the dropdown ─────────────────────────────────
_NEW_CAT_SENTINEL = "✏️ New category…"
try:
    _raw_cats = get_categories()
    _cat_names = sorted(c["attributes"]["name"] for c in _raw_cats)
except Exception:
    _cat_names = []

# Include any categories already on displayed transactions that aren't in Firefly's list yet
_extra_cats = sorted(
    c for c in display_df["category"].dropna().unique()
    if c and c != "Uncategorised" and c not in _cat_names
)
_cat_options = [_NEW_CAT_SENTINEL, "Uncategorised"] + _cat_names + _extra_cats

st.caption(
    "Select a category from the dropdown, or choose **✏️ New category…** and type a name below. "
    "Click **Save changes** when done."
)

edited_df = st.data_editor(
    display_df,
    use_container_width=True,
    height=500,
    hide_index=True,
    disabled=["account", "currency"],
    column_config={
        "date":        st.column_config.DateColumn("Date", format="YYYY-MM-DD", width="small"),
        "description": st.column_config.TextColumn("Description", width="large"),
        "amount":      st.column_config.NumberColumn("Amount", format="AED %.2f", min_value=0, width="small"),
        "type":        st.column_config.SelectboxColumn("Type", options=["Expense", "Income", "Transfer"], width="small"),
        "category":    st.column_config.SelectboxColumn("Category", options=_cat_options, width="medium"),
        "account":     st.column_config.TextColumn("Account", width="medium"),
        "currency":    st.column_config.TextColumn("Currency", width="small"),
    },
)

# ── New-category text input (shown only when sentinel is selected) ─────────────
_sentinel_rows = (edited_df["category"] == _NEW_CAT_SENTINEL)
_new_cat_name = ""
if _sentinel_rows.any():
    _new_cat_name = st.text_input(
        "New category name",
        placeholder="e.g. Subscriptions",
        key="txn_new_cat_input",
    )

# ── Detect changes ────────────────────────────────────────────────────────────
try:
    changed_mask = ~(edited_df.astype(str) == display_df.astype(str)).all(axis=1)
except Exception:
    changed_mask = pd.Series([False] * len(edited_df))

n_changed = int(changed_mask.sum())

btn_col, info_col = st.columns([1, 4])
with btn_col:
    save_clicked = st.button("Save changes", type="primary", disabled=(n_changed == 0))
with info_col:
    if n_changed > 0:
        st.caption(f"{n_changed} row(s) modified — click Save to push to Firefly.")

if save_clicked:
    # Substitute sentinel with typed new category name
    if _sentinel_rows.any():
        if not _new_cat_name.strip():
            st.error("Enter a name for the new category before saving.")
            st.stop()
        edited_df = edited_df.copy()
        edited_df.loc[_sentinel_rows, "category"] = _new_cat_name.strip()

    type_map = {"Expense": "withdrawal", "Income": "deposit", "Transfer": "transfer"}
    saved, errors = 0, []
    for idx in edited_df[changed_mask].index:
        row = edited_df.iloc[idx]
        txn_id = df_sorted.iloc[idx]["id"]
        d = row["date"]
        date_str = (d.isoformat() if hasattr(d, "isoformat") else str(d)) + "T00:00:00+00:00"
        fields = {
            "description": str(row["description"]),
            "date": date_str,
            "amount": str(row["amount"]),
            "type": type_map.get(row["type"], "withdrawal"),
        }
        cat = row["category"]
        if cat and cat != "Uncategorised":
            fields["category_name"] = cat
        try:
            update_transaction(txn_id, fields)
            saved += 1
        except Exception as exc:
            errors.append(f"Row {idx + 1}: {exc}")
    if saved:
        st.success(f"{saved} transaction(s) updated.")
    if errors:
        st.error("\n".join(errors))
    if saved:
        st.rerun()

# ── Export ────────────────────────────────────────────────────────────────────
st.download_button(
    "⬇ Export to CSV",
    data=display_df.to_csv(index=False).encode("utf-8"),
    file_name=f"transactions_{start_date}_{end_date}.csv",
    mime="text/csv",
)
