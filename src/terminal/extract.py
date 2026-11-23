"""Relationship extraction with a free local LLM served by Ollama.

Ollama's /api/chat accepts a JSON schema in `format`, so the model is forced
to return valid, typed JSON. Every relationship keeps the exact evidence
sentence so it can be checked against the filing later.
"""
from __future__ import annotations

import json
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

SYSTEM = """You extract business relationships from SEC filing text.
Rules:
- The filer is "{company}". Use "{company}" as subject or object when the text says "we", "our" or "the Company".
- Only extract relationships that NAME a specific organisation. Skip vague ones ("a supplier in Asia", "certain customers").
- relation must be one of: {relations}.
- component: the product or part involved if stated (e.g. "GPUs", "lidar sensors"), else "".
- evidence: copy the exact sentence from the text that supports it.
- confidence: 0.0-1.0, how clearly the text states this relationship.
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
        return parse_response(content, passage)


def parse_response(content: str, passage: Passage) -> list[Relationship]:
    """Validate model JSON; drop anything malformed or unsupported by the passage."""
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
        # Hallucination guard: the object name must appear in the passage.
        if not _mentioned(rel.object, passage.text) and not _mentioned(rel.subject, passage.text):
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
    first = name.split()[0].strip(",.").lower()
    return len(first) > 2 and first in text.lower()
