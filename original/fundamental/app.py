import streamlit as st
import pandas as pd
from google.oauth2.service_account import Credentials
import gspread

st.set_page_config(page_title="Deep-Value Sentinel", layout="wide")

SHEET_ID = "1NcJyga0UZh-SYcr5hH0Xbq_-JYX9simpAsOzWh5PtCE"
SCOPE = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]


@st.cache_data(ttl=300)
def load_data():
    try:
        if "gcp_service_account" in st.secrets:
            creds = Credentials.from_service_account_info(dict(st.secrets["gcp_service_account"]), scopes=SCOPE)
        else:
            creds = Credentials.from_service_account_file("creds.json", scopes=SCOPE)
    except Exception:
        creds = Credentials.from_service_account_file("creds.json", scopes=SCOPE)

    gc = gspread.authorize(creds)
    return pd.DataFrame(gc.open_by_key(SHEET_ID).sheet1.get_all_records())


def show(frame):
    """Works on both old and new Streamlit versions."""
    try:
        st.dataframe(frame, width="stretch", hide_index=True)
    except Exception:
        st.dataframe(frame, use_container_width=True, hide_index=True)


def section(df, status, cols):
    cols = [c for c in cols if c in df.columns]
    subset = df[df["Status"] == status][cols]
    if subset.empty:
        st.caption("Nothing here right now.")
    else:
        show(subset)


st.title("🛡️ 2026 Deep-Value Sentinel")

try:
    df = load_data()
except Exception as e:
    st.error(f"Could not load data from Google Sheets: {e}")
    st.stop()

if df.empty or "Status" not in df.columns:
    st.warning("The sheet has no scan data yet. Run the scanner first.")
    st.stop()

if "Updated" in df.columns and len(df):
    st.caption(f"Last scan: {df['Updated'].iloc[0]}")

basic = ["Ticker", "Market Cap", "Sector", "Current price"]
trade = ["Ticker", "Sector", "Current price", "Drawdown %", "Tech Score",
         "Stop Loss", "Target 1", "Target 2", "Risk Reward"]
watch = ["Ticker", "Sector", "Current price", "Drawdown %", "Tech Score", "Notes"]
why = ["Ticker", "Sector", "Current price", "Notes"]

col1, col2 = st.columns(2)

with col1:
    st.subheader("🔥 Execution Ready (Pass)")
    st.caption("Strong fundamentals, fair valuation, 50-70% off the high, bottom formed, daily trend turning up.")
    section(df, "🔥 PASS", trade)

    st.subheader("⏳ Waiting for Price")
    section(df, "WAIT (Price)", basic + ["Drawdown %"])

    st.subheader("💰 Waiting for Valuation")
    section(df, "WAIT (Valuation)", basic + ["PE"])

with col2:
    st.subheader("👀 Watchlist (Near Buy Zone)")
    st.caption("Good business and valuation, price has fallen, but chart confirmation is still missing.")
    section(df, "👀 WATCH", watch)

    st.subheader("⚠️ Fundamental Wait (Fails Criteria)")
    section(df, "WAIT (Fund)", why)

st.divider()
st.info("💡 All columns are in your Google Sheet for cross-verification. This is a screening tool, not investment advice.")
