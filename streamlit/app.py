import calendar
import streamlit as st
import pandas as pd
from datetime import date

from auth import require_login
from sidebar import render_sidebar
from utils import apply_theme
from firefly_api import get_transactions

st.set_page_config(
    page_title="Household Finance",
    page_icon="💰",
    layout="wide",
    initial_sidebar_state="expanded",
)

apply_theme()

authenticator, name, authentication_status = require_login()

if not authentication_status:
    st.stop()

render_sidebar()

# ── Header ────────────────────────────────────────────────────────────────────
today = date.today()
st.title("Household Finance")
st.caption(f"{today.strftime('%A, %d %B %Y')}")

# ── This-month KPIs ───────────────────────────────────────────────────────────
month_start = today.replace(day=1)
prev_month_end = month_start - pd.Timedelta(days=1)
prev_month_start = prev_month_end.replace(day=1)

with st.spinner("Loading summary…"):
    try:
        def _sum(start, end, txn_type):
            raw = get_transactions(start=start.isoformat(), end=end.isoformat(), limit=500)
            total = 0.0
            for t in raw:
                for s in t.get("attributes", {}).get("transactions", []):
                    if s.get("type") == txn_type:
                        total += float(s.get("amount", 0))
            return total

        this_spend  = _sum(month_start,       today,            "withdrawal")
        this_income = _sum(month_start,       today,            "deposit")
        prev_spend  = _sum(prev_month_start,  prev_month_end,   "withdrawal")
        prev_income = _sum(prev_month_start,  prev_month_end,   "deposit")
        stats_ok = True
    except Exception:
        stats_ok = False

st.subheader(f"This month — {month_start.strftime('%B %Y')}")

if stats_ok:
    k1, k2, k3, k4 = st.columns(4)
    spend_delta  = this_spend  - prev_spend
    income_delta = this_income - prev_income
    net_this     = this_income - this_spend
    net_prev     = prev_income - prev_spend

    k1.metric(
        "Total Spend",
        f"AED {this_spend:,.0f}",
        delta=f"AED {spend_delta:+,.0f} vs last month",
        delta_color="inverse",
    )
    k2.metric(
        "Total Income",
        f"AED {this_income:,.0f}",
        delta=f"AED {income_delta:+,.0f} vs last month",
    )
    k3.metric(
        "Net",
        f"AED {net_this:,.0f}",
        delta=f"AED {net_this - net_prev:+,.0f} vs last month",
    )
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    k4.metric("Days elapsed", f"{(today - month_start).days + 1} / {days_in_month}")
else:
    st.info("Connect Firefly III to see live stats.")

st.markdown("---")

# ── Navigation cards ──────────────────────────────────────────────────────────
st.subheader("Navigate to")
c1, c2, c3, c4, c5, c6 = st.columns(6)

with c1:
    with st.container(border=True):
        st.markdown("#### 📥 Upload & Import")
        st.caption("Parse PDF or CSV bank statements and push transactions into Firefly III.")
        st.page_link("pages/1_upload.py", label="Open →")

with c2:
    with st.container(border=True):
        st.markdown("#### 📊 Dashboard")
        st.caption("Spending charts, KPIs, category breakdowns, and month-over-month trends.")
        st.page_link("pages/2_dashboard.py", label="Open →")

with c3:
    with st.container(border=True):
        st.markdown("#### 📋 Transactions")
        st.caption("Search, filter and browse all transactions. Export to CSV.")
        st.page_link("pages/3_transactions.py", label="Open →")

with c4:
    with st.container(border=True):
        st.markdown("#### 🤖 Ask AI")
        st.caption("Ask natural-language questions about your finances, powered by Claude.")
        st.page_link("pages/4_ask_ai.py", label="Open →")

with c5:
    with st.container(border=True):
        st.markdown("#### 🔄 Transfers")
        st.caption("View and apply transfer rules that classify internal transactions automatically.")
        st.page_link("pages/5_rules.py", label="Open →")

with c6:
    with st.container(border=True):
        st.markdown("#### 🏷️ Categories")
        st.caption("Define and apply category rules that auto-tag transactions by description.")
        st.page_link("pages/6_categories.py", label="Open →")
