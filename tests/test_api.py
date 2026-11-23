"""The local web API, with a fake EDGAR (no network) and a temporary database."""
import pytest

pytest.importorskip("fastapi")   # skip if the [api] extra isn't installed
from fastapi.testclient import TestClient

from terminal import api
from terminal.edgar import EdgarError, Filing
from terminal.store import Store

TICKERS = {"TER": {"cik": 97210, "ticker": "TER", "name": "TERADYNE, INC"},
           "TXN": {"cik": 97476, "ticker": "TXN", "name": "TEXAS INSTRUMENTS INC"}}

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


@pytest.fixture
def client(tmp_path):
    fake = FakeEdgar()
    api.app.dependency_overrides[api.get_edgar] = lambda: fake

    def tmp_store():
        st = Store(tmp_path / "t.db")
        try:
            yield st
        finally:
            st.close()
    api.app.dependency_overrides[api.get_store] = tmp_store
    yield TestClient(api.app), fake
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
