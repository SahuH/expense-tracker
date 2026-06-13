import streamlit as st
import requests
import pandas as pd
import os

from auth import require_login
from sidebar import render_sidebar
from utils import apply_theme

st.set_page_config(
    page_title="Transfer Rules — Household Finance",
    page_icon="💰",
    layout="wide",
)

apply_theme()

_, _, authentication_status = require_login()
if not authentication_status:
    st.stop()

render_sidebar()

PARSER_URL = os.getenv("PARSER_URL", "http://parser:8000")

st.title("Transfer Rules")
st.caption(
    "Rules that control how transactions are classified during import. "
    "**Built-in** rules are hard-coded for known account patterns. "
    "**Custom** rules are added via the Upload page and take priority. "
    "Both sets are applied automatically on every import."
)

# ── Fetch effective rules ──────────────────────────────────────────────────────
try:
    _resp = requests.get(f"{PARSER_URL}/rules/effective", timeout=5)
    _effective: dict = _resp.json().get("rules", {}) if _resp.ok else {}
except Exception as _exc:
    st.error(f"Could not reach parser: {_exc}")
    st.stop()

# ── Build flat display table ───────────────────────────────────────────────────
_rows = []
for _account, _rules in _effective.items():
    for _rule in _rules:
        _action = _rule.get("action", "transfer")
        _match = _rule.get("match", "contains")
        _origin = _rule.get("_origin", "builtin")
        _rows.append({
            "Account": _account,
            "Keyword": _rule.get("keyword", ""),
            "Match": _match,
            "Action": "Transfer to →" if _action != "skip" else "Skip (drop)",
            "Counterpart account": _rule.get("other_account", "—"),
            "Origin": "Custom" if _origin == "user" else "Built-in",
        })

if _rows:
    _df = pd.DataFrame(_rows)

    # Tabs: one per account, plus an "All" tab
    _accounts = list(_effective.keys())
    _tab_labels = ["All"] + _accounts
    _tabs = st.tabs(_tab_labels)

    with _tabs[0]:
        st.dataframe(
            _df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Account":            st.column_config.TextColumn("Account"),
                "Keyword":            st.column_config.TextColumn("Keyword"),
                "Match":              st.column_config.TextColumn("Match", width="small"),
                "Action":             st.column_config.TextColumn("Action"),
                "Counterpart account":st.column_config.TextColumn("Counterpart account"),
                "Origin":             st.column_config.TextColumn("Origin", width="small"),
            },
        )

    for _tab, _acct in zip(_tabs[1:], _accounts):
        with _tab:
            _acct_df = _df[_df["Account"] == _acct].drop(columns=["Account"])
            st.dataframe(
                _acct_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Keyword":            st.column_config.TextColumn("Keyword"),
                    "Match":              st.column_config.TextColumn("Match", width="small"),
                    "Action":             st.column_config.TextColumn("Action"),
                    "Counterpart account":st.column_config.TextColumn("Counterpart account"),
                    "Origin":             st.column_config.TextColumn("Origin", width="small"),
                },
            )
else:
    st.info("No rules configured. Add rules on the Upload page.")

# ── Apply rules to existing transactions ──────────────────────────────────────
st.markdown("---")
st.subheader("Apply rules to existing Firefly transactions")
st.markdown(
    "This scans **all existing withdrawals and deposits** in Firefly and applies the rules above:\n"
    "- **Transfer** rules convert matching transactions to internal Firefly transfers.\n"
    "- **Skip** rules delete the destination-side duplicate (the source side becomes the transfer).\n\n"
    "Run this once after adding new rules or after the initial import. "
    "It is safe to run multiple times — already-converted transfers are not re-processed."
)

_col_btn, _col_info = st.columns([2, 6])
if _col_btn.button("Apply rules now", type="primary", use_container_width=True):
    with st.spinner("Scanning Firefly transactions and applying rules… this may take a minute."):
        try:
            _apply_resp = requests.post(f"{PARSER_URL}/rules/apply", timeout=300)
            _apply_resp.raise_for_status()
            _result = _apply_resp.json()
        except Exception as _exc:
            st.error(f"Apply failed: {_exc}")
            st.stop()

    if _result.get("success"):
        _updated = _result.get("updated_count", 0)
        _deleted = _result.get("deleted_count", 0)
        st.success(
            f"Done! Converted **{_updated}** transactions to transfers "
            f"and removed **{_deleted}** destination-side duplicates."
        )
    else:
        _updated = _result.get("updated_count", 0)
        _deleted = _result.get("deleted_count", 0)
        _errs = _result.get("errors", [])
        st.warning(
            f"Completed with errors — converted {_updated}, removed {_deleted}, "
            f"{_result.get('error_count', 0)} failed."
        )
        with st.expander("Error details"):
            for _e in _errs:
                st.text(_e)
