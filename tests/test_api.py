"""The local web API, with a fake EDGAR (no network) and a temporary database."""
import pytest

pytest.importorskip("fastapi")   # skip if the [api] extra isn't installed
from fastapi.testclient import TestClient

import terminal.extract as extract_mod
from terminal import api
from terminal.edgar import EdgarError, Filing
from terminal.extract import Relationship, Tag
from terminal.jobs import JobManager
from terminal.resolve import Resolver
from terminal.store import Store

TICKERS = {"TER": {"cik": 97210, "ticker": "TER", "name": "TERADYNE, INC"},
           "TXN": {"cik": 97476, "ticker": "TXN", "name": "TEXAS INSTRUMENTS INC"},
           "NVDA": {"cik": 1045810, "ticker": "NVDA", "name": "NVIDIA CORP"}}

DOC = """<html><body><p>Item 1. Business</p>
<p>We build collaborative robots. Our robot controllers use GPUs supplied by NVIDIA Corporation under a multi-year supply agreement.</p>
<p>Item 1A. Risk Factors</p><p>We compete with Advantest Corporation and Cohu, Inc. in semiconductor test markets worldwide.</p>
<p>Item 1B.</p></body></html>"""

FACTS = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
    {"start": "2024-01-01", "end": "2024-12-31", "val": 2_820_000_000, "form": "10-K",
     "filed": "2025-02-01", "accn": "a1"},
    {"start": "2023-01-01", "end": "2023-12-31", "val": 2_676_000_000, "form": "10-K",
     "filed": "2024-02-01", "accn": "a0"},
]}}}}}


class FakeEdgar:
    def __init__(self):
        self.facts_calls = 0

    def tickers(self): return TICKERS
    def lookup(self, t): return TICKERS[t.upper()]
    def search(self, q, limit=8):
        return [c for t, c in TICKERS.items() if t.startswith(q.upper())][:limit]
    def find(self, q):
        if q.upper() not in TICKERS:
            raise EdgarError(f"No SEC filer matches '{q}'")
        return TICKERS[q.upper()]
    def profile(self, cik): return {"sic": "3825", "sic_desc": "Instruments for measuring"}
    def company_facts(self, cik):
        self.facts_calls += 1
        return FACTS
    def filings(self, ticker, limit=1):
        c = TICKERS[ticker]
        return [Filing(c["cik"], ticker, c["name"], "10-K", "0000097210-25-000010",
                       "2025-02-20", "2024-12-31", "ter-20241231.htm")]
    def get(self, url): return DOC


class FakeLLM:
    model = "fake"
    def extract(self, company, passage):
        if "NVIDIA" in passage.text:
            return [Relationship("NVIDIA Corporation", "supplies", company, "GPUs supplied by NVIDIA",
                                 0.9, "GPUs", passage.section, passage.index)]
        if "Advantest" in passage.text:
            return [Relationship(company, "competes_with", "Advantest Corporation", "We compete with Advantest",
                                 0.9, "", passage.section, passage.index),
                    Relationship(company, "competes_with", "five largest customers", "junk", 0.9,  # filtered
                                 "", passage.section, passage.index)]
        return []


@pytest.fixture
def client(tmp_path, monkeypatch):
    fake = FakeEdgar()
    api.app.dependency_overrides[api.get_edgar] = lambda: fake

    def tmp_store():
        st = Store(tmp_path / "t.db")
        try:
            yield st
        finally:
            st.close()
    api.app.dependency_overrides[api.get_store] = tmp_store
    # Background jobs with the fakes; the LLM is "ready" unless a test says otherwise.
    jobs = JobManager(make_edgar=lambda: fake, make_store=lambda: Store(tmp_path / "t.db"),
                      make_llm=FakeLLM, make_resolver=lambda: Resolver(TICKERS, aliases_path=tmp_path / "none.yaml"))
    api.app.dependency_overrides[api.get_jobs] = lambda: jobs
    api.app.dependency_overrides[api.llm_problem] = lambda: None
    monkeypatch.setattr(extract_mod, "tag_company", lambda llm, company, text: [Tag("robotics", "maker", "e", 0.9)])
    c = TestClient(api.app)
    c.jobs = jobs
    yield c, fake
    api.app.dependency_overrides.clear()


def test_health(client):
    c, _ = client
    assert c.get("/api/health").json()["ok"] is True


def test_search(client):
    c, _ = client
    assert [r["ticker"] for r in c.get("/api/search", params={"q": "te"}).json()] == ["TER"]


def test_company_first_search_fetches_live(client):
    c, fake = client
    r = c.get("/api/company/ter")
    assert r.status_code == 200
    d = r.json()
    assert d["mode"] == "company" and d["center"] == "CIK:97210"
    co = d["company"]
    assert co["sic"] == "3825"
    assert co["financials"]["revenue"]["value"] == 2_820_000_000
    assert [h["value"] for h in co["history"]["revenue"]] == [2_820_000_000, 2_676_000_000]
    assert d["filings"][0]["form"] == "10-K" and d["filings"][0]["status"] == "fetched"
    assert fake.facts_calls == 1


def test_company_is_saved_for_later(client, tmp_path):
    c, _ = client
    c.get("/api/company/TER")
    st = Store(tmp_path / "t.db")
    assert st.financials_history(["CIK:97210"])["CIK:97210"]["revenue"][0]["value"] == 2_820_000_000
    st.close()


def test_unknown_company_is_404(client):
    c, _ = client
    r = c.get("/api/company/NOPE")
    assert r.status_code == 404 and "No SEC filer" in r.json()["detail"]


def test_home_opens_the_explorer(client):
    c, _ = client
    r = c.get("/")                      # follows the redirect
    assert r.status_code == 200
    assert r.url.path == "/prototype/explorer.html"
    assert "liveCompany" in r.text      # the live-wired Explorer, not the old viewer


# ---------- background extraction ----------

def test_extraction_flow(client):
    c, _ = client
    d = c.get("/api/company/TER").json()
    assert d["extraction"]["status"] == "not_started" and d["edges"] == []

    job = c.post("/api/extract/TER").json()
    assert job["status"] in ("queued", "running", "done") and job["ticker"] == "TER"
    done = c.jobs.wait("CIK:97210")
    assert done["status"] == "done", done
    assert done["found"] == 2                      # NVIDIA supplier + Advantest; the junk one is filtered

    d = c.get("/api/company/TER").json()
    assert d["extraction"]["status"] == "done"
    rels = {(e["source"], e["relation"], e["target"]) for e in d["edges"]}
    assert ("CIK:1045810", "supplies", "CIK:97210") in rels
    assert ("CIK:97210", "competes_with", "ORG:advantest") in rels
    assert d["company"]["tags"][0]["tag"] == "robotics"
    assert c.get("/api/jobs/TER").json()["status"] == "done"


def test_extract_again_is_a_no_op_unless_refresh(client):
    c, _ = client
    c.get("/api/company/TER")
    c.post("/api/extract/TER"); c.jobs.wait("CIK:97210")
    assert c.post("/api/extract/TER").json() == {"status": "done"}
    again = c.post("/api/extract/TER", params={"refresh": True}).json()
    assert again["status"] in ("queued", "running", "done") and again.get("refresh") is True
    assert c.jobs.wait("CIK:97210")["found"] == 2  # replaced, not duplicated


def test_extract_needs_ollama(client):
    c, _ = client
    c.get("/api/company/TER")
    api.app.dependency_overrides[api.llm_problem] = lambda: "Ollama not reachable at http://localhost:11434."
    r = c.post("/api/extract/TER")
    assert r.status_code == 503 and "Ollama" in r.json()["detail"]


def test_extract_needs_a_search_first(client):
    c, _ = client
    assert c.post("/api/extract/TER").status_code == 409


def test_job_failure_is_reported(client, monkeypatch):
    c, fake = client
    c.get("/api/company/TER")
    monkeypatch.setattr(fake, "get", lambda url: (_ for _ in ()).throw(RuntimeError("disk full")))
    c.post("/api/extract/TER")
    s = c.jobs.wait("CIK:97210")
    assert s["status"] == "failed" and "disk full" in s["error"]
    assert c.get("/api/jobs/TER").json()["status"] == "failed"


def _edges(c):
    return {(e["source"], e["relation"], e["target"]) for e in c.get("/api/company/TER").json()["edges"]}


def test_failed_reread_keeps_the_old_relationships(client, monkeypatch):
    c, fake = client
    c.get("/api/company/TER"); c.post("/api/extract/TER"); c.jobs.wait("CIK:97210")
    before = _edges(c)
    assert len(before) == 2
    monkeypatch.setattr(fake, "get", lambda url: (_ for _ in ()).throw(RuntimeError("network down")))
    c.post("/api/extract/TER", params={"refresh": True})
    assert c.jobs.wait("CIK:97210")["status"] == "failed"
    assert _edges(c) == before                     # nothing lost


def test_reread_swaps_in_the_new_results_at_the_end(client, monkeypatch):
    c, _ = client
    c.get("/api/company/TER"); c.post("/api/extract/TER"); c.jobs.wait("CIK:97210")
    # The next reading no longer finds the NVIDIA supplier link.
    orig = FakeLLM.extract
    monkeypatch.setattr(FakeLLM, "extract", lambda self, co, p: [] if "NVIDIA" in p.text else orig(self, co, p))
    c.post("/api/extract/TER", params={"refresh": True})
    assert c.jobs.wait("CIK:97210")["found"] == 1
    assert _edges(c) == {("CIK:97210", "competes_with", "ORG:advantest")}
