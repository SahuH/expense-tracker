import streamlit as st
from auth import require_login

st.set_page_config(
    page_title="Household Finance",
    page_icon="💰",
    layout="wide",
    initial_sidebar_state="expanded",
)

authenticator, name, authentication_status = require_login()

if not authentication_status:
    st.stop()

# Sidebar nav
st.sidebar.title(f"Welcome, {name}")
authenticator.logout("Logout", "sidebar")

st.sidebar.markdown("---")
st.sidebar.markdown("### Navigation")
st.sidebar.page_link("pages/1_upload.py", label="Upload & Import")
st.sidebar.page_link("pages/2_dashboard.py", label="Dashboard")
st.sidebar.page_link("pages/3_transactions.py", label="Transactions")
st.sidebar.page_link("pages/4_ask_ai.py", label="Ask AI")

# Landing page
st.title("Household Finance Tracker")
st.markdown(
    """
    Welcome to your personal finance dashboard. Use the sidebar to navigate:

    - **Upload & Import** — Parse and import bank statements
    - **Dashboard** — Spending charts and KPIs
    - **Transactions** — Search and filter all transactions
    - **Ask AI** — Natural-language finance questions
    """
)
