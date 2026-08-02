import streamlit as st


def render_sidebar():
    """
    Render a consistent sidebar on every page: welcome message, logout button,
    and page navigation links.

    Reuses the stauth.Authenticate instance stored by require_login() so that
    only one CookieManager widget is created per render (avoids DuplicateWidgetID).
    """
    name = st.session_state.get("name") or "User"

    st.sidebar.markdown(f"**👋 {name}**")

    authenticator = st.session_state.get("_authenticator")
    if authenticator:
        authenticator.logout("Logout", "sidebar")

    st.sidebar.divider()

    st.sidebar.page_link("app.py",                  label="🏠  Home")
    st.sidebar.page_link("pages/1_upload.py",        label="📥  Upload & Import")
    st.sidebar.page_link("pages/2_dashboard.py",     label="📊  Dashboard")
    st.sidebar.page_link("pages/3_transactions.py",  label="📋  Transactions")
    st.sidebar.page_link("pages/4_ask_ai.py",        label="🤖  Ask AI")
    st.sidebar.page_link("pages/5_rules.py",         label="🔄  Transfers")
    st.sidebar.page_link("pages/6_categories.py",    label="🏷️  Categories")
