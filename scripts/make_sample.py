"""Write web/data/sample-robotics.json so the viewer works before the pipeline runs.
The relationships here are ILLUSTRATIVE, not extracted from filings."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from terminal.graph import analytics, build_graph  # noqa: E402

SEGS = ["Components & chips", "Robot makers", "Customers"]
NODES = [  # id, name, ticker (None = not an SEC filer), segment
    ("NVDA", "NVIDIA", "NVDA", 0), ("HDS", "Harmonic Drive Systems", None, 0),
    ("NAB", "Nabtesco", None, 0), ("CGNX", "Cognex", "CGNX", 0), ("OUST", "Ouster", "OUST", 0),
    ("FANUC", "Fanuc", None, 1), ("YASK", "Yaskawa Electric", None, 1), ("ABB", "ABB", None, 1),
    ("TER", "Teradyne", "TER", 1), ("ISRG", "Intuitive Surgical", "ISRG", 1), ("SYM", "Symbotic", "SYM", 1),
    ("TM", "Toyota Motor", "TM", 2), ("WMT", "Walmart", "WMT", 2), ("AMZN", "Amazon", "AMZN", 2),
]
SUPPLY = [("NVDA", "TER"), ("NVDA", "ISRG"), ("NVDA", "SYM"), ("HDS", "FANUC"), ("HDS", "YASK"),
          ("HDS", "TER"), ("NAB", "FANUC"), ("NAB", "YASK"), ("NAB", "ABB"), ("CGNX", "FANUC"),
          ("CGNX", "ABB"), ("OUST", "SYM"), ("FANUC", "TM"), ("YASK", "TM"), ("ABB", "TM"),
          ("TER", "AMZN"), ("SYM", "WMT"), ("SYM", "AMZN")]
COMPETE = [("FANUC", "YASK"), ("FANUC", "ABB"), ("YASK", "ABB"), ("HDS", "NAB"), ("TER", "ABB")]

TAGS = {  # illustrative cross-industry tags, as the tagging step would produce
    "NVDA": [("ai compute", "platform"), ("semiconductors", "maker"), ("robotics", "supplier"),
             ("automotive", "supplier"), ("data centers", "supplier")],
    "TER": [("robotics", "maker"), ("semiconductors", "supplier")],
    "ISRG": [("robotics", "maker"), ("medical devices", "maker")],
    "SYM": [("robotics", "maker"), ("logistics", "supplier")],
    "OUST": [("sensors", "maker"), ("robotics", "supplier"), ("automotive", "supplier")],
    "CGNX": [("sensors", "maker"), ("industrial automation", "supplier")],
    "TM": [("automotive", "maker"), ("robotics", "customer")],
    "AMZN": [("cloud software", "operator"), ("retail", "operator"), ("robotics", "customer")],
    "WMT": [("retail", "operator"), ("robotics", "customer")],
}
nodes = [{"id": i, "name": n, "ticker": t, "cik": None, "listed": t is not None, "segment": SEGS[s],
          "is_seed": False, "in_industry": True, "financials": None, "sic": None, "sic_desc": None,
          "expanded": i in TAGS,
          "tags": [{"tag": tg, "role": r, "confidence": 0.8} for tg, r in TAGS.get(i, [])]}
         for i, n, t, s in NODES]
rels = [{"source_id": a, "target_id": b, "relation": "supplies", "confidence": 0.8,
         "evidence": "(illustrative sample)", "component": ""} for a, b in SUPPLY]
rels += [{"source_id": a, "target_id": b, "relation": "competes_with", "confidence": 0.7,
          "evidence": "(illustrative sample)", "component": ""} for a, b in COMPETE]

g = build_graph(nodes, rels)
edges = [{"source": r["source_id"], "target": r["target_id"], "relation": r["relation"],
          "components": [], "confidence": r["confidence"],
          "citations": [{"evidence": r["evidence"], "form": None, "filed": None, "filing_url": None,
                         "section": None, "confidence": r["confidence"]}]} for r in rels]
out = {"industry": "Robotics (sample)", "sample": True, "segments": SEGS, "nodes": nodes,
       "edges": edges, "analytics": analytics(g)}
path = ROOT / "web" / "data" / "sample-robotics.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(out, indent=1))
print("wrote", path)
