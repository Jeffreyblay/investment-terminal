"""End-to-end `itx company` with a fake EDGAR and fake LLM (no network)."""
import json
from types import SimpleNamespace

import terminal.cli as cli
import terminal.extract as extract
from terminal.edgar import Filing
from terminal.extract import Relationship, Tag

TICKERS = {"TER": {"cik": 2, "ticker": "TER", "name": "TERADYNE, INC"},
           "NVDA": {"cik": 1, "ticker": "NVDA", "name": "NVIDIA CORP"}}

DOC = """<html><body><p>Item 1. Business</p>
<p>We build collaborative robots. Our controllers use GPUs supplied by NVIDIA Corporation under a supply agreement with Teradyne.</p>
<p>Item 1A. Risk Factors</p><p>We compete with ABB Ltd and Fanuc Corporation in industrial automation markets worldwide.</p>
<p>Item 1B.</p></body></html>"""


class FakeEdgar:
    def tickers(self): return TICKERS
    def find(self, q): return TICKERS[q.upper()]
    def lookup(self, t): return TICKERS[t.upper()]
    def profile(self, cik): return {"sic": "3825", "sic_desc": "Instruments for measuring"}
    def company_facts(self, cik): return {"facts": {}}
    def get(self, url): return DOC
    def filings(self, ticker, limit=1):
        c = TICKERS[ticker]
        return [Filing(c["cik"], ticker, c["name"], "10-K", f"acc-{ticker}", "2026-02-01", "2025-12-31", "d.htm")]


class FakeLLM:
    model = "fake"
    def extract(self, company, passage):
        if "NVIDIA" in passage.text and "TERADYNE" in company:
            return [Relationship("NVIDIA Corporation", "supplies", company, "GPUs supplied by NVIDIA", 0.9,
                                 "GPUs", passage.section, passage.index)]
        return []


def test_company_command(tmp_path, monkeypatch):
    from terminal import store as store_mod
    monkeypatch.setattr(cli, "WEB_DATA", tmp_path / "web")
    monkeypatch.setattr(cli, "_edgar", lambda: FakeEdgar())
    monkeypatch.setattr(cli, "_llm", lambda model=None: FakeLLM())
    monkeypatch.setattr(extract, "tag_company",
                        lambda llm, company, text: [Tag("robotics", "maker", "We build collaborative robots.", 0.9)])
    orig = store_mod.Store.__init__
    monkeypatch.setattr(store_mod.Store, "__init__", lambda self, path=None: orig(self, tmp_path / "t.db"))

    cli.main(["company", "TER", "--depth", "1"])

    data = json.loads((tmp_path / "web" / "company-ter.json").read_text())
    assert data["mode"] == "company" and data["center"] == "CIK:2"
    ter = next(n for n in data["nodes"] if n["id"] == "CIK:2")
    assert ter["sic"] == "3825" and ter["tags"][0]["tag"] == "robotics"
    assert any(e["source"] == "CIK:1" and e["target"] == "CIK:2" for e in data["edges"])
    nvda = next(n for n in data["nodes"] if n["id"] == "CIK:1")
    assert nvda["expanded"]  # depth 1 processed the neighbour too
