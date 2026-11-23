"""Offline tests: no network, no Ollama needed."""
import json

from terminal.extract import Relationship, parse_response
from terminal.graph import build_graph, export_json
from terminal.resolve import Resolver, normalize
from terminal.sections import Passage, candidate_passages, html_to_text, split_sections
from terminal.store import Store

HTML = """<html><body>
<p>Table of Contents</p><p>Item 1. Business 3</p><p>Item 1A. Risk Factors 10</p>
<p>Item 1. Business</p>
<p>We design collaborative robots. Our robot controllers use GPUs supplied by NVIDIA Corporation under a multi-year supply agreement, and our gear reducers are sourced from Harmonic Drive Systems.</p>
<p>Amazon.com, Inc. accounted for 14% of our revenue in fiscal 2025 and is our largest customer for autonomous mobile robots.</p>
<p>We have offices in several countries and our headquarters is in Massachusetts, which is not relevant to relationships at all.</p>
<p>Item 1A. Risk Factors</p>
<p>We rely on a single-source supplier in Asia for certain components; we compete with ABB Ltd and Fanuc Corporation in industrial automation.</p>
<p>Item 1B. Unresolved Staff Comments</p>
</body></html>"""

SEC = {
    "NVDA": {"cik": 1045810, "ticker": "NVDA", "name": "NVIDIA CORP"},
    "AMZN": {"cik": 1018724, "ticker": "AMZN", "name": "AMAZON COM INC"},
    "TER": {"cik": 97210, "ticker": "TER", "name": "TERADYNE, INC"},
    "ABBNY": {"cik": 1091587, "ticker": "ABBNY", "name": "ABB LTD"},
}


def test_sections_skip_table_of_contents():
    text = html_to_text(HTML)
    secs = split_sections(text, "10-K")
    assert "NVIDIA" in secs["business"]
    assert "single-source" in secs["risk_factors"]
    assert "Table of Contents" not in secs["business"]


def test_candidate_passages_filter():
    secs = split_sections(html_to_text(HTML), "10-K")
    ps = candidate_passages(secs, ["collaborative robot"])
    texts = " ".join(p.text for p in ps)
    assert "NVIDIA" in texts and "Amazon" in texts and "ABB" in texts
    assert "headquarters" not in texts


def test_normalize_and_resolve(tmp_path):
    assert normalize("NVIDIA Corporation") == "nvidia"
    r = Resolver(SEC, aliases_path=tmp_path / "none.yaml")
    assert r.resolve("NVIDIA Corporation")["ticker"] == "NVDA"
    assert r.resolve("Amazon.com, Inc.")["ticker"] == "AMZN"
    assert r.resolve("ABB Ltd")["ticker"] == "ABBNY"
    priv = r.resolve("Harmonic Drive Systems")
    assert not priv["listed"] and priv["id"].startswith("ORG:")


def test_aliases(tmp_path):
    p = tmp_path / "aliases.yaml"
    p.write_text("Universal Robots: TER\nSpaceX: Space Exploration Technologies\n")
    r = Resolver(SEC, aliases_path=p)
    assert r.resolve("Universal Robots")["ticker"] == "TER"
    assert r.resolve("SpaceX")["name"] == "Space Exploration Technologies"


def test_parse_response_guards():
    p = Passage("business", 1, "Our controllers use GPUs supplied by NVIDIA Corporation.")
    good = {"subject": "NVIDIA Corporation", "relation": "supplies", "object": "Teradyne",
            "evidence": "GPUs supplied by NVIDIA", "confidence": 0.9, "component": "GPUs"}
    bad_rel = dict(good, relation="likes")
    hallucinated = dict(good, subject="Intel", object="Qualcomm")
    rels = parse_response(json.dumps({"relationships": [good, bad_rel, hallucinated]}), p)
    assert len(rels) == 1 and rels[0].component == "GPUs"
    assert parse_response("not json", p) == []


def test_store_and_export(tmp_path):
    st = Store(tmp_path / "t.db")
    r = Resolver(SEC, aliases_path=tmp_path / "none.yaml")
    ter = r.resolve("TER")
    st.upsert_entity(ter)
    st.add_member("robotics", ter["id"], "Robot makers", True)
    rel = Relationship("NVIDIA Corporation", "supplies", "Teradyne", "GPUs supplied by NVIDIA", 0.9,
                       "GPUs", "business", 1)
    cust = Relationship("Teradyne", "customer_of", "Harmonic Drive Systems", "reducers sourced from HDS",
                        0.8, "gear reducers", "business", 1)
    for x in (rel, cust):
        s, t = r.resolve(x.subject), r.resolve(x.object)
        st.upsert_entity(s); st.upsert_entity(t)
        st.add_relationship(s["id"], x.relation, t["id"], x, "0000-00-000001", "test")
    nodes, rels = st.industry_graph("robotics")
    st.close()
    g = build_graph(nodes, rels)
    data = export_json(g, {"name": "Robotics", "segments": []}, tmp_path / "out.json")
    rels_out = {(e["source"], e["target"], e["relation"]) for e in data["edges"]}
    # customer_of is flipped so edges read supplier -> customer
    assert ("ORG:harmonic drive systems", ter["id"], "supplies") in rels_out
    assert ("CIK:1045810", ter["id"], "supplies") in rels_out
    assert data["analytics"]["customers_per_supplier"]
