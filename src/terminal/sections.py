"""Turn a 10-K / 20-F HTML document into clean text, cut out the useful sections,
and pick only the paragraphs likely to mention relationships.

This step is free (no LLM) and shrinks each filing from ~100k words to a few
thousand, which is what makes local-LLM extraction practical.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

# Modern 10-Ks are inline-XBRL XHTML; the HTML parser reads them fine, so skip bs4's warning.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# 10-K item headings. 20-F uses "Item 4" (company info) and "Item 3.D" (risk factors).
SECTION_PATTERNS = {
    "business": (r"item\s*1\.?\s*business", r"item\s*1a\.?\s*risk\s*factors"),
    "risk_factors": (r"item\s*1a\.?\s*risk\s*factors", r"item\s*1b\.?|item\s*1c\.?|item\s*2\.?\s*properties"),
    "f20_info": (r"item\s*4\.?\s*information\s+on\s+the\s+company", r"item\s*4a\.?|item\s*5\.?"),
    "f20_risk": (r"risk\s+factors", r"item\s*4\.?\s*information"),
}

# Cue words that suggest a sentence names a relationship.
CUES = re.compile(
    r"\b(supplier|suppliers|supply agreement|sole[- ]source|single[- ]source|"
    r"customer|customers|% of (?:our )?(?:total |net )?(?:revenue|sales)|percent of (?:our )?revenue|"
    r"competitor|competitors|compete with|competition from|"
    r"partner|partnership|joint venture|collaborat|license agreement|"
    r"manufactur(?:e|er|ers|ed) (?:by|for)|contract manufactur|foundry|distributor|"
    r"acquired|acquisition of|subsidiary|invest(?:ed|ment) in|contract(?:ed)? with|awarded)\b",
    re.I,
)
# A capitalised name-like token (rough filter: paragraph must name *someone*).
NAMEISH = re.compile(r"\b[A-Z][A-Za-z0-9&.\-]{2,}(?:\s+[A-Z][A-Za-z0-9&.\-]+)*\b")


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "ix:header"]):
        tag.decompose()
    # Keep block structure as newlines so paragraphs survive.
    for br in soup.find_all(["br"]):
        br.replace_with("\n")
    for blk in soup.find_all(["p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"]):
        blk.insert_after("\n")
    text = soup.get_text(" ")
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def extract_section(text: str, start_pat: str, end_pat: str) -> str:
    """Return the longest span between a start heading and the next end heading.
    Picking the longest skips the table of contents, where headings sit close together."""
    best = ""
    starts = [m.start() for m in re.finditer(start_pat, text, re.I)]
    for s in starts:
        m_end = re.compile(end_pat, re.I).search(text, s + 50)
        e = m_end.start() if m_end else min(len(text), s + 200_000)
        if e - s > len(best):
            best = text[s:e]
    return best.strip()


def split_sections(text: str, form: str = "10-K") -> dict[str, str]:
    keys = ["business", "risk_factors"] if form.startswith("10-K") else ["f20_info", "f20_risk"]
    out = {}
    for k in keys:
        s = extract_section(text, *SECTION_PATTERNS[k])
        if s:
            out[k] = s
    if not out:  # fallback: whole document
        out["full_text"] = text
    return out


@dataclass
class Passage:
    section: str
    index: int
    text: str


def candidate_passages(sections: dict[str, str], keywords: list[str] | None = None,
                       max_chars: int = 1800) -> list[Passage]:
    """Paragraphs that contain a relationship cue AND a capitalised name,
    or an industry keyword. Long paragraphs are cut to max_chars."""
    kw = re.compile("|".join(re.escape(k) for k in keywords), re.I) if keywords else None
    out: list[Passage] = []
    for name, body in sections.items():
        paras = [p.strip() for p in re.split(r"\n\s*\n", body) if len(p.strip()) > 80]
        for i, p in enumerate(paras):
            has_cue = bool(CUES.search(p))
            has_kw = bool(kw and kw.search(p))
            names = NAMEISH.findall(p)
            if (has_cue and len(names) >= 2) or (has_kw and has_cue):
                out.append(Passage(name, i, p[:max_chars]))
    return out
