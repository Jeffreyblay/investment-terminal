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
import threading
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
        self._lock = threading.Lock()
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
        # One request at a time across threads (the API serves searches in parallel),
        # so the SEC rate limit holds no matter how many searches are running.
        with self._lock:
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

    def _names(self) -> dict[str, dict]:
        """Normalised company name -> {cik, ticker, name}, built once."""
        if getattr(self, "_by_norm", None) is None:
            from .resolve import normalize
            self._by_norm = {}
            for c in self.tickers().values():
                self._by_norm.setdefault(normalize(c["name"]), c)
        return self._by_norm

    def find(self, query: str) -> dict:
        """Ticker or company name -> {cik, ticker, name}. Exact ticker first, then fuzzy name."""
        q = query.strip()
        t = self.tickers().get(q.upper())
        if t:
            return t
        from rapidfuzz import fuzz, process
        from .resolve import normalize
        names = self._names()
        hit = process.extractOne(normalize(q), list(names), scorer=fuzz.WRatio)
        if not hit or hit[1] < 85:
            raise EdgarError(f"No SEC filer matches '{query}'")
        return names[hit[0]]

    def search(self, query: str, limit: int = 8) -> list[dict]:
        """Several matches for a search box: exact ticker, ticker prefix, then fuzzy name."""
        from rapidfuzz import fuzz, process
        from .resolve import normalize
        q = query.strip()
        if not q:
            return []
        out: dict[int, dict] = {}            # by CIK, keeps insertion order
        tickers = self.tickers()
        if q.upper() in tickers:
            out[tickers[q.upper()]["cik"]] = tickers[q.upper()]
        for t, c in tickers.items():          # SEC file is roughly largest-first
            if len(out) >= limit:
                break
            if t.startswith(q.upper()):
                out.setdefault(c["cik"], c)
        names = self._names()
        for name, score, _ in process.extract(normalize(q), list(names), scorer=fuzz.WRatio,
                                              limit=limit):
            if score >= 70:
                out.setdefault(names[name]["cik"], names[name])
        return list(out.values())[:limit]

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


# XBRL metrics from the annual financial statements (Item 8).
#   concepts: us-gaap names to try. Companies tag the same thing differently, and many
#             switched names over time (e.g. Revenues -> RevenueFromContract... after 2018),
#             so all of them are merged into one series. Earlier names win on a tie.
#   kind:     "duration" = measured over a period (income / cash flow statement), must be ~1 year
#             "instant"  = a balance at one date (balance sheet)
#   unit:     XBRL unit key
XBRL_METRICS: dict[str, dict] = {
    # Income statement
    "revenue": {"kind": "duration", "unit": "USD", "concepts": [
        "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
        "SalesRevenueGoodsNet"]},
    "gross_profit": {"kind": "duration", "unit": "USD", "concepts": ["GrossProfit"]},
    "operating_income": {"kind": "duration", "unit": "USD", "concepts": ["OperatingIncomeLoss"]},
    "net_income": {"kind": "duration", "unit": "USD", "concepts": ["NetIncomeLoss"]},
    "rnd": {"kind": "duration", "unit": "USD", "concepts": [
        "ResearchAndDevelopmentExpense",
        "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"]},
    "eps_diluted": {"kind": "duration", "unit": "USD/shares", "concepts": ["EarningsPerShareDiluted"]},
    # Balance sheet
    "cash": {"kind": "instant", "unit": "USD", "concepts": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]},
    "total_assets": {"kind": "instant", "unit": "USD", "concepts": ["Assets"]},
    "total_liabilities": {"kind": "instant", "unit": "USD", "concepts": ["Liabilities"]},
    "equity": {"kind": "instant", "unit": "USD", "concepts": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]},
    "long_term_debt": {"kind": "instant", "unit": "USD", "concepts": [
        "LongTermDebtNoncurrent", "LongTermDebt"]},
    # Cash flow statement
    "operating_cash_flow": {"kind": "duration", "unit": "USD", "concepts": [
        "NetCashProvidedByUsedInOperatingActivities"]},
    "capex": {"kind": "duration", "unit": "USD", "concepts": [
        "PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"]},
}

ANNUAL_FORMS = ("10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A")


def _days(start: str, end: str) -> int:
    from datetime import date
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def annual_series(facts: dict, years: int = 5) -> dict[str, list[dict]]:
    """Per metric, the last `years` fiscal-year values, newest first:
    {"revenue": [{"period_end", "value", "concept", "filed", "accession"}, ...], ...}

    - Merges every concept name for a metric, so a company that changed tags still
      gets an up-to-date series.
    - Duration metrics keep only ~12-month periods (drops quarters inside 10-Ks).
    - If the same period appears in several filings, the latest-filed value wins (restatements).
    """
    gaap = facts.get("facts", {}).get("us-gaap", {})
    out: dict[str, list[dict]] = {}
    for metric, spec in XBRL_METRICS.items():
        by_end: dict[str, dict] = {}
        for rank, concept in enumerate(spec["concepts"]):
            for u in gaap.get(concept, {}).get("units", {}).get(spec["unit"], []):
                if u.get("form") not in ANNUAL_FORMS or "end" not in u or "val" not in u:
                    continue
                if spec["kind"] == "duration":
                    if "start" not in u or not 350 <= _days(u["start"], u["end"]) <= 380:
                        continue
                cand = {"period_end": u["end"], "value": u["val"], "concept": concept,
                        "filed": u.get("filed", ""), "accession": u.get("accn"), "_rank": rank}
                cur = by_end.get(u["end"])
                # Keep the preferred concept; within the same concept, the latest filing.
                if (cur is None or rank < cur["_rank"]
                        or (rank == cur["_rank"] and cand["filed"] > cur["filed"])):
                    by_end[u["end"]] = cand
        if by_end:
            rows = sorted(by_end.values(), key=lambda r: r["period_end"], reverse=True)[:years]
            for r in rows:
                r.pop("_rank")
            out[metric] = rows
    return _drop_discontinued(out)


def _drop_discontinued(out: dict[str, list[dict]], max_gap_days: int = 400) -> dict[str, list[dict]]:
    """Drop metrics whose newest value is much older than the company's latest fiscal year.
    That happens when a company stopped using every tag we know for a metric (e.g. NVIDIA's capex
    tag ended in 2012). Showing that old number next to this year's figures would be misleading."""
    if not out:
        return out
    newest = max(rows[0]["period_end"] for rows in out.values())
    return {m: rows for m, rows in out.items() if _days(rows[0]["period_end"], newest) <= max_gap_days}


def headline_from_series(series: dict[str, list[dict]]) -> dict:
    """Latest fiscal-year value per metric: {"revenue": {"value", "period_end", "concept"}, ...}.
    This is what the viewer's company panel shows."""
    return {m: {"value": s[0]["value"], "period_end": s[0]["period_end"], "concept": s[0]["concept"]}
            for m, s in series.items() if s}


def headline_financials(facts: dict) -> dict:
    return headline_from_series(annual_series(facts, years=1))
