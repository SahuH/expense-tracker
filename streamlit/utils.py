import streamlit as st


def apply_theme():
    """Inject shared CSS applied on every page."""
    st.markdown(
        """
        <style>
        /* ── Metric cards: subtle border + background ───────────────────── */
        [data-testid="metric-container"] {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 10px;
            padding: 16px 20px !important;
        }

        /* ── Round all standard buttons ─────────────────────────────────── */
        .stButton > button,
        [data-testid="stDownloadButton"] > button,
        [data-testid="stBaseButton-secondary"],
        [data-testid="stBaseButton-primary"] {
            border-radius: 8px;
        }

        /* ── Status / expander: rounded corners ─────────────────────────── */
        [data-testid="stStatus"],
        [data-testid="stExpander"] {
            border-radius: 8px;
        }

        /* ── Containers with border: rounded ────────────────────────────── */
        [data-testid="stVerticalBlockBorderWrapper"] > div {
            border-radius: 10px;
        }

        /* ── Tighten chat input bottom gap ──────────────────────────────── */
        [data-testid="stChatInput"] {
            padding-bottom: 0.4rem;
        }

        /* ── Dataframe: rounder header ──────────────────────────────────── */
        [data-testid="stDataFrameResizable"] {
            border-radius: 8px;
            overflow: hidden;
        }

        /* ── Hide Streamlit's auto-generated sidebar page list ──────────── */
        [data-testid="stSidebarNavItems"],
        [data-testid="stSidebarNavSeparator"] {
            display: none !important;
        }

        /* ── Selectbox / multiselect dropdown: visible options ───────────── */
        [data-baseweb="popover"] [data-baseweb="menu"] {
            background-color: #1E2130 !important;
            border: 1px solid rgba(129,140,248,0.25) !important;
            border-radius: 8px !important;
        }

        [data-baseweb="popover"] li {
            background-color: #1E2130 !important;
            color: #F3F4F6 !important;
        }

        [data-baseweb="popover"] li:hover,
        [data-baseweb="popover"] li[aria-selected="true"] {
            background-color: #2D3155 !important;
            color: #FFFFFF !important;
        }

        /* ── Selectbox input box itself ──────────────────────────────────── */
        [data-baseweb="select"] > div:first-child {
            background-color: #1A1B2E !important;
            border-color: rgba(129,140,248,0.35) !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def step_indicator(steps: list, current: int):
    """
    Render a horizontal step-progress bar above a workflow section.
    steps  — list of short step labels
    current — 1-indexed step that is currently active
    """
    parts = []
    for i, label in enumerate(steps):
        n = i + 1
        if n < current:
            color, weight, marker = "#00cc96", "600", "✓"
        elif n == current:
            color, weight, marker = "#818CF8", "700", str(n)
        else:
            color, weight, marker = "#6B7280", "400", str(n)

        parts.append(
            f'<span style="color:{color};font-weight:{weight};'
            f'white-space:nowrap">{marker}&thinsp;{label}</span>'
        )

    sep = '&nbsp;<span style="color:#374151;margin:0 5px">—</span>&nbsp;'
    st.markdown(
        '<div style="display:flex;flex-wrap:wrap;align-items:center;'
        f'gap:2px;margin:6px 0 18px 0;font-size:0.83rem">{sep.join(parts)}</div>',
        unsafe_allow_html=True,
    )
