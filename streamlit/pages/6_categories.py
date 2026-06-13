import streamlit as st
import requests
import os
from collections import defaultdict

from auth import require_login
from sidebar import render_sidebar
from utils import apply_theme

st.set_page_config(
    page_title="Categories — Household Finance",
    page_icon="💰",
    layout="wide",
)

apply_theme()

_, _, authentication_status = require_login()
if not authentication_status:
    st.stop()

render_sidebar()

PARSER_URL = os.getenv("PARSER_URL", "http://parser:8000")

st.title("Categories & Rules")
st.caption(
    "Rules that auto-assign a category to a transaction when its description contains a pattern. "
    "Each pattern is exclusive — it can only appear in one rule."
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fetch_rules() -> list:
    try:
        r = requests.get(f"{PARSER_URL}/category-rules", timeout=5)
        return r.json().get("rules", []) if r.ok else []
    except Exception:
        return []


def _conflict_msg(detail) -> str:
    if isinstance(detail, dict):
        conflicts = detail.get("conflicts", [])
        if conflicts:
            lines = [f"**{c['pattern']}** — already used in _{c['rule']}_" for c in conflicts]
            return "Pattern conflict:\n" + "\n".join(f"- {l}" for l in lines)
    return str(detail)


# ── Load rules ────────────────────────────────────────────────────────────────
rules = _fetch_rules()
grouped: dict[str, list] = defaultdict(list)
for rule in rules:
    grouped[rule.get("category", "Uncategorised")].append(rule)
total_patterns = sum(len(r.get("patterns", [])) for r in rules)

# ── Top bar: stats + apply button ─────────────────────────────────────────────
stat_col, _, apply_col = st.columns([3, 3, 2])
with stat_col:
    st.markdown(
        f"**{len(rules)}** rules &nbsp;·&nbsp; **{total_patterns}** patterns &nbsp;·&nbsp; "
        f"**{len(grouped)}** top-level categories"
    )
with apply_col:
    if st.button("Apply rules to all transactions", type="primary", use_container_width=True):
        with st.spinner("Scanning Firefly transactions and applying categories…"):
            try:
                r = requests.post(f"{PARSER_URL}/category-rules/apply", timeout=300)
                result = r.json() if r.ok else {"success": False, "errors": [r.text]}
            except Exception as exc:
                result = {"success": False, "errors": [str(exc)]}

        if result.get("success"):
            st.success(
                f"Done! Updated **{result.get('updated_count', 0)}** transactions "
                f"({result.get('skipped_count', 0)} already categorised or no match)."
            )
            counts = result.get("category_counts", {})
            if counts:
                with st.expander("Breakdown by category"):
                    for cat, n in sorted(counts.items(), key=lambda x: -x[1]):
                        st.markdown(f"- **{cat}**: {n}")
        else:
            errs = result.get("errors", [])
            st.warning(
                f"Completed with errors — "
                f"{result.get('updated_count', 0)} updated, "
                f"{result.get('error_count', 0)} failed."
            )
            with st.expander("Error details"):
                for e in errs:
                    st.text(e)
        st.rerun()

st.markdown("---")

# ── Add new rule ──────────────────────────────────────────────────────────────
with st.expander("+ Add new rule", expanded=not rules):
    existing_cats = sorted(grouped.keys())
    cat_choice = st.selectbox(
        "Top-level category",
        ["— type below to create new —"] + existing_cats,
        key="new_rule_cat_sel",
    )
    new_cat = st.text_input(
        "Category name (leave blank to use selection above)",
        key="new_rule_cat_txt",
        placeholder="e.g. Food & Dining",
    )
    new_subcat = st.text_input(
        "Subcategory (optional)",
        key="new_rule_subcat",
        placeholder="e.g. Restaurants",
    )
    new_pattern = st.text_input(
        "Initial pattern (contains, case-insensitive)",
        key="new_rule_pattern",
        placeholder="e.g. carrefour",
    )

    if st.button("Save rule", type="primary"):
        category = (new_cat.strip() or
                    (cat_choice if cat_choice != "— type below to create new —" else ""))
        if not category:
            st.error("Category name is required.")
        elif not new_pattern.strip():
            st.error("At least one pattern is required.")
        else:
            try:
                resp = requests.post(
                    f"{PARSER_URL}/category-rules",
                    json={
                        "category": category,
                        "subcategory": new_subcat.strip(),
                        "patterns": [new_pattern.strip().lower()],
                    },
                    timeout=10,
                )
                if resp.ok:
                    st.success(f"Rule added: **{category}** › **{new_subcat or '—'}**")
                    st.rerun()
                elif resp.status_code == 409:
                    st.error(_conflict_msg(resp.json().get("detail")))
                else:
                    st.error(f"Error {resp.status_code}: {resp.text}")
            except Exception as exc:
                st.error(f"Could not reach parser: {exc}")

st.markdown("---")

# ── Rules list ────────────────────────────────────────────────────────────────
if not rules:
    st.info("No category rules yet. Add your first rule above.")
    st.stop()

for top_cat, cat_rules in sorted(grouped.items()):
    n_patterns = sum(len(r.get("patterns", [])) for r in cat_rules)
    with st.expander(f"**{top_cat}** — {len(cat_rules)} subcategories, {n_patterns} patterns", expanded=True):

        for rule in cat_rules:
            rule_id = rule["id"]
            subcat = rule.get("subcategory") or "—"
            patterns = rule.get("patterns", [])

            rc1, rc2 = st.columns([5, 1])
            rc1.markdown(f"**{subcat}**")
            if rc2.button("Delete rule", key=f"del_rule_{rule_id}", help="Remove this rule and all its patterns"):
                try:
                    requests.delete(f"{PARSER_URL}/category-rules/{rule_id}", timeout=5)
                    st.rerun()
                except Exception as exc:
                    st.error(f"Error: {exc}")

            # Show patterns as chips with individual delete buttons
            pat_cols = st.columns(min(len(patterns) + 1, 6))
            for idx, pat in enumerate(patterns):
                col = pat_cols[idx % 5]
                col.markdown(
                    f'<span style="background:rgba(129,140,248,0.15);border:1px solid rgba(129,140,248,0.3);'
                    f'border-radius:6px;padding:2px 8px;font-size:0.82rem">{pat}</span>',
                    unsafe_allow_html=True,
                )
                if col.button("×", key=f"del_pat_{rule_id}_{idx}", help=f"Remove pattern '{pat}'"):
                    try:
                        requests.delete(
                            f"{PARSER_URL}/category-rules/{rule_id}/patterns/{idx}",
                            timeout=5,
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Error: {exc}")

            # Add pattern to this rule
            with st.form(key=f"add_pat_{rule_id}"):
                fc1, fc2 = st.columns([4, 1])
                new_p = fc1.text_input(
                    "Add pattern",
                    placeholder="e.g. mcdonald",
                    label_visibility="collapsed",
                )
                submitted = fc2.form_submit_button("Add", use_container_width=True)
                if submitted and new_p.strip():
                    try:
                        r = requests.post(
                            f"{PARSER_URL}/category-rules/{rule_id}/patterns",
                            json={"pattern": new_p.strip().lower()},
                            timeout=5,
                        )
                        if r.ok:
                            st.rerun()
                        elif r.status_code == 409:
                            st.error(_conflict_msg(r.json().get("detail")))
                        else:
                            st.error(f"Error {r.status_code}: {r.text}")
                    except Exception as exc:
                        st.error(f"Error: {exc}")

            st.markdown("<hr style='border-color:rgba(255,255,255,0.06);margin:8px 0'>", unsafe_allow_html=True)
