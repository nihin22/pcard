"""P-Card Audit Workbench – Streamlit app with two tabs.

1. Ask the database: natural-language questions -> SQL (Google Gemini) -> results.
2. Prohibited-purchase dashboard: Description search and Vendor search by year.

The Gemini API key is read from Streamlit secrets (GEMINI_API_KEY). It is never in this code or on GitHub.
"""
import time

import pandas as pd
import streamlit as st

import pcard_core as core

st.set_page_config(page_title="P-Card Audit Workbench", page_icon="💳", layout="wide")


# ------------------------------------------------------------------ setup
@st.cache_resource(show_spinner="Preparing the transaction database (first start only)…")
def init_db():
    core.ensure_db()
    return core.years()


YEARS = init_db()


def secret(name, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:          # no secrets file at all (e.g. local run without .streamlit/secrets.toml)
        return default


API_KEY = secret("GEMINI_API_KEY")
_fallbacks = [m.strip() for m in str(secret("GEMINI_FALLBACK_MODELS", "")).split(",") if m.strip()]
MODELS = list(dict.fromkeys([secret("GEMINI_MODEL", "")] + (_fallbacks or core.DEFAULT_MODELS)))


def llm(system, user, max_tokens=2048):
    return core.call_gemini(system, user, API_KEY, MODELS, max_tokens)


def to_df(columns, rows):
    df = pd.DataFrame(rows, columns=columns)
    for c in ("TransactionDate", "PostedDate"):
        if c in df.columns:
            df[c] = df[c].astype(str).str.replace(" 0:00:00", "", regex=False)
    return df


def money_config(df):
    return {c: st.column_config.NumberColumn(c, format="$%.2f") for c in df.columns
            if any(k in c.lower() for k in ("amount", "total", "spent", "avg", "sum"))
            and pd.api.types.is_numeric_dtype(df[c])}


# ------------------------------------------------------------------ header
st.title("💳 P-Card Audit Workbench")
st.caption("Internal audit of purchasing-card transactions, Oklahoma State University 2010–2014. "
           "A flagged transaction is only a potential deviation – not evidence of wrongdoing.")

tab_ask, tab_dash = st.tabs(["🔎 Ask the database", "🚩 Prohibited-purchase dashboard"])

# ------------------------------------------------------------------ TAB 1
with tab_ask:
    st.subheader("Ask a question in plain language")
    st.write("Type a question about the P-card transactions. Gemini writes a **read-only SQL query**, the app runs it "
             "on the database, and you see the answer, the SQL and the result table so you can verify it.")
    if not API_KEY:
        st.warning("The AI feature is not configured yet: add `GEMINI_API_KEY` under the app's **Settings → Secrets**.")

    examples = [
        "Which 10 employees spent the most in 2014?",
        "How many transactions over $5,000 were there each year?",
        "Show all purchases from liquor stores in 2014",
        "Total spending by month in 2013",
        "Which vendors received the most money from Employee B1FA2F0D?",
    ]
    if "question" not in st.session_state:
        st.session_state.question = ""
    st.write("Examples:")
    cols = st.columns(len(examples))
    for col, ex in zip(cols, examples):
        if col.button(ex, use_container_width=True):
            st.session_state.question = ex

    with st.form("ask"):
        question = st.text_area("Your question", key="question", height=90,
                                placeholder="e.g. Which 10 employees spent the most in 2014?")
        asked = st.form_submit_button("Ask", type="primary")

    if asked:
        # simple per-session rate limit to protect the key
        log = [t for t in st.session_state.get("ask_log", []) if time.time() - t < 60]
        if len(log) >= 10:
            st.error("Too many questions in the last minute – please wait a moment.")
        else:
            st.session_state.ask_log = log + [time.time()]
            with st.spinner("Writing and running a query…"):
                try:
                    st.session_state.last = {"q": question, **core.ask(question, llm)}
                except Exception as e:
                    st.session_state.last = None
                    st.error(str(e))

    res = st.session_state.get("last")
    if res:
        st.markdown(f"**{res['q']}**")
        if res["answer"]:
            st.info(res["answer"])
        with st.expander(f"SQL used ({len(res['rows'])}{'+' if res['truncated'] else ''} rows)"):
            st.code(res["sql"], language="sql")
            st.caption(res["explanation"])
        df = to_df(res["columns"], res["rows"])
        if df.empty:
            st.write("The query returned no rows.")
        else:
            if res["truncated"]:
                st.caption(f"Showing the first {len(df):,} rows.")
            st.dataframe(df, use_container_width=True, hide_index=True, column_config=money_config(df))
            st.download_button("Download CSV", df.to_csv(index=False), "pcard_question_result.csv", "text/csv")

# ------------------------------------------------------------------ TAB 2
with tab_dash:
    st.subheader("How to use this dashboard")
    st.markdown(
        "1. **Choose the year** to search (or *All years*).\n"
        "2. Use **Description search** to look for words in the transaction description (e.g. *wine*, *gift card*), "
        "or **Vendor search** to look for words in the merchant name (e.g. *usps*, *liquor*).\n"
        "3. Pick a **prohibited category** to fill in suggested keywords, or type your own. Separate several keywords "
        "with commas – a transaction matches if it contains *any* of them. Matching is not case-sensitive.\n"
        "4. Press **Search**. Review the totals, the top cardholders and vendors and the transaction list. Click a column "
        "heading to sort; use **Download CSV** to keep the evidence for follow-up.\n"
        "5. A match is only a **potential** deviation: check receipts and business purpose before concluding. Many "
        "descriptions just say *GENERAL PURCHASE*, so the vendor search usually finds more."
    )

    f1, f2 = st.columns([1, 3])
    year = f1.selectbox("Year", ["All years"] + [str(y) for y in reversed(YEARS)], index=1)
    positive = f2.checkbox("Charges only (exclude credits/returns)", value=True)
    year_arg = None if year == "All years" else int(year)

    cat_names = ["— choose to fill keywords —"] + [c["name"] for c in core.CATEGORIES]
    by_name = {c["name"]: c for c in core.CATEGORIES}

    def fill(mode):
        name = st.session_state[f"cat_{mode}"]
        if name in by_name:
            st.session_state[f"kw_{mode}"] = by_name[name][mode]

    left, right = st.columns(2)
    for col, mode, title, field, ph in [
        (left, "description", "Description search", "transaction description", "e.g. wine, beer, liquor"),
        (right, "vendor", "Vendor search", "vendor (merchant) name", "e.g. usps, post office"),
    ]:
        with col.container(border=True):
            st.markdown(f"#### {title}")
            st.caption(f"Searches the **{field}** field.")
            st.selectbox("Prohibited category (optional)", cat_names, key=f"cat_{mode}", on_change=fill, args=(mode,))
            st.text_input("Keyword(s)", key=f"kw_{mode}", placeholder=ph)
            if st.button(f"Search {mode}", key=f"btn_{mode}", type="primary"):
                st.session_state.search = (mode, st.session_state[f"kw_{mode}"])

    if st.session_state.get("search"):
        mode, kw = st.session_state.search
        try:
            r = core.search(mode, kw, year_arg, positive, max_rows=100000)
        except ValueError as e:
            st.error(str(e))
        else:
            s = r["summary"]
            label = "Vendor search" if mode == "vendor" else "Description search"
            st.markdown(f"### {label}: “{kw}” · {year.lower() if year_arg is None else year}")
            m = st.columns(5)
            m[0].metric("Transactions", f"{s['transactions']:,}")
            m[1].metric("Total amount", f"${s['total']:,.2f}")
            m[2].metric("Cardholders", f"{s['cardholders']:,}")
            m[3].metric("Vendors", f"{s['vendors']:,}")
            m[4].metric("Largest charge", f"${s['largest']:,.2f}")

            t1, t2 = st.columns(2)
            for col, title, rows, who in [(t1, "Top cardholders (by amount)", r["top_cardholders"], "Cardholder"),
                                          (t2, "Top vendors (by amount)", r["top_vendors"], "Vendor")]:
                col.markdown(f"**{title}**")
                tdf = pd.DataFrame(rows, columns=[who, "Transactions", "Total"])
                col.dataframe(tdf, hide_index=True, use_container_width=True, column_config=money_config(tdf))

            st.markdown("**Transactions for follow-up**")
            df = to_df(r["columns"], r["rows"])
            if df.empty:
                st.write("No matching transactions. Try another keyword or year.")
            else:
                if r["truncated"]:
                    st.caption(f"Showing the first {len(df):,} transactions.")
                st.dataframe(df, use_container_width=True, hide_index=True, height=520, column_config=money_config(df))
                st.download_button("Download CSV", df.to_csv(index=False),
                                   f"pcard_{mode}_{year_arg or 'all'}.csv", "text/csv")
