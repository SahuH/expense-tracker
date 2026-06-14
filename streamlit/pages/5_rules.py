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
    "**Custom** rules take priority and can be edited below."
)

# ── Fetch effective rules ──────────────────────────────────────────────────────
try:
    _resp = requests.get(f"{PARSER_URL}/rules/effective", timeout=5)
    _effective: dict = _resp.json().get("rules", {}) if _resp.ok else {}
except Exception as _exc:
    st.error(f"Could not reach parser: {_exc}")
    st.stop()

# ── Split into user vs built-in ────────────────────────────────────────────────
_user_rows = []
_builtin_rows = []

for _account, _rules_list in _effective.items():
    for _rule in _rules_list:
        _action = _rule.get("action", "transfer")
        _match = _rule.get("match", "contains")
        _origin = _rule.get("_origin", "builtin")
        _row = {
            "Account": _account,
            "Keyword": _rule.get("keyword", ""),
            "Match": _match,
            "Action": "Transfer to →" if _action != "skip" else "Skip (drop)",
            "Counterpart account": _rule.get("other_account", ""),
        }
        if _origin == "user":
            _user_rows.append(_row)
        else:
            _builtin_rows.append(_row)

_COLS = ["Account", "Keyword", "Match", "Action", "Counterpart account"]
_user_df = pd.DataFrame(_user_rows, columns=_COLS) if _user_rows else pd.DataFrame(columns=_COLS)
_builtin_df = pd.DataFrame(_builtin_rows) if _builtin_rows else pd.DataFrame(columns=_COLS)

# ── Custom rules editor ────────────────────────────────────────────────────────
st.subheader("Custom Rules")
st.caption(
    "Edit cells directly, add rows with the **+** button at the bottom, "
    "or delete rows by selecting them and pressing **Delete**. "
    "Click **Save changes** to persist."
)

_edited_user_df = st.data_editor(
    _user_df,
    use_container_width=True,
    hide_index=True,
    num_rows="dynamic",
    column_config={
        "Account":             st.column_config.TextColumn("Account", width="medium"),
        "Keyword":             st.column_config.TextColumn("Keyword", width="medium"),
        "Match":               st.column_config.SelectboxColumn("Match", options=["contains", "exact"], width="small"),
        "Action":              st.column_config.SelectboxColumn("Action", options=["Transfer to →", "Skip (drop)"], width="medium"),
        "Counterpart account": st.column_config.TextColumn("Counterpart account", width="large"),
    },
)

# Detect changes (row count or content differs)
_original_records = _user_df.fillna("").to_dict("records")
_edited_records   = _edited_user_df.fillna("").to_dict("records")
_changed = _original_records != _edited_records

_btn_col, _info_col = st.columns([1, 4])
with _btn_col:
    _save_clicked = st.button("Save changes", type="primary", disabled=not _changed, key="save_custom_rules")
with _info_col:
    if _changed:
        st.caption("Unsaved changes detected — click Save to persist.")

if _save_clicked:
    _new_rules: dict = {}
    _errors: list = []

    for _, _row in _edited_user_df.iterrows():
        _acct = (_row.get("Account") or "").strip()
        _kw   = (_row.get("Keyword") or "").strip()
        if not _acct or not _kw:
            continue  # skip blank/incomplete rows
        _is_skip    = _row.get("Action", "Transfer to →") == "Skip (drop)"
        _counterpart = (_row.get("Counterpart account") or "").strip()
        if not _is_skip and not _counterpart:
            _errors.append(f"'{_kw}' (account: {_acct}): Counterpart account is required for Transfer rules.")
            continue
        _entry: dict = {"keyword": _kw}
        if (_row.get("Match") or "contains") == "exact":
            _entry["match"] = "exact"
        if _is_skip:
            _entry["action"] = "skip"
        else:
            _entry["other_account"] = _counterpart
        _new_rules.setdefault(_acct, []).append(_entry)

    if _errors:
        for _e in _errors:
            st.error(_e)
    else:
        try:
            _put_resp = requests.put(
                f"{PARSER_URL}/rules/user",
                json={"rules": _new_rules},
                timeout=10,
            )
            _put_resp.raise_for_status()
            st.success("Custom rules saved.")
            st.rerun()
        except Exception as _exc:
            st.error(f"Save failed: {_exc}")

# ── Built-in rules (read-only) ─────────────────────────────────────────────────
st.markdown("---")
st.subheader("Built-in Rules")
st.caption("These are hard-coded for known account patterns and cannot be modified from the UI.")

if not _builtin_df.empty:
    _builtin_accounts = sorted(_builtin_df["Account"].unique().tolist())
    _tab_labels = ["All"] + _builtin_accounts
    _tabs = st.tabs(_tab_labels)

    _col_cfg = {
        "Account":             st.column_config.TextColumn("Account"),
        "Keyword":             st.column_config.TextColumn("Keyword"),
        "Match":               st.column_config.TextColumn("Match", width="small"),
        "Action":              st.column_config.TextColumn("Action"),
        "Counterpart account": st.column_config.TextColumn("Counterpart account"),
    }

    with _tabs[0]:
        st.dataframe(_builtin_df, use_container_width=True, hide_index=True, column_config=_col_cfg)

    for _tab, _acct in zip(_tabs[1:], _builtin_accounts):
        with _tab:
            _acct_df = _builtin_df[_builtin_df["Account"] == _acct].drop(columns=["Account"])
            st.dataframe(_acct_df, use_container_width=True, hide_index=True, column_config={
                k: v for k, v in _col_cfg.items() if k != "Account"
            })
else:
    st.info("No built-in rules found.")

# ── Apply rules to existing transactions ──────────────────────────────────────
st.markdown("---")
st.subheader("Apply rules to existing Firefly transactions")
st.markdown(
    "This scans **all existing withdrawals and deposits** in Firefly and applies the rules above:\n"
    "- **Transfer** rules convert matching transactions to internal Firefly transfers.\n"
    "- **Skip** rules delete the destination-side duplicate.\n\n"
    "Safe to run multiple times — already-converted transfers are not re-processed."
)

_col_btn, _ = st.columns([2, 6])
if _col_btn.button("Apply rules now", type="primary", use_container_width=True):
    with st.spinner("Scanning Firefly transactions and applying rules… this may take a minute."):
        try:
            _apply_resp = requests.post(f"{PARSER_URL}/rules/apply", timeout=300)
            _apply_resp.raise_for_status()
            _result = _apply_resp.json()
        except Exception as _exc:
            st.error(f"Apply failed: {_exc}")
            st.stop()

    _updated = _result.get("updated_count", 0)
    _deleted = _result.get("deleted_count", 0)
    if _result.get("success"):
        st.success(
            f"Done! Converted **{_updated}** transactions to transfers "
            f"and removed **{_deleted}** destination-side duplicates."
        )
    else:
        st.warning(
            f"Completed with errors — converted {_updated}, removed {_deleted}, "
            f"{_result.get('error_count', 0)} failed."
        )
        with st.expander("Error details"):
            for _e in _result.get("errors", []):
                st.text(_e)
