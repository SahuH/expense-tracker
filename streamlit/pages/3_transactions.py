import streamlit as st
import pandas as pd
from datetime import date

from auth import require_login
from sidebar import render_sidebar
from utils import apply_theme
from firefly_api import get_transactions, get_accounts

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


# ── Date range + account ──────────────────────────────────────────────────────
today = date.today()

if "txn_start" not in st.session_state:
    st.session_state.txn_start = today.replace(year=today.year - 1)
if "txn_end" not in st.session_state:
    st.session_state.txn_end = today

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
display_df = df.drop(columns=["id"]).sort_values("date", ascending=False).copy()
display_df["type"] = display_df["type"].map({"withdrawal": "Expense", "deposit": "Income"})
display_df["date"] = display_df["date"].dt.strftime("%Y-%m-%d")

selected = st.dataframe(
    display_df,
    use_container_width=True,
    height=500,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    column_config={
        "date":        st.column_config.TextColumn("Date",        width="small"),
        "description": st.column_config.TextColumn("Description", width="large"),
        "amount":      st.column_config.NumberColumn("Amount",    format="AED %.2f", width="small"),
        "type":        st.column_config.TextColumn("Type",        width="small"),
        "category":    st.column_config.TextColumn("Category",    width="medium"),
        "account":     st.column_config.TextColumn("Account",     width="medium"),
        "currency":    st.column_config.TextColumn("Currency",    width="small"),
    },
)

# ── Row detail ────────────────────────────────────────────────────────────────
if selected and selected.get("selection", {}).get("rows"):
    row = display_df.iloc[selected["selection"]["rows"][0]]
    with st.container(border=True):
        st.markdown("**Transaction detail**")
        d1, d2, d3 = st.columns(3)
        with d1:
            st.metric("Amount", f"AED {row['amount']:,.2f}")
            st.write(f"**Date:** {row['date']}")
        with d2:
            st.write(f"**Description:** {row['description']}")
            st.write(f"**Type:** {row['type']}")
        with d3:
            st.write(f"**Category:** {row['category']}")
            st.write(f"**Account:** {row['account']}")

# ── Export ────────────────────────────────────────────────────────────────────
st.download_button(
    "⬇ Export to CSV",
    data=display_df.to_csv(index=False).encode("utf-8"),
    file_name=f"transactions_{start_date}_{end_date}.csv",
    mime="text/csv",
)
