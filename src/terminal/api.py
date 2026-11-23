"""Local web API: search a company in the browser, get live SEC data back.

Run:  itx serve            then open http://127.0.0.1:8000  (API docs: /docs)

Nothing needs to be preloaded. The first search for a company fetches it from SEC EDGAR
(profile, filings list, 5-year XBRL financials) in a second or two and saves it to SQLite.
Later searches come from the disk cache in edgar.py (filings list refreshed daily, XBRL weekly).
Reading the annual report with the local LLM takes minutes, so it runs as a background job
(jobs.py) that the Explorer polls for progress.

Endpoints
  GET  /api/health              is the server up, is SEC_USER_AGENT set, is Ollama reachable
  GET  /api/search?q=teradyne   up to 8 matching SEC filers (for the search box)
  GET  /api/company/{query}     ticker or name -> profile, financials, history, filings, graph, extraction
  POST /api/extract/{query}     start reading its latest annual report in the background (?refresh=true to redo)
  GET  /api/jobs/{query}        progress of that job
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
from .jobs import JobManager
from .store import Store

app = FastAPI(title="Investment Terminal", version="0.4.0")


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


def _make_llm():
    from .extract import OllamaExtractor
    return OllamaExtractor()


@lru_cache
def _resolver():
    from .resolve import Resolver
    return Resolver(_edgar().tickers())


_jobs = JobManager(make_edgar=_edgar, make_store=Store, make_llm=_make_llm, make_resolver=_resolver)


def get_jobs() -> JobManager:
    return _jobs


def llm_problem() -> str | None:
    """None if Ollama is running with the model pulled, else a message for the user."""
    try:
        _make_llm().check()
        return None
    except SystemExit as e:          # check() exits with a readable message for the CLI
        return str(e)


# ---------- helpers ----------

def _find(ed: Edgar, query: str) -> dict:
    try:
        return ed.find(query)
    except EdgarError as e:
        raise HTTPException(404, str(e))
    except requests.RequestException as e:
        raise HTTPException(502, f"SEC unreachable: {e}")


def _extraction(st: Store, jobs: JobManager, entity_id: str) -> dict:
    """The company's extraction state: a live job's progress, or done / not_started from the database."""
    job = jobs.status(entity_id)
    if job and (job["status"] in ("queued", "running", "failed") or not st.is_expanded(entity_id)):
        return job
    return {"status": "done" if st.is_expanded(entity_id) else "not_started"}


# ---------- routes ----------

@app.get("/api/health")
def health(problem: str | None = Depends(llm_problem)) -> dict:
    ok = bool(settings.sec_user_agent and "@" in settings.sec_user_agent)
    return {"ok": True, "sec_user_agent_set": ok, "llm_ready": problem is None, "llm_problem": problem}


@app.get("/api/search")
def search(q: str, limit: int = 8, ed: Edgar = Depends(get_edgar)) -> list[dict]:
    try:
        return ed.search(q, limit=min(limit, 25))
    except requests.RequestException as e:
        raise HTTPException(502, f"SEC unreachable: {e}")


@app.get("/api/company/{query}")
def company(query: str, ed: Edgar = Depends(get_edgar), st: Store = Depends(get_store),
            jobs: JobManager = Depends(get_jobs)) -> dict:
    from .cli import add_company   # the same function `itx seed` / `itx company` use
    info = _find(ed, query)
    try:
        add_company(ed, st, info, years=1)   # live on first search, disk cache after
    except EdgarError as e:
        raise HTTPException(404, str(e))
    except requests.RequestException as e:
        raise HTTPException(502, f"SEC unreachable: {e}")
    st.db.commit()

    center = f"CIK:{info['cik']}"
    # Whatever the database already knows around this company (grows while extraction runs).
    nodes, rels = st.ego_graph(center, depth=1)
    data = graph_payload(build_graph(nodes, rels),
                         {"name": f"{info['name']} ({info['ticker']})", "segments": []},
                         {"mode": "company", "center": center})
    data["company"] = next(n for n in data["nodes"] if n["id"] == center)
    data["filings"] = [dict(r) for r in st.db.execute(
        "SELECT accession, form, filed, period, url, status FROM filings "
        "WHERE entity_id=? ORDER BY filed DESC", (center,))]
    data["extraction"] = _extraction(st, jobs, center)
    return data


@app.post("/api/extract/{query}")
def extract(query: str, refresh: bool = False, ed: Edgar = Depends(get_edgar),
            st: Store = Depends(get_store), jobs: JobManager = Depends(get_jobs),
            problem: str | None = Depends(llm_problem)) -> dict:
    info = _find(ed, query)
    center = f"CIK:{info['cik']}"
    if st.is_expanded(center) and not refresh:
        return {"status": "done"}
    if problem:
        raise HTTPException(503, problem)
    if not st.db.execute("SELECT 1 FROM filings WHERE entity_id=?", (center,)).fetchone():
        raise HTTPException(409, "Search the company first (GET /api/company/...), then extract.")
    return jobs.submit(center, info["ticker"], refresh=refresh)


@app.get("/api/jobs/{query}")
def job_status(query: str, ed: Edgar = Depends(get_edgar), st: Store = Depends(get_store),
               jobs: JobManager = Depends(get_jobs)) -> dict:
    info = _find(ed, query)
    return _extraction(st, jobs, f"CIK:{info['cik']}")


@app.get("/", include_in_schema=False)
def home() -> RedirectResponse:
    """The terminal UI is the Explorer."""
    return RedirectResponse("/prototype/explorer.html")


# Everything in web/ (the Explorer, data files) is served as static files, so one server
# does everything. Mounted last, so the routes above take priority.
app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
