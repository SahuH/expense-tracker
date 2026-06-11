import streamlit as st
import pandas as pd
from datetime import date

from auth import require_login
from firefly_api import get_transactions, get_accounts, get_categories

_, _, authentication_status = require_login()
if not authentication_status:
    st.stop()

st.title("Transactions")


def _flatten_transactions(raw: list) -> pd.DataFrame:
    rows = []
    for t in raw:
        tid = t.get("id")
        for split in t.get("attributes", {}).get("transactions", []):
            rows.append(
                {
                    "id": tid,
                    "date": split.get("date", "")[:10],
                    "description": split.get("description", ""),
                    "amount": float(split.get("amount", 0)),
                    "type": split.get("type", ""),
                    "category": split.get("category_name") or "Uncategorised",
                    "account": split.get("source_name") or split.get("destination_name") or "",
                    "currency": split.get("currency_code", "AED"),
                }
            )
    df = pd.DataFrame(rows)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


# ── Filters ────────────────────────────────────────────────────────────────────
st.sidebar.header("Filters")

today = date.today()
default_start = today.replace(day=1)
start_date = st.sidebar.date_input("From", value=today.replace(year=today.year - 1))
end_date = st.sidebar.date_input("To", value=today)

try:
    raw_accounts = get_accounts()
    account_map = {"All accounts": None}
    account_map.update({a["attributes"]["name"]: a["id"] for a in raw_accounts})
except Exception:
    account_map = {"All accounts": None}

selected_account_name = st.sidebar.selectbox("Account", list(account_map.keys()))
account_id = account_map[selected_account_name]

txn_type_filter = st.sidebar.selectbox("Type", ["All", "debit", "credit"])
search_term = st.sidebar.text_input("Search description", "")

amount_min = st.sidebar.number_input("Min amount (AED)", min_value=0.0, value=0.0)
amount_max = st.sidebar.number_input("Max amount (AED)", min_value=0.0, value=100000.0)

# ── Load data ─────────────────────────────────────────────────────────────────
try:
    raw = get_transactions(
        start=start_date.isoformat(),
        end=end_date.isoformat(),
        account_id=account_id,
        limit=1000,
    )
    df = _flatten_transactions(raw)
except Exception as exc:
    st.error(f"Failed to load transactions: {exc}")
    st.stop()

if df.empty:
    st.info("No transactions found.")
    st.stop()

# ── Apply filters ─────────────────────────────────────────────────────────────
if txn_type_filter != "All":
    map_type = {"debit": "withdrawal", "credit": "deposit"}
    df = df[df["type"] == map_type.get(txn_type_filter, txn_type_filter)]

if search_term:
    df = df[df["description"].str.contains(search_term, case=False, na=False)]

df = df[(df["amount"] >= amount_min) & (df["amount"] <= amount_max)]

# Category filter
try:
    raw_cats = get_categories()
    cat_names = ["All"] + sorted({c["attributes"]["name"] for c in raw_cats})
except Exception:
    cat_names = ["All"]

selected_cat = st.sidebar.selectbox("Category", cat_names)
if selected_cat != "All":
    df = df[df["category"] == selected_cat]

# ── Display ───────────────────────────────────────────────────────────────────
st.markdown(f"**{len(df)} transactions** matching filters")

display_df = df.drop(columns=["id"]).sort_values("date", ascending=False)
display_df["date"] = display_df["date"].dt.strftime("%Y-%m-%d")
display_df["amount"] = display_df["amount"].map(lambda x: f"{x:,.2f}")

selected = st.dataframe(
    display_df,
    use_container_width=True,
    on_select="rerun",
    selection_mode="single-row",
)

# ── Row detail ────────────────────────────────────────────────────────────────
if selected and selected.get("selection", {}).get("rows"):
    idx = selected["selection"]["rows"][0]
    row = display_df.iloc[idx]
    with st.expander("Transaction detail", expanded=True):
        for col in display_df.columns:
            st.write(f"**{col.title()}:** {row[col]}")

# ── Export ────────────────────────────────────────────────────────────────────
st.download_button(
    "Export to CSV",
    data=display_df.to_csv(index=False).encode("utf-8"),
    file_name="transactions_export.csv",
    mime="text/csv",
)
