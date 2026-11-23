"""SEC EDGAR client using only free, official endpoints.

Endpoints (no API key, but a User-Agent with name + email is required):
  https://www.sec.gov/files/company_tickers.json          ticker -> CIK -> name
  https://data.sec.gov/submissions/CIK##########.json     filing index per company
  https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json   XBRL financials
  https://www.sec.gov/Archives/edgar/data/<cik>/<acc>/<doc>       filing documents
  https://efts.sec.gov/LATEST/search-index?q=...          full-text search (2001+)

Everything is cached on disk so re-runs cost no requests.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .config import settings


class EdgarError(RuntimeError):
    pass


@dataclass
class Filing:
    cik: int
    ticker: str
    company: str
    form: str
    accession: str
    filed: str
    period: str
    primary_doc: str

    @property
    def url(self) -> str:
        acc = self.accession.replace("-", "")
        return f"https://www.sec.gov/Archives/edgar/data/{self.cik}/{acc}/{self.primary_doc}"


class Edgar:
    def __init__(self, user_agent: str | None = None, max_rps: float | None = None,
                 cache_dir: Path | None = None):
        self.user_agent = user_agent or settings.sec_user_agent
        if not self.user_agent or "@" not in self.user_agent:
            raise EdgarError(
                "Set SEC_USER_AGENT in .env to 'Your Name your@email.com' (SEC requirement)."
            )
        self.min_interval = 1.0 / (max_rps or settings.sec_max_rps)
        self.cache_dir = cache_dir or settings.cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent,
                                     "Accept-Encoding": "gzip, deflate"})
        self._tickers: dict[str, dict] | None = None

    # ---------- low level ----------
    def _cache_path(self, url: str) -> Path:
        h = hashlib.sha1(url.encode()).hexdigest()[:16]
        suffix = ".json" if url.endswith(".json") or "efts" in url else ".html"
        return self.cache_dir / f"{h}{suffix}"

    def get(self, url: str, use_cache: bool = True, max_age_days: float | None = None) -> str:
        path = self._cache_path(url)
        if use_cache and path.exists():
            age = (time.time() - path.stat().st_mtime) / 86400
            if max_age_days is None or age < max_age_days:
                return path.read_text(encoding="utf-8", errors="ignore")
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(4):
            self._last = time.time()
            r = self.session.get(url, timeout=30)
            if r.status_code == 200:
                path.write_text(r.text, encoding="utf-8")
                return r.text
            if r.status_code in (429, 503):
                time.sleep(2 ** attempt)
                continue
            raise EdgarError(f"{r.status_code} for {url}")
        raise EdgarError(f"Gave up after retries: {url}")

    def get_json(self, url: str, **kw) -> dict:
        return json.loads(self.get(url, **kw))

    # ---------- companies ----------
    def tickers(self) -> dict[str, dict]:
        """Map TICKER -> {cik, ticker, name}. Refreshed weekly."""
        if self._tickers is None:
            raw = self.get_json("https://www.sec.gov/files/company_tickers.json", max_age_days=7)
            self._tickers = {
                v["ticker"].upper(): {"cik": int(v["cik_str"]), "ticker": v["ticker"].upper(),
                                      "name": v["title"]}
                for v in raw.values()
            }
        return self._tickers

    def lookup(self, ticker: str) -> dict:
        t = self.tickers().get(ticker.upper())
        if not t:
            raise EdgarError(f"Ticker {ticker} not found in SEC list (not an SEC filer?)")
        return t

    def submissions(self, cik: int) -> dict:
        return self.get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", max_age_days=1)

    def find(self, query: str) -> dict:
        """Ticker or company name -> {cik, ticker, name}. Exact ticker first, then fuzzy name."""
        q = query.strip()
        t = self.tickers().get(q.upper())
        if t:
            return t
        from rapidfuzz import fuzz, process
        from .resolve import normalize
        by_norm = {normalize(c["name"]): c for c in self.tickers().values()}
        hit = process.extractOne(normalize(q), list(by_norm), scorer=fuzz.WRatio)
        if not hit or hit[1] < 85:
            raise EdgarError(f"No SEC filer matches '{query}'")
        return by_norm[hit[0]]

    def profile(self, cik: int) -> dict:
        """Company profile from the submissions file: SIC code, description, exchanges."""
        sub = self.submissions(cik)
        return {"cik": cik, "name": sub.get("name"), "sic": sub.get("sic") or None,
                "sic_desc": sub.get("sicDescription") or None,
                "tickers": sub.get("tickers", []), "exchanges": sub.get("exchanges", [])}

    def filings(self, ticker: str, forms=("10-K", "20-F"), limit: int = 3) -> list[Filing]:
        """Most recent annual reports (10-K for US companies, 20-F for foreign filers)."""
        info = self.lookup(ticker)
        sub = self.submissions(info["cik"])
        recent = sub["filings"]["recent"]
        out: list[Filing] = []
        for i, form in enumerate(recent["form"]):
            if form in forms:
                out.append(Filing(
                    cik=info["cik"], ticker=info["ticker"], company=sub.get("name", info["name"]),
                    form=form, accession=recent["accessionNumber"][i],
                    filed=recent["filingDate"][i], period=recent["reportDate"][i],
                    primary_doc=recent["primaryDocument"][i],
                ))
            if len(out) >= limit:
                break
        return out

    def document(self, filing: Filing) -> str:
        return self.get(filing.url)

    def company_facts(self, cik: int) -> dict:
        return self.get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
                             max_age_days=7)

    def full_text_search(self, phrase: str, forms: str = "10-K",
                         start: str | None = None, end: str | None = None) -> list[dict]:
        """EDGAR full-text search. Returns [{cik, company, form, filed}] hits (first page)."""
        from urllib.parse import urlencode
        params = {"q": f'"{phrase}"', "forms": forms}
        if start and end:
            params.update({"dateRange": "custom", "startdt": start, "enddt": end})
        url = "https://efts.sec.gov/LATEST/search-index?" + urlencode(params)
        data = self.get_json(url, max_age_days=7)
        hits = []
        for h in data.get("hits", {}).get("hits", []):
            src = h.get("_source", {})
            names = src.get("display_names") or [""]
            ciks = src.get("ciks") or [None]
            hits.append({"cik": int(ciks[0]) if ciks[0] else None, "company": names[0],
                         "form": src.get("form"), "filed": src.get("file_date")})
        return hits


# XBRL concept names to try, in order, for headline financials.
XBRL_CONCEPTS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "SalesRevenueNet"],
    "net_income": ["NetIncomeLoss"],
    "rnd": ["ResearchAndDevelopmentExpense"],
}


def headline_financials(facts: dict) -> dict:
    """Latest annual (10-K, FY) USD value per metric from a companyfacts payload."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    out = {}
    for metric, concepts in XBRL_CONCEPTS.items():
        for c in concepts:
            units = gaap.get(c, {}).get("units", {}).get("USD", [])
            annual = [u for u in units if u.get("fp") == "FY" and u.get("form", "").startswith("10-K")]
            if annual:
                latest = max(annual, key=lambda u: u.get("end", ""))
                out[metric] = {"value": latest["val"], "period_end": latest["end"], "concept": c}
                break
    return out
