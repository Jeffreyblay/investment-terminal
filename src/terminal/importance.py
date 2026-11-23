"""How important is each relationship? Read from the whole annual report, with rules (no LLM).

The sentence the model quotes for a link rarely carries the numbers: Symbotic's "Walmart pays us for
operation services" link is backed elsewhere by "Walmart accounted for approximately 87% of total
revenue" (often in the financial-statement notes). So after extraction, every customer / supplier link
of a filing is looked up in the full text:

  filer -> customer X : a sentence naming X with "N%" and revenue/sales  -> major (revenue_pct = N, if N >= 10)
                        or "largest / major / significant / principal customer" -> major
  supplier X -> filer : a sentence naming X with "sole source", "single source", "sole supplier" -> single_source

Anonymous disclosures ("Customer A accounted for 22% of revenue") can't be tied to a name and are skipped.
"""
from __future__ import annotations

import re

from .resolve import normalize

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"“])")
# A percentage counts only when it is the customer's share:
#   "X accounted for approximately 87% ..."   (verb between the name and the number)
#   "... 87% of our total revenue ..."         (number tied to revenue / sales)
# so "Revenue grew 26% ... and Kroger is a new customer" doesn't make Kroger a 26% customer.
_SHARE_AFTER_NAME = re.compile(
    r"^[^;]{0,120}?\b(?:accounted\s+for|represented|comprised|contributed|constituted|made\s+up)\b"
    r"[^;%]{0,60}?(\d{1,3}(?:\.\d+)?)\s*(?:%|percent\b)", re.I)
_REVENUE_SOON = re.compile(r"\b(?:revenues?|net\s+sales|sales)\b", re.I)
_SHARE_OF_REVENUE = re.compile(
    r"(\d{1,3}(?:\.\d+)?)\s*(?:%|percent)\s+of\s+(?:our\s+|its\s+|the\s+company's\s+)?"
    r"(?:total\s+|net\s+|consolidated\s+)?(?:revenues?|net\s+sales|sales)\b", re.I)
_MAJOR_WORDS = re.compile(r"\b(?:largest|major|significant|principal|key)\s+customers?\b", re.I)
_SINGLE = re.compile(r"\b(?:sole|single)[\s-]+(?:source|supplier|manufacturer|provider|vendor)s?\b"
                     r"|\bonly\s+(?:source|supplier|manufacturer)\b", re.I)

SINGLE, MAJOR = "single_source", "major"


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(" ".join(text.split())) if s.strip()]


def _key_word(name: str) -> str | None:
    words = [w for w in normalize(name).split() if len(w) >= 3] or normalize(name).split()
    return words[0] if words else None


def customer_importance(name: str, sents: list[str]) -> tuple[str | None, float | None]:
    """(MAJOR, pct) if the report singles the customer out, else (None, None)."""
    word = _key_word(name)
    if not word:
        return None, None
    pat = re.compile(r"\b" + re.escape(word) + r"\b", re.I)
    best, major = None, False
    for s in sents:
        m = pat.search(s)
        if not m:
            continue
        # The first share after the name is the latest year ("87%, 88% and 89% for fiscal 2025, 2024 ...").
        # With "accounted for / represented", revenue or sales must follow the number: "Walmart, which
        # represented a 6.5% ownership" is a stake in the company, not a revenue share.
        after = s[m.end():]
        share = _SHARE_AFTER_NAME.search(after)
        if share and not _REVENUE_SOON.search(after[share.end(): share.end() + 80]):
            share = None
        share = share or _SHARE_OF_REVENUE.search(s)
        if share:
            v = float(share.group(1))
            if 0 < v <= 100 and (best is None or v > best):
                best = v
        if _MAJOR_WORDS.search(s):
            major = True
    if best is not None and best >= 10:
        return MAJOR, best
    if major:
        return MAJOR, best
    return None, None


def supplier_importance(name: str, sents: list[str]) -> str | None:
    """SINGLE if the report calls this supplier a sole / single source."""
    word = _key_word(name)
    if not word:
        return None
    pat = re.compile(r"\b" + re.escape(word) + r"\b", re.I)
    return SINGLE if any(pat.search(s) and _SINGLE.search(s) for s in sents) else None


def enrich_filing(st, accession: str, filer_id: str, text: str) -> int:
    """Set importance / revenue_pct on every supply link of one filing, from its full text.
    Returns how many links got an importance."""
    sents = sentences(text)
    rows = st.db.execute(
        """SELECT r.id, r.relation, r.source_id, r.target_id, s.name AS src_name, t.name AS tgt_name
           FROM relationships r JOIN entities s ON s.id = r.source_id JOIN entities t ON t.id = r.target_id
           WHERE r.accession = ? AND r.relation IN ('supplies', 'customer_of')""", (accession,)).fetchall()
    n = 0
    for r in rows:
        imp, pct = None, None
        # supplies is stored seller -> buyer; customer_of is buyer -> seller.
        if r["relation"] == "supplies":
            seller, buyer, seller_name, buyer_name = r["source_id"], r["target_id"], r["src_name"], r["tgt_name"]
        else:
            seller, buyer, seller_name, buyer_name = r["target_id"], r["source_id"], r["tgt_name"], r["src_name"]
        if seller == filer_id:                     # the filer sells to them: a customer
            imp, pct = customer_importance(buyer_name, sents)
        elif buyer == filer_id:                    # they sell to the filer: a supplier
            imp = supplier_importance(seller_name, sents)
        st.db.execute("UPDATE relationships SET importance=?, revenue_pct=? WHERE id=?", (imp, pct, r["id"]))
        n += imp is not None
    return n
