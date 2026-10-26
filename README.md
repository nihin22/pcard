# P-Card Audit Workbench (Streamlit)

Two-tab Streamlit app for the *Analytics Mindset – P-card* case.

* **Ask the database** – ask in plain language; Google Gemini writes one read-only SQL `SELECT`, the app runs it and
  shows the answer, the SQL and the result table (CSV download).
* **Prohibited-purchase dashboard** – instructions, year selector, separate **Description search** and **Vendor search**
  with quick-pick keywords for each prohibited category, totals, top cardholders/vendors, sortable table, CSV export.

## API key protection
The Gemini key is **not** in the code or on GitHub. On Streamlit Community Cloud it is stored under the app's
**Settings → Secrets** and read at run time with `st.secrets["GEMINI_API_KEY"]`. Locally it goes in
`.streamlit/secrets.toml`, which is listed in `.gitignore`. All AI calls are made by the server, so visitors never see
the key.

Safety of the generated SQL: only a single `SELECT`/`WITH` is accepted, the database is opened read-only with an SQLite
authorizer that denies every write, queries stop after 8 seconds and results are capped.

## Files
```
streamlit_app.py                 the app (two tabs)
pcard_core.py                    database, searches, SQL checks, Gemini call
data/pcards.db.gz                course database, gzip-compressed (unpacked automatically on first start)
requirements.txt                 streamlit, pandas, requests
.streamlit/secrets.toml.example  template for the secret (the real secrets.toml is never committed)
.gitignore
```

## Deploy on Streamlit Community Cloud (free)
1. Get a free key at <https://aistudio.google.com/apikey>.
2. Upload these files to a **public** GitHub repository.
3. Go to <https://share.streamlit.io>, sign in with GitHub → **Create app** → choose the repository, branch `main`,
   main file `streamlit_app.py`.
4. Open **Advanced settings → Secrets** and paste:
   ```
   GEMINI_API_KEY = "your-key"
   ```
5. **Deploy**. The live URL looks like `https://<name>.streamlit.app`.

## Run locally (optional)
```
pip install -r requirements.txt
copy .streamlit\secrets.toml.example .streamlit\secrets.toml   (then put your key in it)
streamlit run streamlit_app.py
```
