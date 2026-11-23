"""Entity resolution: map names found in text ("TSMC", "Taiwan Semiconductor
Manufacturing Co., Ltd.") to one node id.

Order of attempts:
  1. manual aliases (config/aliases.yaml)  - you curate these over time
  2. exact match on normalised SEC company names / tickers
  3. fuzzy match (rapidfuzz) above a threshold
  4. otherwise a private / non-SEC entity: id "ORG:<normalised name>"
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from rapidfuzz import fuzz, process

from .config import ROOT

SUFFIXES = r"\b(incorporated|inc|corp|corporation|co|company|ltd|limited|plc|llc|lp|sa|ag|nv|se|" \
           r"holdings?|group|technologies|technology|the|class [a-c]|common stock|/de|/new)\b"


def normalize(name: str) -> str:
    n = name.lower().replace("&", " and ")
    n = re.sub(r"[^\w\s]", " ", n)
    n = re.sub(SUFFIXES, " ", n)
    return re.sub(r"\s+", " ", n).strip()


class Resolver:
    def __init__(self, sec_companies: dict[str, dict], aliases_path: Path | None = None,
                 threshold: int = 92):
        """sec_companies: TICKER -> {cik, ticker, name} (from Edgar.tickers())."""
        self.threshold = threshold
        self.by_ticker = {t.upper(): c for t, c in sec_companies.items()}
        self.by_norm: dict[str, dict] = {}
        for c in sec_companies.values():
            self.by_norm.setdefault(normalize(c["name"]), c)
        self._choices = list(self.by_norm.keys())
        path = aliases_path or ROOT / "config" / "aliases.yaml"
        raw = yaml.safe_load(path.read_text()) if path.exists() else {}
        self.aliases = {normalize(k): v for k, v in (raw or {}).items()}

    def resolve(self, name: str) -> dict:
        """Return {id, name, ticker, cik, listed, method}."""
        raw = name.strip()
        norm = normalize(raw)
        if not norm:
            return self._private(raw, norm)

        if norm in self.aliases:
            target = self.aliases[norm]
            if isinstance(target, str) and target.upper() in self.by_ticker:
                return self._listed(self.by_ticker[target.upper()], "alias")
            return self._private(str(target), normalize(str(target)), "alias")

        if raw.upper() in self.by_ticker and raw.isupper() and len(raw) <= 5:
            return self._listed(self.by_ticker[raw.upper()], "ticker")

        if norm in self.by_norm:
            return self._listed(self.by_norm[norm], "exact")

        if len(norm) >= 4 and self._choices:
            match = process.extractOne(norm, self._choices, scorer=fuzz.token_sort_ratio)
            if match and match[1] >= self.threshold:
                return self._listed(self.by_norm[match[0]], f"fuzzy:{match[1]:.0f}")

        return self._private(raw, norm)

    @staticmethod
    def _listed(c: dict, method: str) -> dict:
        return {"id": f"CIK:{c['cik']}", "name": c["name"], "ticker": c["ticker"],
                "cik": c["cik"], "listed": True, "method": method}

    @staticmethod
    def _private(raw: str, norm: str, method: str = "unresolved") -> dict:
        return {"id": f"ORG:{norm or raw.lower()}", "name": raw, "ticker": None, "cik": None,
                "listed": False, "method": method}
