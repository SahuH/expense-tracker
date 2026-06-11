import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

from auth import require_login
from firefly_api import get_transactions, get_accounts

_, _, authentication_status = require_login()
if not authentication_status:
    st.stop()

st.title("Dashboard")


def _load_transactions(start: str, end: str, account_id: str = None) -> pd.DataFrame:
    raw = get_transactions(start=start, end=end, account_id=account_id)
    rows = []
    for t in raw:
        for split in t.get("attributes", {}).get("transactions", []):
            rows.append(
                {
                    "date": split.get("date", "")[:10],
                    "description": split.get("description", ""),
                    "amount": float(split.get("amount", 0)),
                    "type": split.get("type", ""),
                    "category": split.get("category_name", "Other") or "Other",
                    "account": split.get("source_name", "") or split.get("destination_name", ""),
                    "currency": split.get("currency_code", "AED"),
                }
            )
    df = pd.DataFrame(rows)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


# ── Filters ────────────────────────────────────────────────────────────────────
col1, col2, col3 = st.columns(3)
today = date.today()
default_start = today.replace(day=1)

with col1:
    start_date = st.date_input("From", value=default_start)
with col2:
    end_date = st.date_input("To", value=today)

try:
    raw_accounts = get_accounts()
    account_map = {"All accounts": None}
    account_map.update({a["attributes"]["name"]: a["id"] for a in raw_accounts})
except Exception:
    account_map = {"All accounts": None}

with col3:
    selected_account_name = st.selectbox("Account", list(account_map.keys()))

account_id = account_map[selected_account_name]
start_str = start_date.isoformat()
end_str = end_date.isoformat()

try:
    df = _load_transactions(start_str, end_str, account_id)
except Exception as exc:
    st.error(f"Failed to load transactions: {exc}")
    st.stop()

if df.empty:
    st.info("No transactions found for the selected period.")
    st.stop()

debits = df[df["type"] == "withdrawal"]
credits = df[df["type"] == "deposit"]

total_spend = debits["amount"].sum()
total_income = credits["amount"].sum()
net = total_income - total_spend
top_cat = debits.groupby("category")["amount"].sum().idxmax() if not debits.empty else "—"

# ── KPI row ────────────────────────────────────────────────────────────────────
k1, k2, k3, k4 = st.columns(4)
k1.metric("Total Spend", f"AED {total_spend:,.2f}")
k2.metric("Total Income", f"AED {total_income:,.2f}")
k3.metric("Net", f"AED {net:,.2f}", delta_color="normal")
k4.metric("Top Category", top_cat)

st.markdown("---")

# ── Charts row 1 ──────────────────────────────────────────────────────────────
c1, c2 = st.columns(2)

with c1:
    st.subheader("Spend by category")
    if not debits.empty:
        cat_df = debits.groupby("category")["amount"].sum().reset_index()
        fig = px.pie(cat_df, values="amount", names="category", hole=0.4)
        fig.update_traces(textposition="inside", textinfo="percent+label")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No debit transactions.")

with c2:
    st.subheader("Daily spend")
    if not debits.empty:
        daily = debits.groupby("date")["amount"].sum().reset_index()
        fig = px.line(daily, x="date", y="amount", markers=True)
        fig.update_layout(yaxis_title="AED", xaxis_title="")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No debit transactions.")

# ── Charts row 2 — month-over-month ───────────────────────────────────────────
st.subheader("Month-over-month spend (last 6 months)")
try:
    months = []
    for i in range(5, -1, -1):
        m_start = (today.replace(day=1) - relativedelta(months=i))
        m_end = (m_start + relativedelta(months=1) - timedelta(days=1))
        m_df = _load_transactions(m_start.isoformat(), m_end.isoformat(), account_id)
        spend = m_df[m_df["type"] == "withdrawal"]["amount"].sum() if not m_df.empty else 0
        months.append({"month": m_start.strftime("%b %Y"), "spend": spend})

    mom_df = pd.DataFrame(months)
    fig = px.bar(mom_df, x="month", y="spend", text_auto=".0f")
    fig.update_layout(yaxis_title="AED", xaxis_title="")
    st.plotly_chart(fig, use_container_width=True)
except Exception as exc:
    st.warning(f"Could not load month-over-month data: {exc}")
