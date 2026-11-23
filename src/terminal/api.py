"""Local web API: search a company in the browser, get live SEC data back.

Run:  itx serve            then open http://127.0.0.1:8000  (API docs: /docs)

Nothing needs to be preloaded. The first search for a company fetches it from SEC EDGAR
(profile, filings list, 5-year XBRL financials) in a second or two and saves it to SQLite.
Later searches come from the disk cache in edgar.py (filings list refreshed daily, XBRL weekly).

Endpoints
  GET /api/health              is the server up, is SEC_USER_AGENT set
  GET /api/search?q=teradyne   up to 8 matching SEC filers (for the search box)
  GET /api/company/{query}     ticker or name -> profile, financials, history, filings, graph
"""
from __future__ import annotations

from functools import lru_cache

import requests
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import ROOT, settings
from .edgar import Edgar, EdgarError
from .graph import build_graph, graph_payload
from .store import Store

app = FastAPI(title="Investment Terminal", version="0.3.0")


# ---------- dependencies (tests swap these for fakes) ----------

@lru_cache
def _edgar() -> Edgar:
    return Edgar()


def get_edgar() -> Edgar:
    try:
        return _edgar()
    except EdgarError as e:          # SEC_USER_AGENT missing
        raise HTTPException(503, str(e))


def get_store():
    st = Store()
    try:
        yield st
    finally:
        st.close()


# ---------- routes ----------

@app.get("/api/health")
def health() -> dict:
    ok = bool(settings.sec_user_agent and "@" in settings.sec_user_agent)
    return {"ok": True, "sec_user_agent_set": ok}


@app.get("/api/search")
def search(q: str, limit: int = 8, ed: Edgar = Depends(get_edgar)) -> list[dict]:
    try:
        return ed.search(q, limit=min(limit, 25))
    except requests.RequestException as e:
        raise HTTPException(502, f"SEC unreachable: {e}")


@app.get("/api/company/{query}")
def company(query: str, ed: Edgar = Depends(get_edgar), st: Store = Depends(get_store)) -> dict:
    from .cli import add_company   # the same function `itx seed` / `itx company` use
    try:
        info = ed.find(query)
        add_company(ed, st, info, years=1)   # live on first search, disk cache after
    except EdgarError as e:
        raise HTTPException(404, str(e))
    except requests.RequestException as e:
        raise HTTPException(502, f"SEC unreachable: {e}")
    st.db.commit()

    center = f"CIK:{info['cik']}"
    # Whatever the database already knows around this company (empty until extraction runs).
    nodes, rels = st.ego_graph(center, depth=1)
    data = graph_payload(build_graph(nodes, rels),
                         {"name": f"{info['name']} ({info['ticker']})", "segments": []},
                         {"mode": "company", "center": center})
    data["company"] = next(n for n in data["nodes"] if n["id"] == center)
    data["filings"] = [dict(r) for r in st.db.execute(
        "SELECT accession, form, filed, period, url, status FROM filings "
        "WHERE entity_id=? ORDER BY filed DESC", (center,))]
    return data


@app.get("/", include_in_schema=False)
def home() -> RedirectResponse:
    """The terminal UI is the Explorer."""
    return RedirectResponse("/prototype/explorer.html")


# Everything in web/ (the Explorer, data files) is served as static files, so one server
# does everything. Mounted last, so the routes above take priority.
app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
