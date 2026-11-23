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
        if not keep_relationship(rel.subject, rel.object, company, passage.text):
            continue
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
