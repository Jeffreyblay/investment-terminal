"""SQLite storage (built into Python, zero setup). Neo4j is an optional mirror."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .config import settings

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS entities (
  id        TEXT PRIMARY KEY,      -- CIK:320193 or ORG:<normalised name>
  name      TEXT NOT NULL,
  ticker    TEXT,
  cik       INTEGER,
  listed    INTEGER NOT NULL DEFAULT 0,
  financials TEXT                  -- JSON from XBRL headline_financials
);
CREATE TABLE IF NOT EXISTS industry_members (
  industry  TEXT NOT NULL,
  entity_id TEXT NOT NULL REFERENCES entities(id),
  segment   TEXT,
  is_seed   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (industry, entity_id)
);
CREATE TABLE IF NOT EXISTS filings (
  accession TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL REFERENCES entities(id),
  form      TEXT, filed TEXT, period TEXT, url TEXT,
  status    TEXT NOT NULL DEFAULT 'fetched'   -- fetched | extracted | failed
);
CREATE TABLE IF NOT EXISTS mentions (          -- raw names before resolution
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  raw_name TEXT NOT NULL, entity_id TEXT, method TEXT
);
CREATE TABLE IF NOT EXISTS relationships (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id  TEXT NOT NULL REFERENCES entities(id),
  relation   TEXT NOT NULL,
  target_id  TEXT NOT NULL REFERENCES entities(id),
  component  TEXT,
  evidence   TEXT,
  confidence REAL,
  accession  TEXT REFERENCES filings(accession),
  section    TEXT,
  model      TEXT,
  raw_source TEXT, raw_target TEXT,
  UNIQUE (source_id, relation, target_id, accession, evidence)
);
CREATE INDEX IF NOT EXISTS rel_src ON relationships(source_id);
CREATE INDEX IF NOT EXISTS rel_tgt ON relationships(target_id);
CREATE TABLE IF NOT EXISTS company_tags (      -- open-ended industries, built up per company
  entity_id  TEXT NOT NULL REFERENCES entities(id),
  tag        TEXT NOT NULL,                    -- "robotics", "semiconductors", ...
  role       TEXT,                             -- supplier | maker | customer | operator | platform
  confidence REAL,
  evidence   TEXT,
  accession  TEXT,
  PRIMARY KEY (entity_id, tag)
);
CREATE INDEX IF NOT EXISTS tag_idx ON company_tags(tag);
"""

# Columns added after v0.1; applied to existing databases on open.
MIGRATIONS = [
    ("entities", "sic", "TEXT"),
    ("entities", "sic_desc", "TEXT"),
    ("entities", "expanded_at", "TEXT"),   # when this company's own filing was processed
]


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or settings.db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA_SQL)
        for table, col, typ in MIGRATIONS:
            cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    # ---------- company-level ----------
    def set_profile(self, entity_id: str, sic: str | None, sic_desc: str | None) -> None:
        self.db.execute("UPDATE entities SET sic=?, sic_desc=? WHERE id=?", (sic, sic_desc, entity_id))

    def add_tags(self, entity_id: str, tags, accession: str | None) -> None:
        for t in tags:
            self.db.execute(
                """INSERT INTO company_tags (entity_id, tag, role, confidence, evidence, accession)
                   VALUES (?,?,?,?,?,?) ON CONFLICT(entity_id, tag) DO UPDATE SET
                   role=excluded.role, confidence=MAX(confidence, excluded.confidence),
                   evidence=excluded.evidence, accession=excluded.accession""",
                (entity_id, t.tag, t.role, t.confidence, t.evidence, accession))

    def mark_expanded(self, entity_id: str) -> None:
        self.db.execute("UPDATE entities SET expanded_at=datetime('now') WHERE id=?", (entity_id,))

    def is_expanded(self, entity_id: str) -> bool:
        r = self.db.execute("SELECT expanded_at FROM entities WHERE id=?", (entity_id,)).fetchone()
        return bool(r and r["expanded_at"])

    def neighbors(self, entity_id: str, min_conf: float = 0.5) -> list[str]:
        rows = self.db.execute(
            """SELECT target_id AS n FROM relationships WHERE source_id=? AND confidence>=?
               UNION SELECT source_id FROM relationships WHERE target_id=? AND confidence>=?""",
            (entity_id, min_conf, entity_id, min_conf)).fetchall()
        return [r["n"] for r in rows]

    def tags_for(self, ids) -> dict[str, list[dict]]:
        ids = list(ids)
        out: dict[str, list[dict]] = {i: [] for i in ids}
        if not ids:
            return out
        for r in self.db.execute(
                f"SELECT * FROM company_tags WHERE entity_id IN ({','.join('?' * len(ids))}) "
                "ORDER BY confidence DESC", ids):
            out[r["entity_id"]].append({"tag": r["tag"], "role": r["role"],
                                        "confidence": r["confidence"]})
        return out

    def _rels_among(self, ids: set[str], min_conf: float) -> list[dict]:
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        return [dict(r) for r in self.db.execute(
            f"""SELECT r.*, f.url AS filing_url, f.form, f.filed FROM relationships r
                LEFT JOIN filings f ON f.accession = r.accession
                WHERE r.confidence >= ? AND r.source_id IN ({q}) AND r.target_id IN ({q})""",
            (min_conf, *ids, *ids))]

    def _nodes(self, ids: set[str], members: dict | None = None) -> list[dict]:
        if not ids:
            return []
        members = members or {}
        tags = self.tags_for(ids)
        nodes = []
        for row in self.db.execute(f"SELECT * FROM entities WHERE id IN ({','.join('?' * len(ids))})",
                                   tuple(ids)):
            d = dict(row)
            m = members.get(d["id"], {})
            d["segment"] = m.get("segment")
            d["is_seed"] = bool(m.get("is_seed"))
            d["in_industry"] = d["id"] in members
            d["financials"] = json.loads(d["financials"]) if d["financials"] else None
            d["tags"] = tags.get(d["id"], [])
            d["expanded"] = bool(d.get("expanded_at"))
            nodes.append(d)
        return nodes

    def ego_graph(self, center_id: str, depth: int = 1, min_conf: float = 0.5):
        """A company plus everything within `depth` hops, and all links among them."""
        seen, frontier = {center_id}, {center_id}
        for _ in range(depth):
            nxt = set()
            for n in frontier:
                nxt.update(self.neighbors(n, min_conf))
            frontier = nxt - seen
            seen |= frontier
        return self._nodes(seen), self._rels_among(seen, min_conf)

    def tag_graph(self, tag: str, min_conf: float = 0.5, min_tag_conf: float = 0.5):
        """Every company tagged with `tag`, plus their direct partners."""
        core = {r["entity_id"] for r in self.db.execute(
            "SELECT entity_id FROM company_tags WHERE tag=? AND confidence>=?", (tag, min_tag_conf))}
        ids = set(core)
        for n in core:
            ids.update(self.neighbors(n, min_conf))
        members = {i: {"segment": None, "is_seed": False} for i in core}
        return self._nodes(ids, members), self._rels_among(ids, min_conf)

    def all_tags(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT tag, COUNT(*) n FROM company_tags GROUP BY tag ORDER BY n DESC").fetchall()

    def close(self):
        self.db.commit()
        self.db.close()

    def upsert_entity(self, e: dict, financials: dict | None = None) -> None:
        self.db.execute(
            """INSERT INTO entities (id, name, ticker, cik, listed, financials)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=COALESCE(excluded.name, name), ticker=COALESCE(excluded.ticker, ticker),
                 cik=COALESCE(excluded.cik, cik), listed=MAX(listed, excluded.listed),
                 financials=COALESCE(excluded.financials, financials)""",
            (e["id"], e["name"], e.get("ticker"), e.get("cik"), int(bool(e.get("listed"))),
             json.dumps(financials) if financials else None),
        )

    def add_member(self, industry: str, entity_id: str, segment: str | None, is_seed: bool):
        self.db.execute(
            """INSERT INTO industry_members (industry, entity_id, segment, is_seed) VALUES (?,?,?,?)
               ON CONFLICT(industry, entity_id) DO UPDATE SET segment=COALESCE(excluded.segment, segment),
                 is_seed=MAX(is_seed, excluded.is_seed)""",
            (industry, entity_id, segment, int(is_seed)),
        )

    def add_filing(self, f, entity_id: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO filings (accession, entity_id, form, filed, period, url) VALUES (?,?,?,?,?,?)",
            (f.accession, entity_id, f.form, f.filed, f.period, f.url),
        )

    def set_filing_status(self, accession: str, status: str) -> None:
        self.db.execute("UPDATE filings SET status=? WHERE accession=?", (status, accession))

    def pending_filings(self, industry: str) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT f.*, e.name AS company FROM filings f
               JOIN entities e ON e.id = f.entity_id
               JOIN industry_members m ON m.entity_id = e.id AND m.industry = ?
               WHERE f.status = 'fetched' ORDER BY f.filed DESC""", (industry,)).fetchall()

    def add_mention(self, raw: str, resolved: dict) -> None:
        self.db.execute("INSERT INTO mentions (raw_name, entity_id, method) VALUES (?,?,?)",
                        (raw, resolved["id"], resolved["method"]))

    def add_relationship(self, src: str, rel: str, tgt: str, r, accession: str, model: str) -> None:
        self.db.execute(
            """INSERT OR IGNORE INTO relationships
               (source_id, relation, target_id, component, evidence, confidence, accession,
                section, model, raw_source, raw_target) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (src, rel, tgt, r.component, r.evidence, r.confidence, accession, r.section, model,
             r.subject, r.object),
        )

    def unresolved(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT raw_name, COUNT(*) n FROM mentions WHERE method='unresolved'
               GROUP BY raw_name ORDER BY n DESC LIMIT ?""", (limit,)).fetchall()

    def industry_graph(self, industry: str, min_conf: float = 0.5) -> tuple[list[dict], list[dict]]:
        """Nodes = industry members + anything they link to. Edges = relationships touching members."""
        members = {r["entity_id"]: dict(r) for r in self.db.execute(
            "SELECT * FROM industry_members WHERE industry=?", (industry,))}
        rels = [dict(r) for r in self.db.execute(
            """SELECT r.*, f.url AS filing_url, f.form, f.filed FROM relationships r
               LEFT JOIN filings f ON f.accession = r.accession
               WHERE r.confidence >= ? AND (r.source_id IN (SELECT entity_id FROM industry_members WHERE industry=?)
                  OR r.target_id IN (SELECT entity_id FROM industry_members WHERE industry=?))""",
            (min_conf, industry, industry))]
        ids = set(members) | {r["source_id"] for r in rels} | {r["target_id"] for r in rels}
        return self._nodes(ids, members), rels
