"""Core logic for the P-card auditor Streamlit app: database access, keyword searches,
SQL safety checks and natural-language-to-SQL via Google Gemini.

The Gemini API key is NEVER stored in this file. The app reads it from Streamlit
secrets (st.secrets["GEMINI_API_KEY"]) at run time.
"""
import gzip
import json
import os
import re
import shutil
import sqlite3
import threading
import time

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_GZ = os.path.join(BASE_DIR, "data", "pcards.db.gz")
DB_PATH = os.environ.get("PCARD_DB_PATH", os.path.join(BASE_DIR, "data", "pcards.db"))

MAX_ROWS = 1000          # cap on rows returned to the browser
QUERY_TIME_LIMIT = 8.0   # seconds before a query is aborted

_lock = threading.Lock()


# --------------------------------------------------------------------------- database
def ensure_db():
    """Decompress the bundled database on first start (the raw .db is >100 MB, GitHub's file limit)."""
    with _lock:
        if os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 0:
            return
        tmp = DB_PATH + ".tmp"
        with gzip.open(DB_GZ, "rb") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        con = sqlite3.connect(tmp)
        con.execute("CREATE INDEX IF NOT EXISTS ix_year ON pcards(Year)")
        con.commit()
        con.close()
        os.replace(tmp, DB_PATH)


_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
if hasattr(sqlite3, "SQLITE_RECURSIVE"):
    _ALLOWED_ACTIONS.add(sqlite3.SQLITE_RECURSIVE)


def _authorizer(action, arg1, arg2, dbname, source):
    # Only reading is allowed: no INSERT/UPDATE/DELETE/DDL/PRAGMA/ATTACH, whatever the SQL says.
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def connect():
    ensure_db()
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    con.set_authorizer(_authorizer)
    return con


def run_query(sql, params=(), max_rows=MAX_ROWS):
    """Run a read-only query with a time limit. Returns (columns, rows, truncated)."""
    con = connect()
    deadline = time.time() + QUERY_TIME_LIMIT
    con.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 10000)
    try:
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(max_rows + 1)
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise ValueError("The query took too long and was stopped. Try a narrower question.")
        raise
    finally:
        con.close()
    truncated = len(rows) > max_rows
    return cols, [list(r) for r in rows[:max_rows]], truncated


def years():
    _, rows, _ = run_query("SELECT DISTINCT Year FROM pcards ORDER BY Year")
    return [r[0] for r in rows]


# --------------------------------------------------------------------------- dashboard search
SEARCH_FIELDS = {"description": "Description", "vendor": "Vendor"}
RESULT_COLUMNS = "ID, Year, Month, TransactionDate, PostedDate, FullName, Amount, Vendor, Description, MCC"


def _keyword_clause(field, keyword):
    """Comma-separated keywords are OR-ed together. LIKE is case-insensitive for ASCII in SQLite."""
    terms = [t.strip() for t in keyword.split(",") if t.strip()]
    if not terms:
        raise ValueError("Enter at least one keyword.")
    if len(terms) > 10:
        raise ValueError("Use at most 10 keywords at a time.")
    clause = "(" + " OR ".join(f"{field} LIKE ? ESCAPE '\\'" for _ in terms) + ")"
    params = ["%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for t in terms]
    return clause, params


def search(mode, keyword, year=None, positive_only=True, max_rows=MAX_ROWS):
    field = SEARCH_FIELDS.get(mode)
    if not field:
        raise ValueError("Unknown search type.")
    clause, params = _keyword_clause(field, keyword)
    where = [clause]
    if year not in (None, "", "all"):
        where.append("Year = ?")
        params.append(int(year))
    if positive_only:
        where.append("Amount > 0")
    w = " AND ".join(where)
    order = ("ORDER BY Year, Month, CAST(substr(TransactionDate, instr(TransactionDate,'/')+1, 2) AS INTEGER), "
             "FullName, Amount DESC")
    cols, rows, truncated = run_query(f"SELECT {RESULT_COLUMNS} FROM pcards WHERE {w} {order}", params, max_rows)
    _, summ, _ = run_query(
        f"SELECT COUNT(*), ROUND(COALESCE(SUM(Amount),0),2), COUNT(DISTINCT FullName), COUNT(DISTINCT Vendor), "
        f"ROUND(COALESCE(MAX(Amount),0),2) FROM pcards WHERE {w}", params)
    _, top_people, _ = run_query(
        f"SELECT FullName, COUNT(*), ROUND(SUM(Amount),2) FROM pcards WHERE {w} "
        f"GROUP BY FullName ORDER BY SUM(Amount) DESC LIMIT 10", params)
    _, top_vendors, _ = run_query(
        f"SELECT Vendor, COUNT(*), ROUND(SUM(Amount),2) FROM pcards WHERE {w} "
        f"GROUP BY Vendor ORDER BY SUM(Amount) DESC LIMIT 10", params)
    n, total, people, vendors, largest = summ[0]
    return {
        "columns": cols, "rows": rows, "truncated": truncated,
        "summary": {"transactions": n, "total": total, "cardholders": people, "vendors": vendors, "largest": largest},
        "top_cardholders": top_people, "top_vendors": top_vendors,
    }


# Suggested keywords for each prohibited category (Part I of the case).
CATEGORIES = [
    {"name": "Alcohol", "description": "beer, wine, liquor, vodka, whiskey, champagne",
     "vendor": "liquor, wine, brewing, brewery, tavern, bottle shop, spirits"},
    {"name": "Cash / cash advances / ATM", "description": "cash, atm, withdrawal, advance",
     "vendor": "atm, cash, western union, moneygram"},
    {"name": "Decorations", "description": "decoration, decor, balloon, ornament, banner",
     "vendor": "party city, hobby lobby, michaels, balloon, decor"},
    {"name": "Donations & sponsorships", "description": "donation, sponsor, contribution",
     "vendor": "foundation, church, united way, charity, ministries"},
    {"name": "Gasoline", "description": "fuel, gas, unleaded, diesel",
     "vendor": "oil, fuel, shell, conoco, phillips 66, qt, quiktrip, valero, exxon"},
    {"name": "Gifts & gift cards", "description": "gift card, gift cert, gift",
     "vendor": "gift, card shop, hallmark"},
    {"name": "Insurance", "description": "insurance, premium, policy",
     "vendor": "insurance, state farm, geico, allstate"},
    {"name": "Late fees", "description": "late fee, late charge, finance charge, penalty",
     "vendor": "late fee, penalty"},
    {"name": "Mail & postage", "description": "postage, stamp, mailing",
     "vendor": "usps, post office, postal, pitney, stamps.com"},
    {"name": "Moving expenses", "description": "moving, relocation, mover",
     "vendor": "moving, u-haul, uhaul, mayflower, two men, movers"},
    {"name": "Personal purchases", "description": "personal, cosmetic, jewelry, salon",
     "vendor": "salon, spa, jewel, cosmetic, netflix, itunes, cinema"},
    {"name": "Personal memberships & dues", "description": "membership, dues, renewal",
     "vendor": "club, membership, fitness, gym, costco, sams"},
    {"name": "Salaries, wages & benefits", "description": "salary, wage, payroll, benefit",
     "vendor": "payroll, adp, paychex, staffing"},
    {"name": "Service & incentive awards", "description": "award, plaque, trophy, recognition",
     "vendor": "trophy, awards, plaque, engraving"},
]


# --------------------------------------------------------------------------- natural language
SCHEMA_PROMPT = """You translate auditors' questions into ONE SQLite query against this database.

Table pcards (489,178 rows, Oklahoma State University P-card transactions, Sep 2010 - Dec 2014):
- Year INTEGER            -- calendar year of the transaction date (2010-2014)
- Month INTEGER           -- month of the transaction date (1-12)
- FullName TEXT           -- cardholder, e.g. 'Employee 51914657'
- ID INTEGER              -- unique transaction id
- AgencyNumber INTEGER    -- always 1000
- AgencyName TEXT         -- always 'OKLAHOMA STATE UNIVERSITY'
- CardholderLastName TEXT -- e.g. 'Employee51914657'
- CardholderFirstInitial TEXT
- Description TEXT        -- free text, often 'GENERAL PURCHASE', 'ROOM CHARGES', 'AIR TRAVEL'
- Amount REAL             -- USD; negative values are credits/returns
- Vendor TEXT             -- merchant name in UPPER CASE, e.g. 'USPS 39781002033603457', 'LOWES #00241'
- TransactionDate TEXT    -- text 'M/D/YYYY 0:00:00', e.g. '7/26/2014 0:00:00' (NOT sortable as text)
- PostedDate TEXT         -- same text format
- MCC TEXT                -- merchant category in UPPER CASE, e.g. 'EATING PLACES AND RESTAURANTS', 'POSTAGE STAMPS',
                             'PACKAGE STORES--BEER,WINE,AND LIQUOR', 'SERVICE STATIONS (WITH OR WITHOUT ANCILLARY SERVICES'

Rules:
- Use only SELECT (WITH ... SELECT is fine). Never modify data. One statement, no semicolons inside.
- Use Year/Month columns for date filters. To get an ISO date use
  printf('%04d-%02d-%02d', Year, Month, CAST(substr(TransactionDate, instr(TransactionDate,'/')+1, 2) AS INTEGER)).
- For text matching use LIKE '%word%' (case-insensitive) on Vendor, Description or MCC.
- Round money to 2 decimals. Add a sensible ORDER BY. Add LIMIT 200 unless the user asks for a full list or an aggregate.
- Policy context: single-transaction limit $5,000; monthly $10,000; annual $50,000. Prohibited: alcohol, cash/ATM,
  decorations, donations/sponsorships, gasoline, gifts/gift cards, insurance, late fees, postage, moving expenses,
  personal purchases, personal memberships, salaries/wages, service/incentive awards, split purchases.

Reply with JSON only, no prose and no code fences:
{"sql": "<the query>", "explanation": "<one or two sentences on how the query answers the question>"}"""

ANSWER_PROMPT = """You are helping an internal auditor. Given the question, the SQL that was run and the first rows of the
result, write a short, factual answer (2-4 sentences) in the same language as the question. Mention key numbers.
Do not invent data that is not in the result. If the result is empty, say so and suggest how to rephrase."""

_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex|"
                        r"analyze|begin|commit|rollback|savepoint|release|load_extension)\b", re.I)


def validate_sql(sql):
    s = sql.strip().strip("`").strip()
    s = re.sub(r"^sql\s*", "", s, flags=re.I)
    s = s.rstrip().rstrip(";").strip()
    if ";" in s:
        raise ValueError("Only a single SQL statement is allowed.")
    if not re.match(r"^(select|with)\b", s, re.I):
        raise ValueError("Only SELECT queries are allowed.")
    no_strings = re.sub(r"'(?:[^']|'')*'", "''", s)
    if _FORBIDDEN.search(no_strings):
        raise ValueError("The query contains a statement that is not allowed.")
    return s


# Models tried in order. If one is overloaded/unavailable, the next one is used.
# Can be overridden with GEMINI_MODEL / GEMINI_FALLBACK_MODELS in Streamlit secrets.
DEFAULT_MODELS = ["gemini-3.8-flash", "gemini-flash-latest", "gemini-flash-lite-latest"]
RETRY_STATUS = {404, 429, 500, 503, 504}   # not found / quota / overloaded / server errors


def _gemini_once(system, user, api_key, model, max_tokens):
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json={"systemInstruction": {"parts": [{"text": system}]},
              "contents": [{"role": "user", "parts": [{"text": user}]}],
              "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens}},
        timeout=60,
    )
    if resp.status_code != 200:
        try:
            msg = resp.json().get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        return None, resp.status_code, msg
    parts = (resp.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts), 200, ""


def call_gemini(system, user, api_key, models=None, max_tokens=2048):
    """Call Google Gemini, falling back to the next model if one is busy or unavailable.
    The key is passed in at run time (from Streamlit secrets) and never stored in code."""
    if not api_key:
        raise RuntimeError("The AI feature is not configured: add GEMINI_API_KEY to the app's Secrets.")
    if isinstance(models, str):
        models = [models]
    models = [m for m in (models or DEFAULT_MODELS) if m]
    errors = []
    for model in models:
        for attempt in range(2):                     # one quick retry on the same model
            try:
                text, status, msg = _gemini_once(system, user, api_key, model, max_tokens)
            except requests.RequestException as e:
                text, status, msg = None, 503, str(e)
            if text is not None:
                return text
            if status not in RETRY_STATUS:           # e.g. 400 bad key/request: no point trying others
                raise RuntimeError(f"Gemini returned an error ({status}). {msg}")
            if status == 404:                         # model does not exist: go straight to the next
                break
            time.sleep(1.5 * (attempt + 1))
        errors.append(f"{model}: {status}")
    raise RuntimeError("All Gemini models are busy or unavailable right now (" + ", ".join(errors) +
                       "). Please try again in a minute.")


def _parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("Could not understand the AI response. Please rephrase the question.")
    return json.loads(m.group(0))


def ask(question, llm):
    """llm(system, user, max_tokens=...) -> text"""
    question = (question or "").strip()
    if not question:
        raise ValueError("Please type a question.")
    if len(question) > 1000:
        raise ValueError("Please keep the question under 1,000 characters.")
    plan = _parse_json(llm(SCHEMA_PROMPT, question))
    sql = validate_sql(plan.get("sql", ""))
    try:
        cols, rows, truncated = run_query(sql)
    except sqlite3.Error as e:
        # one repair attempt: send the error back to the model
        plan = _parse_json(llm(SCHEMA_PROMPT, f"{question}\n\nYour previous SQL failed with: {e}\nSQL was:\n{sql}\n"
                                              "Return corrected JSON."))
        sql = validate_sql(plan.get("sql", ""))
        cols, rows, truncated = run_query(sql)
    preview = json.dumps({"columns": cols, "rows": rows[:40], "row_count": len(rows),
                          "truncated": truncated}, default=str)
    try:
        answer = llm(ANSWER_PROMPT, f"Question: {question}\nSQL: {sql}\nResult: {preview}", max_tokens=500).strip()
    except Exception:
        answer = ""
    return {"sql": sql, "explanation": plan.get("explanation", ""), "columns": cols, "rows": rows,
            "truncated": truncated, "answer": answer}
