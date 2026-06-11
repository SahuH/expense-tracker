import streamlit as st
import streamlit_authenticator as stauth
import yaml
from yaml.loader import SafeLoader
import os

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
COOKIE_EXPIRY_DAYS = 7


def require_login():
    with open(CONFIG_PATH) as f:
        config = yaml.load(f, Loader=SafeLoader)

    authenticator = stauth.Authenticate(
        config["credentials"],
        config["cookie"]["name"],
        config["cookie"]["key"],
        config["cookie"]["expiry_days"],
    )

    authenticator.login(location="main")

    name = st.session_state.get("name")
    authentication_status = st.session_state.get("authentication_status")

    if authentication_status is False:
        st.error("Incorrect username or password")
    elif authentication_status is None:
        st.warning("Please enter your credentials")

    return authenticator, name, authentication_status
