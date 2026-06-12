import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

from auth import require_login
from sidebar import render_sidebar
from utils import apply_theme
from firefly_api import get_transactions, get_accounts

st.set_page_config(page_title="Dashboard — Household Finance", page_icon="💰", layout="wide")
apply_theme()

_, _, authentication_status = require_login()
if not authentication_status:
    st.stop()

render_sidebar()

st.title("Dashboard")

COLORS = {
    "spend": "#ef553b",
    "income": "#00cc96",
    "bar": "rgba(99,110,250,0.7)",
    "ma": "#ef553b",
}
CAT_COLORS = px.colors.qualitative.Set2


def _load_transactions(start: str, end: str, account_id: str = None) -> pd.DataFrame:
    raw = get_transactions(start=start, end=end, account_id=account_id)
    rows = []
    for t in raw:
        for split in t.get("attributes", {}).get("transactions", []):
            rows.append({
                "date": split.get("date", "")[:10],
                "description": split.get("description", ""),
                "amount": float(split.get("amount", 0)),
                "type": split.get("type", ""),
                "category": split.get("category_name", "Other") or "Other",
                "account": split.get("source_name", "") or split.get("destination_name", ""),
                "currency": split.get("currency_code", "AED"),
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
    "Last 3M": (today.replace(day=1) - relativedelta(months=2), today),
    "YTD": (today.replace(month=1, day=1), today),
    "Last 12M": (today - relativedelta(months=12), today),
}

# Initialise session state for date range
if "db_start" not in st.session_state:
    st.session_state.db_start = today.replace(day=1)
if "db_end" not in st.session_state:
    st.session_state.db_end = today
if "db_preset" not in st.session_state:
    st.session_state.db_preset = "This Month"

preset_cols = st.columns(len(PRESETS) + 4)
for i, (label, (ps, pe)) in enumerate(PRESETS.items()):
    is_active = st.session_state.db_preset == label
    if preset_cols[i].button(label, type="primary" if is_active else "secondary", use_container_width=True):
        st.session_state.db_start = ps
        st.session_state.db_end = pe
        st.session_state.db_preset = label
        st.rerun()

fcol1, fcol2, fcol3 = st.columns(3)
with fcol1:
    start_date = st.date_input("From", key="db_start")
with fcol2:
    end_date = st.date_input("To", key="db_end")

try:
    raw_accounts = get_accounts()
    account_map = {"All accounts": None}
    account_map.update({a["attributes"]["name"]: a["id"] for a in raw_accounts})
except Exception:
    account_map = {"All accounts": None}

with fcol3:
    selected_account_name = st.selectbox("Account", list(account_map.keys()))

account_id = account_map[selected_account_name]
start_str = start_date.isoformat()
end_str = end_date.isoformat()

# ── Load current-period data ──────────────────────────────────────────────────
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

# ── KPI calculations ──────────────────────────────────────────────────────────
total_spend = debits["amount"].sum()
total_income = credits["amount"].sum()
net = total_income - total_spend
n_days = max((end_date - start_date).days, 1)
avg_daily = total_spend / n_days
n_txns = len(debits)

if not debits.empty:
    cat_totals = debits.groupby("category")["amount"].sum()
    meaningful_cats = cat_totals[~cat_totals.index.isin(["Other", "Uncategorised", ""])]
    top_cat = (meaningful_cats if not meaningful_cats.empty else cat_totals).idxmax()
else:
    top_cat = "—"

# ── KPI row ───────────────────────────────────────────────────────────────────
st.markdown("---")
k1, k2, k3, k4, k5, k6 = st.columns(6)
k1.metric("Total Spend", f"AED {total_spend:,.0f}")
k2.metric("Total Income", f"AED {total_income:,.0f}")
k3.metric("Net", f"AED {net:,.0f}")
k4.metric("Avg / Day", f"AED {avg_daily:,.0f}")
k5.metric("Transactions", f"{n_txns:,}")
k6.metric("Top Category", top_cat)
st.markdown("---")

# ── Row 1: Category donut (left) + Top 10 transactions (right) ────────────────
r1c1, r1c2 = st.columns([3, 2])

with r1c1:
    st.subheader("Spend by category")
    if not debits.empty:
        cat_df = (
            debits.groupby("category")["amount"].sum()
            .reset_index()
            .sort_values("amount", ascending=False)
        )
        # Collapse tail into "Other" so the donut stays readable
        TOP_N = 8
        if len(cat_df) > TOP_N:
            other_sum = cat_df.iloc[TOP_N:]["amount"].sum()
            cat_df = pd.concat(
                [cat_df.head(TOP_N), pd.DataFrame([{"category": "Other", "amount": other_sum}])],
                ignore_index=True,
            )
        fig = px.pie(
            cat_df, values="amount", names="category", hole=0.45,
            color_discrete_sequence=CAT_COLORS,
        )
        fig.update_traces(
            textposition="inside",
            textinfo="percent+label",
            hovertemplate="<b>%{label}</b><br>AED %{value:,.2f}  (%{percent})<extra></extra>",
        )
        fig.update_layout(
            showlegend=True,
            legend=dict(orientation="v", x=1.02, y=0.5),
            margin=dict(l=0, r=130, t=10, b=10),
            height=350,
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No debit transactions.")

with r1c2:
    st.subheader("Top 10 transactions")
    if not debits.empty:
        top_df = (
            debits[["date", "description", "amount", "category"]]
            .sort_values("amount", ascending=False)
            .head(10)
            .copy()
        )
        top_df["date"] = top_df["date"].dt.strftime("%d %b")
        top_df["description"] = top_df["description"].str[:32]
        top_df["amount"] = top_df["amount"].map(lambda x: f"AED {x:,.0f}")
        top_df.columns = ["Date", "Description", "Amount", "Category"]
        st.dataframe(top_df, use_container_width=True, hide_index=True, height=350)
    else:
        st.info("No transactions.")

st.markdown("---")

# ── Row 2: Daily spend with rolling avg (left) + Day-of-week (right) ──────────
r2c1, r2c2 = st.columns(2)

with r2c1:
    st.subheader("Daily spend")
    if not debits.empty:
        daily_s = debits.groupby("date")["amount"].sum()
        date_range = pd.date_range(daily_s.index.min(), daily_s.index.max())
        daily = daily_s.reindex(date_range, fill_value=0).reset_index()
        daily.columns = ["date", "amount"]
        daily["7d_avg"] = daily["amount"].rolling(7, min_periods=1).mean()

        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=daily["date"], y=daily["amount"],
            name="Daily", marker_color=COLORS["bar"],
        ))
        fig.add_trace(go.Scatter(
            x=daily["date"], y=daily["7d_avg"],
            name="7-day avg", mode="lines",
            line=dict(color=COLORS["ma"], width=2.5),
        ))
        fig.update_layout(
            yaxis_title="AED", xaxis_title="", height=320,
            margin=dict(l=0, r=0, t=10, b=0),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            hovermode="x unified", bargap=0.2,
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No debit transactions.")

with r2c2:
    st.subheader("Spend by day of week")
    if not debits.empty:
        DOW_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        dow_df = debits.copy()
        dow_df["dow"] = dow_df["date"].dt.day_name()
        dow_agg = (
            dow_df.groupby("dow")["amount"].sum()
            .reindex(DOW_ORDER, fill_value=0)
            .reset_index()
        )
        dow_agg.columns = ["Day", "Amount"]

        fig = px.bar(
            dow_agg, x="Amount", y="Day", orientation="h",
            color="Amount", color_continuous_scale="Blues",
            text=dow_agg["Amount"].map(lambda x: f"AED {x:,.0f}"),
        )
        fig.update_traces(textposition="outside")
        fig.update_layout(
            xaxis_title="AED", yaxis_title="", height=320,
            margin=dict(l=0, r=90, t=10, b=0),
            coloraxis_showscale=False,
            yaxis=dict(categoryorder="array", categoryarray=DOW_ORDER[::-1]),
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No debit transactions.")

st.markdown("---")

# ── Load 6-month history in a single API call ─────────────────────────────────
six_months_start = (today.replace(day=1) - relativedelta(months=5)).isoformat()
try:
    df_6m = _load_transactions(six_months_start, today.isoformat(), account_id)
    df_6m["month_ts"] = df_6m["date"].dt.to_period("M").dt.to_timestamp()
    df_6m["month_label"] = df_6m["date"].dt.strftime("%b %Y")
    have_6m = not df_6m.empty
except Exception:
    have_6m = False

# ── Row 3: MoM income vs expense — full width ─────────────────────────────────
st.subheader("Month-over-month — last 6 months")
if have_6m:
    mom_spend = (
        df_6m[df_6m["type"] == "withdrawal"]
        .groupby(["month_ts", "month_label"])["amount"].sum()
        .reset_index().rename(columns={"amount": "Spend"})
    )
    mom_income = (
        df_6m[df_6m["type"] == "deposit"]
        .groupby(["month_ts", "month_label"])["amount"].sum()
        .reset_index().rename(columns={"amount": "Income"})
    )
    mom = mom_spend.merge(mom_income, on=["month_ts", "month_label"], how="outer").fillna(0)
    mom = mom.sort_values("month_ts")

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=mom["month_label"], y=mom["Spend"], name="Spend",
        marker_color=COLORS["spend"],
        text=mom["Spend"].map(lambda x: f"{x:,.0f}"), textposition="outside",
    ))
    fig.add_trace(go.Bar(
        x=mom["month_label"], y=mom["Income"], name="Income",
        marker_color=COLORS["income"],
        text=mom["Income"].map(lambda x: f"{x:,.0f}"), textposition="outside",
    ))
    fig.update_layout(
        barmode="group", yaxis_title="AED", xaxis_title="", height=320,
        margin=dict(l=0, r=0, t=30, b=0),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("Not enough data for month-over-month view.")

st.markdown("---")

# ── Row 4: Category trend (left) + Spend by account (right) ──────────────────
r4c1, r4c2 = st.columns(2)

with r4c1:
    st.subheader("Top category trends — last 6 months")
    if have_6m:
        debits_6m = df_6m[df_6m["type"] == "withdrawal"].copy()
        if not debits_6m.empty:
            # Pick the top 5 meaningful categories across the 6-month window
            cat_totals_6m = debits_6m.groupby("category")["amount"].sum()
            meaningful_6m = cat_totals_6m[~cat_totals_6m.index.isin(["Other", "Uncategorised", ""])]
            top5 = (meaningful_6m if not meaningful_6m.empty else cat_totals_6m).nlargest(5).index.tolist()

            ct = (
                debits_6m[debits_6m["category"].isin(top5)]
                .groupby(["month_ts", "month_label", "category"])["amount"]
                .sum()
                .reset_index()
                .sort_values("month_ts")
            )
            fig = px.line(
                ct, x="month_label", y="amount", color="category",
                markers=True, color_discrete_sequence=CAT_COLORS,
            )
            fig.update_layout(
                yaxis_title="AED", xaxis_title="", height=300,
                margin=dict(l=0, r=0, t=10, b=0),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No spend data in the last 6 months.")
    else:
        st.info("Not enough data.")

with r4c2:
    st.subheader("Spend by account")
    if not debits.empty:
        acct_df = (
            debits.groupby("account")["amount"].sum()
            .reset_index()
            .sort_values("amount")
        )
        acct_df.columns = ["Account", "Amount"]
        acct_df["Account"] = acct_df["Account"].str[:30]

        fig = px.bar(
            acct_df, x="Amount", y="Account", orientation="h",
            color="Amount", color_continuous_scale="Purples",
            text=acct_df["Amount"].map(lambda x: f"AED {x:,.0f}"),
        )
        fig.update_traces(textposition="outside")
        fig.update_layout(
            xaxis_title="AED", yaxis_title="", height=300,
            margin=dict(l=0, r=90, t=10, b=0),
            coloraxis_showscale=False,
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No data.")
