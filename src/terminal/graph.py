"""Graph analytics (NetworkX) and export to JSON for the web viewer, plus an
optional loader into Neo4j Community Edition."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import networkx as nx

# Normalise direction so edges read supplier -> customer.
FLIP = {"customer_of": "supplies"}


def build_graph(nodes: list[dict], rels: list[dict]) -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()
    for n in nodes:
        g.add_node(n["id"], **n)
    for r in rels:
        src, tgt, rel = r["source_id"], r["target_id"], r["relation"]
        if rel in FLIP:
            src, tgt, rel = tgt, src, FLIP[rel]
        g.add_edge(src, tgt, relation=rel, **{k: r.get(k) for k in
                   ("component", "evidence", "confidence", "accession", "filing_url", "form",
                    "filed", "section")})
    return g


def analytics(g: nx.MultiDiGraph) -> dict:
    """Who matters structurally: most-depended-on suppliers, chokepoints, clusters."""
    supply = nx.DiGraph((u, v) for u, v, d in g.edges(data=True) if d["relation"] == "supplies")
    out = {"customers_per_supplier": {}, "betweenness": {}, "communities": []}
    if supply.number_of_nodes():
        out["customers_per_supplier"] = dict(sorted(
            ((n, supply.out_degree(n)) for n in supply), key=lambda x: -x[1])[:15])
        bc = nx.betweenness_centrality(supply)
        out["betweenness"] = dict(sorted(bc.items(), key=lambda x: -x[1])[:15])
    und = nx.Graph(g.to_undirected())
    if und.number_of_edges():
        comms = nx.algorithms.community.greedy_modularity_communities(und)
        out["communities"] = [sorted(c) for c in comms]
    return out


def export_json(g: nx.MultiDiGraph, industry: dict, path: Path, extra: dict | None = None) -> dict:
    """Write the web viewer's data file (nodes, edges with citations, analytics).
    `industry` needs a "name" (and optional "segments"); `extra` adds keys such as
    {"mode": "company", "center": "CIK:1045810"}."""
    data = graph_payload(g, industry, extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, default=str))
    update_index(path.parent, path.stem, data)
    return data


def graph_payload(g: nx.MultiDiGraph, industry: dict, extra: dict | None = None) -> dict:
    """The viewer's data format as a dict (used by export_json and by the API)."""
    edges = []
    for u, v, d in g.edges(data=True):
        edges.append({"source": u, "target": v, **d})
    # Collapse duplicate evidence into one edge with a citation list.
    merged: dict[tuple, dict] = {}
    for e in edges:
        key = (e["source"], e["target"], e["relation"])
        m = merged.setdefault(key, {"source": e["source"], "target": e["target"],
                                    "relation": e["relation"], "components": set(),
                                    "confidence": 0.0, "citations": []})
        if e.get("component"):
            m["components"].add(e["component"])
        m["confidence"] = max(m["confidence"], e.get("confidence") or 0)
        m["citations"].append({k: e.get(k) for k in ("evidence", "form", "filed", "filing_url",
                                                     "section", "confidence")})
    for m in merged.values():
        m["components"] = sorted(m["components"])

    nodes = []
    for nid, d in g.nodes(data=True):
        nodes.append({k: d.get(k) for k in ("id", "name", "ticker", "cik", "listed", "segment",
                                            "is_seed", "in_industry", "financials", "sic",
                                            "sic_desc", "tags", "expanded", "history")})
    data = {
        "mode": "industry",
        "industry": industry["name"],
        "segments": industry.get("segments", []),
        "nodes": nodes,
        "edges": list(merged.values()),
        "analytics": analytics(g),
        **(extra or {}),
    }
    return data


def update_index(folder: Path, name: str, data: dict) -> None:
    """Keep <folder>/index.json listing every exported dataset, for the viewer's picker."""
    idx_path = folder / "index.json"
    try:
        idx = json.loads(idx_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        idx = {"datasets": []}
    idx["datasets"] = [d for d in idx["datasets"] if d["file"] != name]
    idx["datasets"].append({"file": name, "label": data.get("industry", name),
                            "mode": data.get("mode", "industry"), "nodes": len(data["nodes"])})
    idx_path.write_text(json.dumps(idx, indent=1))


def load_neo4j(data: dict, uri: str, user: str, password: str) -> None:
    """Mirror an exported graph into Neo4j Community (free). Requires `pip install .[neo4j]`."""
    from neo4j import GraphDatabase  # optional dependency

    by_rel = defaultdict(list)
    for e in data["edges"]:
        by_rel[e["relation"].upper()].append({
            "s": e["source"], "t": e["target"], "conf": e["confidence"],
            "components": e["components"],
            "evidence": [c["evidence"] for c in e["citations"]],
            "urls": [c["filing_url"] for c in e["citations"]],
        })
    with GraphDatabase.driver(uri, auth=(user, password)) as drv, drv.session() as s:
        s.run("CREATE CONSTRAINT company_id IF NOT EXISTS FOR (c:Company) REQUIRE c.id IS UNIQUE")
        s.run("""UNWIND $rows AS r MERGE (c:Company {id: r.id})
                 SET c.name = r.name, c.ticker = r.ticker, c.listed = r.listed,
                     c.segment = r.segment""",
              rows=[{k: n[k] for k in ("id", "name", "ticker", "listed", "segment")}
                    for n in data["nodes"]])
        for rel, rows in by_rel.items():
            # rel comes from our fixed RELATIONS enum, so it's safe to interpolate.
            s.run(f"""UNWIND $rows AS r
                      MATCH (a:Company {{id: r.s}}), (b:Company {{id: r.t}})
                      MERGE (a)-[x:{rel}]->(b)
                      SET x.confidence = r.conf, x.components = r.components,
                          x.evidence = r.evidence, x.filing_urls = r.urls""", rows=rows)
