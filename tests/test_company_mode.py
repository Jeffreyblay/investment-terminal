"""Company-level search: tagging, ego graphs, tag graphs, DB migration. Offline."""
import json
import sqlite3

from terminal.extract import Relationship, Tag, normalize_tag, parse_tags
from terminal.graph import build_graph, export_json
from terminal.store import Store


def test_parse_tags_normalises_and_dedupes():
    raw = {"tags": [
        {"tag": "Robots", "role": "supplier", "evidence": "x", "confidence": 0.6},
        {"tag": "robotics", "role": "supplier", "evidence": "y", "confidence": 0.9},
        {"tag": "Semiconductor", "role": "maker", "evidence": "z", "confidence": 0.95},
        {"tag": "a very long tag that is really a sentence", "role": "maker", "evidence": "", "confidence": 1},
        {"role": "maker"},
    ]}
    tags = {t.tag: t for t in parse_tags(json.dumps(raw))}
    assert set(tags) == {"robotics", "semiconductors"}
    assert tags["robotics"].confidence == 0.9
    assert normalize_tag("AI") == "ai compute"
    assert parse_tags("nope") == []


def _seed(st):
    for eid, name in [("CIK:1", "NVIDIA"), ("CIK:2", "Teradyne"), ("CIK:3", "Amazon"),
                      ("CIK:4", "Toyota"), ("ORG:harmonic drive", "Harmonic Drive")]:
        st.upsert_entity({"id": eid, "name": name, "listed": eid.startswith("CIK")})
    links = [("CIK:1", "CIK:2"), ("ORG:harmonic drive", "CIK:2"), ("CIK:2", "CIK:3"), ("CIK:3", "CIK:4")]
    for a, b in links:
        r = Relationship(a, "supplies", b, f"{a} supplies {b}", 0.9)
        st.add_relationship(a, "supplies", b, r, "acc", "test")
    st.add_tags("CIK:1", [Tag("robotics", "supplier", "e", 0.8), Tag("semiconductors", "maker", "e", 0.95)], "acc")
    st.add_tags("CIK:2", [Tag("robotics", "maker", "e", 0.9)], "acc")
    st.add_tags("CIK:3", [Tag("retail", "operator", "e", 0.9)], "acc")


def test_ego_graph_depth(tmp_path):
    st = Store(tmp_path / "t.db")
    _seed(st)
    n1, _ = st.ego_graph("CIK:2", depth=1)
    assert {n["id"] for n in n1} == {"CIK:1", "CIK:2", "CIK:3", "ORG:harmonic drive"}
    n2, r2 = st.ego_graph("CIK:2", depth=2)
    assert "CIK:4" in {n["id"] for n in n2} and len(r2) == 4
    nvda = next(n for n in n1 if n["id"] == "CIK:1")
    assert {t["tag"] for t in nvda["tags"]} == {"robotics", "semiconductors"}
    st.close()


def test_tag_graph_and_export(tmp_path):
    st = Store(tmp_path / "t.db")
    _seed(st)
    nodes, rels = st.tag_graph("robotics")
    ids = {n["id"] for n in nodes}
    assert {"CIK:1", "CIK:2"} <= ids              # tagged
    assert "CIK:3" in ids                         # direct partner of a tagged company
    core = {n["id"] for n in nodes if n["in_industry"]}
    assert core == {"CIK:1", "CIK:2"}
    st.close()
    data = export_json(build_graph(nodes, rels), {"name": "Tag: robotics"}, tmp_path / "tag-robotics.json",
                       {"mode": "tag", "tag": "robotics"})
    assert data["mode"] == "tag"
    idx = json.loads((tmp_path / "index.json").read_text())
    assert idx["datasets"][0]["file"] == "tag-robotics"


def test_migrates_v01_database(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE entities (id TEXT PRIMARY KEY, name TEXT NOT NULL, ticker TEXT, cik INTEGER,"
               " listed INTEGER NOT NULL DEFAULT 0, financials TEXT)")
    db.execute("INSERT INTO entities (id, name) VALUES ('CIK:9', 'Old Co')")
    db.commit(); db.close()
    st = Store(path)
    st.set_profile("CIK:9", "3674", "Semiconductors & Related Devices")
    st.mark_expanded("CIK:9")
    assert st.is_expanded("CIK:9")
    st.close()
