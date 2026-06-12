import streamlit as st
import anthropic
import psycopg2
import json
import os
import re
from datetime import date

from auth import require_login
from sidebar import render_sidebar
from utils import apply_theme
from firefly_api import get_transactions, get_categories, get_accounts

st.set_page_config(
    page_title="Ask AI — Household Finance",
    page_icon="💰",
    layout="wide",
)

apply_theme()

_, name, authentication_status = require_login()
if not authentication_status:
    st.stop()

render_sidebar()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
if not ANTHROPIC_API_KEY:
    st.info("Ask AI requires an Anthropic API key. Add `ANTHROPIC_API_KEY=sk-ant-...` to your `.env` file and restart the stack.")
    st.stop()

PG_HOST = os.getenv("POSTGRES_HOST", "postgres")
PG_PORT = os.getenv("POSTGRES_PORT", "5432")
PG_DB   = os.getenv("POSTGRES_DB",   "firefly")
PG_USER = os.getenv("POSTGRES_USER", "firefly")
PG_PASS = os.getenv("POSTGRES_PASSWORD", "")

SYSTEM_PROMPT = f"""You are a household finance assistant for a two-person household (Harsh and Wife).
You have access to transaction data via function tools.
Answer concisely and clearly. Use markdown formatting where helpful (tables, bullet points).
Always quote amounts in AED unless the transaction was in a different currency.
Never make up data — only answer based on what the tools return.
Today's date is {date.today().isoformat()}.
"""

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

tools = [
    {
        "name": "get_transactions",
        "description": "Fetch transactions from Firefly III within a date range, optionally filtered by account.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start":      {"type": "string",  "description": "Start date YYYY-MM-DD"},
                "end":        {"type": "string",  "description": "End date YYYY-MM-DD"},
                "account_id": {"type": "string",  "description": "Firefly account ID (optional)"},
                "limit":      {"type": "integer", "description": "Max transactions to return", "default": 500},
            },
            "required": ["start", "end"],
        },
    },
    {
        "name": "query_postgres",
        "description": "Run a read-only SQL SELECT query against the Firefly III PostgreSQL database for aggregations and analytics.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "A SELECT query. No DDL or DML allowed."},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "get_accounts",
        "description": "List all asset accounts with their IDs and names.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_categories",
        "description": "List all spending categories.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]

_TOOL_LABELS = {
    "get_transactions": "Fetching transactions from Firefly…",
    "query_postgres":   "Running database query…",
    "get_accounts":     "Loading account list…",
    "get_categories":   "Loading categories…",
}


def _run_tool(tool_name: str, tool_input: dict):
    if tool_name == "get_transactions":
        raw = get_transactions(**tool_input)
        rows = []
        for t in raw:
            for split in t.get("attributes", {}).get("transactions", []):
                rows.append({
                    "date":        split.get("date", "")[:10],
                    "description": split.get("description", ""),
                    "amount":      float(split.get("amount", 0)),
                    "type":        split.get("type", ""),
                    "category":    split.get("category_name") or "Uncategorised",
                    "account":     split.get("source_name") or split.get("destination_name") or "",
                    "currency":    split.get("currency_code", "AED"),
                })
        return rows

    elif tool_name == "query_postgres":
        sql = tool_input.get("sql", "")
        if not re.match(r"^\s*SELECT\b", sql, re.IGNORECASE):
            return {"error": "Only SELECT queries are permitted"}
        try:
            conn = psycopg2.connect(
                host=PG_HOST, port=PG_PORT, dbname=PG_DB,
                user=PG_USER, password=PG_PASS, connect_timeout=10,
            )
            conn.set_session(readonly=True)
            with conn.cursor() as cur:
                cur.execute(sql)
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchmany(500)]
            conn.close()
            return rows
        except Exception as exc:
            return {"error": str(exc)}

    elif tool_name == "get_accounts":
        raw = get_accounts()
        return [{"id": a["id"], "name": a["attributes"]["name"]} for a in raw]

    elif tool_name == "get_categories":
        raw = get_categories()
        return [{"id": c["id"], "name": c["attributes"]["name"]} for c in raw]

    return {"error": f"Unknown tool: {tool_name}"}


def _chat(messages: list, status=None) -> tuple[str, list]:
    """
    Agentic loop with optional st.status widget for live tool-call visibility.
    Uses prompt caching on the system prompt.
    """
    _system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=_system,
        tools=tools,
        messages=messages,
    )
    messages = messages + [{"role": "assistant", "content": response.content}]

    while response.stop_reason == "tool_use":
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            label = _TOOL_LABELS.get(block.name, f"Calling {block.name}…")
            if status:
                status.update(label=label, state="running", expanded=True)
                params_preview = json.dumps(block.input, default=str)
                if len(params_preview) > 120:
                    params_preview = params_preview[:120] + "…"
                status.write(f"`{block.name}` · {params_preview}")

            result = _run_tool(block.name, block.input)
            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,
                "content":     json.dumps(result, default=str),
            })

        if status:
            status.update(label="Analyzing results…", state="running", expanded=False)

        messages = messages + [{"role": "user", "content": tool_results}]
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=_system,
            tools=tools,
            messages=messages,
        )
        messages = messages + [{"role": "assistant", "content": response.content}]

    if status:
        status.update(label="Done", state="complete", expanded=False)

    final_text = "".join(block.text for block in response.content if hasattr(block, "text"))
    return final_text, messages


# ── Session state ─────────────────────────────────────────────────────────────
if "chat_messages" not in st.session_state:
    st.session_state.chat_messages = []
if "api_messages" not in st.session_state:
    st.session_state.api_messages = []

# ── Header row: title + clear button ─────────────────────────────────────────
_, main_col, _ = st.columns([1, 6, 1])

with main_col:
    hcol, bcol = st.columns([5, 1])
    with hcol:
        st.title("🤖 Ask AI")
        st.caption("Ask natural-language questions about your household finances.")
    with bcol:
        st.markdown("<div style='margin-top:2rem'></div>", unsafe_allow_html=True)
        if st.button("Clear chat", use_container_width=True):
            st.session_state.chat_messages = []
            st.session_state.api_messages  = []
            st.rerun()

    # ── Suggested prompts (shown only on an empty conversation) ───────────────
    SUGGESTED = [
        "How much did we spend this month?",
        "What's our top expense category this year?",
        "Compare this month's spending to last month",
        "Show our 5 biggest transactions in the last 3 months",
        "How much have we spent on dining so far this year?",
        "What is our average daily spend this year?",
    ]

    if not st.session_state.chat_messages:
        st.markdown("**Try asking:**")
        s_cols = st.columns(3)
        for i, prompt_text in enumerate(SUGGESTED):
            if s_cols[i % 3].button(prompt_text, key=f"sp_{i}", use_container_width=True):
                st.session_state["_pending_prompt"] = prompt_text
                st.rerun()

    # ── Chat history ──────────────────────────────────────────────────────────
    for msg in st.session_state.chat_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # ── Chat input + pending-prompt handling ──────────────────────────────────
    typed = st.chat_input("Ask about your finances…")
    incoming = typed or st.session_state.pop("_pending_prompt", None)

    if incoming:
        st.session_state.chat_messages.append({"role": "user", "content": incoming})
        st.session_state.api_messages.append( {"role": "user", "content": incoming})

        with st.chat_message("user"):
            st.markdown(incoming)

        with st.chat_message("assistant"):
            with st.status("Working on it…", expanded=True) as status:
                try:
                    answer, updated_msgs = _chat(st.session_state.api_messages, status)
                    st.session_state.api_messages = updated_msgs
                except Exception as exc:
                    answer = f"Sorry, something went wrong: {exc}"
                    status.update(label="Error", state="error")

            st.markdown(answer)
            st.session_state.chat_messages.append({"role": "assistant", "content": answer})
