import streamlit as st
import anthropic
import psycopg2
import json
import os
import re
from datetime import date

from auth import require_login
from firefly_api import get_transactions, get_categories, get_accounts

_, name, authentication_status = require_login()
if not authentication_status:
    st.stop()

st.title("Ask AI")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
if not ANTHROPIC_API_KEY:
    st.info("Ask AI requires an Anthropic API key. Add `ANTHROPIC_API_KEY=sk-ant-...` to your `.env` file and restart the stack.")
    st.stop()
PG_HOST = os.getenv("POSTGRES_HOST", "postgres")
PG_PORT = os.getenv("POSTGRES_PORT", "5432")
PG_DB = os.getenv("POSTGRES_DB", "firefly")
PG_USER = os.getenv("POSTGRES_USER", "firefly")
PG_PASS = os.getenv("POSTGRES_PASSWORD", "")

SYSTEM_PROMPT = f"""You are a household finance assistant for a two-person household (Harsh and Wife).
You have access to transaction data via function tools.
Answer concisely and clearly.
Always quote amounts in AED unless the transaction was in a different currency.
Never make up data — only answer based on what the tools return.
Today's date is {date.today().isoformat()}.
"""

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

tools = [
    {
        "name": "get_transactions",
        "description": "Fetch transactions from Firefly III. Returns a list of transactions within a date range, optionally filtered by account.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "end": {"type": "string", "description": "End date YYYY-MM-DD"},
                "account_id": {"type": "string", "description": "Firefly account ID (optional)"},
                "limit": {"type": "integer", "description": "Max transactions to return", "default": 500},
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


def _run_tool(tool_name: str, tool_input: dict):
    if tool_name == "get_transactions":
        raw = get_transactions(**tool_input)
        rows = []
        for t in raw:
            for split in t.get("attributes", {}).get("transactions", []):
                rows.append(
                    {
                        "date": split.get("date", "")[:10],
                        "description": split.get("description", ""),
                        "amount": float(split.get("amount", 0)),
                        "type": split.get("type", ""),
                        "category": split.get("category_name") or "Uncategorised",
                        "account": split.get("source_name") or split.get("destination_name") or "",
                        "currency": split.get("currency_code", "AED"),
                    }
                )
        return rows

    elif tool_name == "query_postgres":
        sql = tool_input.get("sql", "")
        # Safety: only allow SELECT statements
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


def _chat(messages: list) -> tuple[str, list]:
    """
    Agentic loop: call Claude, run tools if needed, return final text + updated messages.
    Uses prompt caching on the system prompt.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=tools,
        messages=messages,
    )

    messages = messages + [{"role": "assistant", "content": response.content}]

    while response.stop_reason == "tool_use":
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = _run_tool(block.name, block.input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str),
                    }
                )

        messages = messages + [{"role": "user", "content": tool_results}]
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=tools,
            messages=messages,
        )
        messages = messages + [{"role": "assistant", "content": response.content}]

    final_text = ""
    for block in response.content:
        if hasattr(block, "text"):
            final_text += block.text

    return final_text, messages


# ── Session history ───────────────────────────────────────────────────────────
if "chat_messages" not in st.session_state:
    st.session_state.chat_messages = []
if "api_messages" not in st.session_state:
    st.session_state.api_messages = []

# Render history
for msg in st.session_state.chat_messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# ── Input ─────────────────────────────────────────────────────────────────────
if prompt := st.chat_input("Ask about your finances…"):
    st.session_state.chat_messages.append({"role": "user", "content": prompt})
    st.session_state.api_messages.append({"role": "user", "content": prompt})

    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Thinking…"):
            try:
                answer, updated_api_msgs = _chat(st.session_state.api_messages)
                st.session_state.api_messages = updated_api_msgs
            except Exception as exc:
                answer = f"Error: {exc}"

        st.markdown(answer)
        st.session_state.chat_messages.append({"role": "assistant", "content": answer})

if st.button("Clear conversation"):
    st.session_state.chat_messages = []
    st.session_state.api_messages = []
    st.rerun()
