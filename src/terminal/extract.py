"""Relationship extraction with a free local LLM served by Ollama.

Ollama's /api/chat accepts a JSON schema in `format`, so the model is forced
to return valid, typed JSON. Every relationship keeps the exact evidence
sentence so it can be checked against the filing later.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict

import requests

from .config import settings
from .sections import Passage

RELATIONS = [
    "supplies",          # subject supplies goods/services to object
    "customer_of",       # subject buys from object
    "competes_with",
    "partners_with",     # JV, collaboration, licensing
    "subsidiary_of",
    "acquired",
    "invested_in",
    "contracted_by",     # e.g. government contract awarded by object
    "manufactures_for",  # contract manufacturer / foundry
]

SCHEMA = {
    "type": "object",
    "properties": {
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "relation": {"type": "string", "enum": RELATIONS},
                    "object": {"type": "string"},
                    "component": {"type": "string"},
                    "evidence": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["subject", "relation", "object", "evidence", "confidence"],
            },
        }
    },
    "required": ["relationships"],
}

SYSTEM = """You extract business relationships between NAMED companies from SEC filing text.
Rules:
- The filer is "{company}". Use "{company}" as subject or object when the text says "we", "our" or "the Company".
- Every relationship must involve "{company}" on one side and a specific, NAMED organisation on the other
  (a company, government agency or university with a proper name, e.g. "Advantest Corporation", "NASA").
- Do NOT extract: unnamed or generic groups ("our five largest customers", "a supplier in Asia", "OEMs", "IDMs",
  "contract manufacturers", "third-party vendors"), product or brand names (they are not companies),
  countries or regions, industries or markets, or "doing business in" a place.
- Direction matters:
    supplies         = subject SELLS to object   ("NVIDIA supplies GPUs to {company}" -> NVIDIA supplies {company})
    customer_of      = subject BUYS from object  ("{company} buys reducers from Nabtesco" -> {company} customer_of Nabtesco)
    If "{company}" sells to a named customer, write "{company}" supplies <customer>.
- relation must be one of: {relations}.
- component: the product or part involved if stated (e.g. "GPUs", "lidar sensors"), else "".
- evidence: copy the exact sentence from the text that supports it.
- confidence: 0.0-1.0. Use 0.9+ only when the sentence names both parties and states the relationship outright;
  0.5-0.8 when it is implied.
- If there are none, return {{"relationships": []}}."""


@dataclass
class Relationship:
    subject: str
    relation: str
    object: str
    evidence: str
    confidence: float
    component: str = ""
    section: str = ""
    passage_index: int = -1

    def to_dict(self) -> dict:
        return asdict(self)


class OllamaExtractor:
    def __init__(self, model: str | None = None, url: str | None = None, timeout: int = 180):
        self.model = model or settings.ollama_model
        self.url = (url or settings.ollama_url).rstrip("/")
        self.timeout = timeout

    def check(self) -> None:
        try:
            r = requests.get(f"{self.url}/api/tags", timeout=5)
            r.raise_for_status()
        except requests.RequestException as e:
            raise SystemExit(f"Ollama not reachable at {self.url}. Install from ollama.com and run "
                             f"`ollama pull {self.model}`. ({e})")
        names = [m.get("name", "") for m in r.json().get("models", [])]
        if not any(n.split(":")[0] == self.model.split(":")[0] for n in names):
            raise SystemExit(f"Model {self.model} not pulled. Run: ollama pull {self.model}")

    def extract(self, company: str, passage: Passage) -> list[Relationship]:
        body = {
            "model": self.model,
            "stream": False,
            "format": SCHEMA,
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": SYSTEM.format(company=company,
                                                            relations=", ".join(RELATIONS))},
                {"role": "user", "content": passage.text},
            ],
        }
        r = requests.post(f"{self.url}/api/chat", json=body, timeout=self.timeout)
        r.raise_for_status()
        content = r.json().get("message", {}).get("content", "{}")
        return parse_response(content, passage, company)


# ---------- quality filters (no LLM) ----------
# Small models often ignore "skip unnamed counterparties", so these rules enforce it after the fact.

# Lowercase words allowed inside a real organisation name ("Bank of America", "Rohde & Schwarz").
_NAME_CONNECTORS = {"of", "and", "the", "for", "de", "du", "der", "und", "&", "a", "an", "in", "on", "at"}
# Leading words that mark a generic phrase, not a name ("Five largest customers", "Certain suppliers").
_GENERIC_LEAD = {"a", "an", "the", "our", "its", "their", "certain", "other", "various", "several", "some", "many",
                 "one", "two", "three", "four", "five", "additional", "major", "key", "significant", "large",
                 "largest", "leading", "third-party", "independent", "local", "global", "foreign", "domestic"}
# Bare category nouns that the model sometimes returns as if they were companies.
_GENERIC_NAMES = {"company", "the company", "customer", "customers", "supplier", "suppliers", "vendor", "vendors",
                  "distributor", "distributors", "partner", "partners", "competitor", "competitors", "oem", "oems",
                  "idm", "idms", "osat", "osats", "foundry", "foundries", "fabless", "government", "products",
                  "subcontractors", "contract manufacturers", "u.s. government", "us government", "china", "asia",
                  "europe", "japan", "taiwan", "korea", "united states"}


def is_named_org(name: str) -> bool:
    """True if `name` looks like a specific organisation, not a generic group or phrase."""
    n = " ".join(name.replace("’", "'").split())
    if len(n) < 2 or n.lower() in _GENERIC_NAMES:
        return False
    words = [w.strip(",.()") for w in n.split()]
    if words[0].lower() in _GENERIC_LEAD:
        return False
    if re.fullmatch(r"[A-Z]{2,6}s", n):                  # plural acronyms: "OEMs", "IDMs", "OSATs"
        return False
    # Every word in a proper name starts uppercase/digit ("iTest", "eBay" style is fine),
    # apart from short connectors. Rejects "business in China", "customer of our ... segment".
    for w in words:
        if w and w[0].islower() and w.lower() not in _NAME_CONNECTORS and not re.match(r"[a-z][A-Z]", w):
            return False
    return not (words[0].lower() in _NAME_CONNECTORS)    # can't start with "and", "of", ...


def is_filer(name: str, company: str) -> bool:
    """Does `name` refer to the filer itself ("Teradyne", "TERADYNE, INC", "we")?"""
    from rapidfuzz import fuzz
    from .resolve import normalize
    if name.strip().lower() in {"we", "us", "our", "the company"}:
        return True
    a, b = normalize(name), normalize(company)
    return bool(a and b) and (a == b or fuzz.token_set_ratio(a, b) >= 90)


def keep_relationship(subject: str, obj: str, company: str | None, passage_text: str | None = None) -> bool:
    """The rules every stored relationship must pass:
    - with a known filer: one side is the filer and the other is a named organisation
      (a 10-K describes the filer's own relationships; this drops "J750 supplies microcontrollers")
    - the counterparty's name actually appears in the passage (hallucination guard), when we have it."""
    if company:
        if is_filer(subject, company):
            other = obj
        elif is_filer(obj, company):
            other = subject
        else:
            return False
        if is_filer(other, company):
            return False                                 # self-loop
        if not is_named_org(other):
            return False
        return passage_text is None or _mentioned(other, passage_text)
    # Filer unknown: at least one side must be a named organisation found in the passage.
    return any(is_named_org(x) and (passage_text is None or _mentioned(x, passage_text))
               for x in (subject, obj))


# The evidence sentence must contain a cue for the relation it supports. Drops cases like
# "Photon can be configured as an upper stage" (a product description) or "we launched from
# NASA Wallops" (a place), which the model labelled `supplies`.
# supplies and customer_of are the same link seen from either end, so they share one cue list
# (review() may turn one into the other when it fixes the direction).
_SUPPLY_CUES = (r"suppl|customer|client|sell|sold|sale|purchas|buy|bought|order|contract|deliver|provid|revenue"
                r"|vendor|sourc|distribut|ship|launch\w* for|award|use[sd]?|depend|rel(?:y|ies|ied|iance) (?:on|upon)"
                r"|manufactur|fabricat|foundr|assembl|pays|paid")
RELATION_CUES = {rel: re.compile(r"\b(?:" + pat + ")", re.I) for rel, pat in {
    "supplies": _SUPPLY_CUES,
    "customer_of": _SUPPLY_CUES,
    "manufactures_for": r"manufactur|foundr|fabricat|assembl|produc|suppl|contract",
    "competes_with": r"compet|rival|alternative|market share",
    "partners_with": r"partner|collaborat|joint venture|alliance|agreement|licens|work(?:s|ed|ing)? (?:closely )?with"
                     r"|team|integrat|cooperat",
    "acquired": r"acqui|merger|merged|purchase of",
    "invested_in": r"invest|stake|equity|shares|stock|ownership",
    "subsidiary_of": r"subsidiar|owned|parent|unit of|division of",
    "contracted_by": r"contract|award|task order|agreement|selected|funded|grant",
}.items()}

_ACQ = re.compile(r"\b(acquisitions? of|acquired by|acquired)\b", re.I)


def _acquisition_side(name: str, evidence: str) -> str | None:
    """'filer' if the evidence says the filer acquired `name` ("our acquisition of X", "we acquired X"),
    'other' if `name` acquired the filer ("we were acquired by X"), else None."""
    from .resolve import normalize
    words = [w for w in normalize(name).split() if len(w) >= 3] or normalize(name).split()
    if not words:
        return None
    for m in _ACQ.finditer(evidence):
        after = evidence[m.end(): m.end() + 300]
        if re.search(r"\b" + re.escape(words[0]) + r"\b", after, re.I):
            return "other" if m.group(1).lower() == "acquired by" else "filer"
    return None


def _key_word(name: str) -> str | None:
    """First distinctive word of a name, for finding it in a sentence ("SK Hynix Inc." -> "hynix")."""
    from .resolve import normalize
    words = [w for w in normalize(name).split() if len(w) >= 3] or normalize(name).split()
    return words[0] if words else None


# Who buys from whom, read from the sentence. Each pattern ends where the counterparty's name may follow.
_FILER_BUYS = re.compile(            # "we purchase memory from X", "supplied by X", "we rely on X for"
    r"\b(?:(?:purchas\w*|buy\w*|bought|sourc\w*|procur\w*|obtain\w*|licens\w*|order\w*)\b[^.;]{0,150}?\bfrom"
    r"|(?:supplied|manufactured|fabricated|produced|provided|assembled|made)\s+(?:for\s+us\s+)?by"
    r"|(?:rel(?:y|ies|ied)|depend\w*)\s+(?:on|upon))\b", re.I)
_SUPPLIER_LIST = re.compile(         # "foundries, such as X", "suppliers including X"
    r"\b(?:foundr\w*|suppliers?|vendors?|subcontractors?|(?:contract\s+)?manufacturers?)\b,?\s+"
    r"(?:such\s+as|including|like)\b", re.I)
_FILER_USES = re.compile(r"\b(?:us(?:e|es|ed|ing)|utiliz\w*|engag\w*|employ\w*)\b", re.I)
_TO_MAKE = re.compile(                # "... to produce our wafers", "... to manufacture our products"
    r"\bto\s+(?:produce|manufacture|make|fabricate|build|assemble|test|package)\b", re.I)
_FILER_SELLS = re.compile(           # "sold to X", "sales to X", "shipments to X"
    r"\b(?:sold|sell\w*|sales|ship\w*|deliver\w*|licensed)\b[^.;]{0,60}?\bto\b", re.I)
_OTHER_BUYS_AFTER = re.compile(      # "X accounted for 14% of revenue", "X purchases our", "X pays us"
    r"^[^;]{0,80}?\b(?:accounted for|represented|purchas\w*|bought|buys|ordered|orders|pays|paid)\b", re.I)
    # (dots allowed: names like "Walmart Inc." contain them; the evidence is a single sentence anyway)


def _seller(other: str, evidence: str) -> str | None:
    """'other' if the sentence says the counterparty sells to the filer, 'filer' if the filer sells
    to the counterparty, None if the sentence doesn't say."""
    word = _key_word(other)
    if not word:
        return None
    name = re.compile(r"\b" + re.escape(word) + r"\b", re.I)
    for m in _FILER_BUYS.finditer(evidence):
        if name.search(evidence[m.end(): m.end() + 200]):
            return "other"
    for m in _SUPPLIER_LIST.finditer(evidence):
        if name.search(evidence[m.end(): m.end() + 200]):
            return "other"
    for m in _FILER_USES.finditer(evidence):     # "we utilize X ... to produce our wafers"
        after = evidence[m.end(): m.end() + 250]
        hit = name.search(after)
        if hit and _TO_MAKE.search(after[hit.end():]):
            return "other"
    for m in _FILER_SELLS.finditer(evidence):
        if name.search(evidence[m.end(): m.end() + 120]):
            return "filer"
    for m in name.finditer(evidence):
        if _OTHER_BUYS_AFTER.search(evidence[m.end():]):
            return "filer"
    return None


def review(rel: Relationship, company: str | None, passage_text: str | None = None) -> Relationship | None:
    """Every quality rule in one place; returns the relationship (possibly relabelled) or None to drop it.
    1. keep_relationship: filer on one side, a named organisation on the other, found in the passage
    2. acquisitions: "our acquisition of X" becomes <filer> acquired X, whatever the model called it
    3. supply direction: for supplies / customer_of, the sentence decides who sells to whom
       ("we purchase memory from X" -> X supplies <filer>), stored as <seller> supplies <buyer>
    4. the evidence sentence must contain a cue for the relation (RELATION_CUES)"""
    from dataclasses import replace
    if not keep_relationship(rel.subject, rel.object, company, passage_text):
        return None
    ev = rel.evidence or ""
    if company:
        other = rel.object if is_filer(rel.subject, company) else rel.subject
        side = _acquisition_side(other, ev)
        if side == "filer":
            rel = replace(rel, relation="acquired", subject=company, object=other)
        elif side == "other":
            rel = replace(rel, relation="acquired", subject=other, object=company)
        elif rel.relation in ("supplies", "customer_of"):
            seller = _seller(other, ev)
            if seller == "other":
                rel = replace(rel, relation="supplies", subject=other, object=company)
            elif seller == "filer":
                rel = replace(rel, relation="supplies", subject=company, object=other)
    cue = RELATION_CUES.get(rel.relation)
    if cue is None:
        return None
    if rel.relation == "competes_with":
        # Competitor lists are often introduced elsewhere in the paragraph ("Companies that have
        # introduced products in the field ... include: A; B; C"), so the cue may be anywhere in the
        # passage. Without the passage (itx clean) competitors aren't dropped on this rule.
        if passage_text is not None and not cue.search(ev + " " + passage_text):
            return None
    elif not cue.search(ev):
        return None
    return rel


def parse_response(content: str, passage: Passage, company: str | None = None) -> list[Relationship]:
    """Validate model JSON; drop anything malformed, generic, or unsupported by the passage."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []
    out = []
    for item in data.get("relationships", []):
        try:
            rel = Relationship(
                subject=str(item["subject"]).strip(),
                relation=str(item["relation"]).strip(),
                object=str(item["object"]).strip(),
                evidence=str(item.get("evidence", "")).strip(),
                confidence=max(0.0, min(1.0, float(item.get("confidence", 0)))),
                component=str(item.get("component", "") or "").strip(),
                section=passage.section,
                passage_index=passage.index,
            )
        except (KeyError, TypeError, ValueError):
            continue
        if rel.relation not in RELATIONS or not rel.subject or not rel.object:
            continue
        if rel.subject.lower() == rel.object.lower():
            continue
        rel = review(rel, company, passage.text)
        if rel:
            out.append(rel)
    return out


# ---------- industry tagging (company-level search) ----------

TAG_SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string"},
                    "role": {"type": "string", "enum": ["supplier", "maker", "customer", "operator", "platform"]},
                    "evidence": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["tag", "role", "evidence", "confidence"],
            },
        }
    },
    "required": ["tags"],
}

TAG_SYSTEM = """You classify which industries a company participates in, from its own filing.
Return 1-8 industry tags for "{company}".
- tag: a short, common industry name in lower case, 1-3 words (e.g. "robotics", "semiconductors",
  "space", "electric vehicles", "data centers", "medical devices", "defense", "cloud software").
  Prefer these when they fit: {vocab}.
- role: how it participates: supplier (sells parts/components into it), maker (builds the core product),
  customer (major buyer), operator (runs services on it), platform (software/compute others build on).
- evidence: the exact sentence that shows it.
- confidence: 0.0-1.0.
Include cross-industry roles: a chip maker that sells into robots gets "robotics" with role "supplier"."""

TAG_VOCAB = ["robotics", "semiconductors", "space", "defense", "electric vehicles", "automotive",
             "data centers", "cloud software", "medical devices", "industrial automation",
             "telecommunications", "energy", "batteries", "aerospace", "logistics", "retail",
             "consumer electronics", "ai compute", "sensors", "materials"]

SYNONYMS = {"robots": "robotics", "robot": "robotics", "semiconductor": "semiconductors",
            "chips": "semiconductors", "aerospace and defense": "aerospace", "evs": "electric vehicles",
            "ev": "electric vehicles", "artificial intelligence": "ai compute", "ai": "ai compute",
            "satellites": "space", "satellite": "space", "space economy": "space"}


@dataclass
class Tag:
    tag: str
    role: str
    evidence: str
    confidence: float


def normalize_tag(t: str) -> str:
    t = " ".join(t.lower().replace("_", " ").replace("-", " ").split())
    return SYNONYMS.get(t, t)


def parse_tags(content: str) -> list[Tag]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []
    out: dict[str, Tag] = {}
    for item in data.get("tags", []):
        try:
            tag = normalize_tag(str(item["tag"]))
            conf = max(0.0, min(1.0, float(item.get("confidence", 0))))
            role = str(item.get("role", "maker"))
        except (KeyError, TypeError, ValueError):
            continue
        if not tag or len(tag) > 40 or len(tag.split()) > 4:
            continue
        if tag not in out or conf > out[tag].confidence:
            out[tag] = Tag(tag, role, str(item.get("evidence", ""))[:500], conf)
    return list(out.values())[:8]


def tag_company(extractor: "OllamaExtractor", company: str, business_text: str) -> list[Tag]:
    body = {
        "model": extractor.model, "stream": False, "format": TAG_SCHEMA,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": TAG_SYSTEM.format(company=company, vocab=", ".join(TAG_VOCAB))},
            {"role": "user", "content": business_text[:6000]},
        ],
    }
    r = requests.post(f"{extractor.url}/api/chat", json=body, timeout=extractor.timeout)
    r.raise_for_status()
    return parse_tags(r.json().get("message", {}).get("content", "{}"))


def _mentioned(name: str, text: str) -> bool:
    """Does the name's first distinctive word appear in the text as a whole word?
    ("The Boeing Company" checks "boeing", not "the"; "Intel" doesn't match "intelligent".)"""
    from .resolve import normalize
    words = [w for w in normalize(name).split() if len(w) >= 3] or normalize(name).split()
    if not words:
        return False
    return re.search(r"\b" + re.escape(words[0]) + r"\b", text, re.I) is not None
