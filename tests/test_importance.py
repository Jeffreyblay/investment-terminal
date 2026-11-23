"""Importance from the full report: major customers (revenue share) and single-source suppliers."""
from terminal.extract import Relationship
from terminal.graph import build_graph, graph_payload
from terminal.importance import MAJOR, SINGLE, customer_importance, enrich_filing, sentences, supplier_importance
from terminal.store import Store

REPORT = """Item 1. Business. We automate warehouses. Walmart Inc. accounted for approximately 87%, 88% and 89% of
total revenue for fiscal 2025, 2024 and 2023, respectively. Customer A accounted for 22% of revenue.
We rely on Harmonic Drive Systems as the sole source of our gear reducers. We also buy chips from Intel Corporation
and other vendors. Target Corporation is one of our largest customers. Revenue grew 26% to $2.2 billion,
and Kroger is a new customer."""


def test_major_customer_with_revenue_share():
    assert customer_importance("Walmart Inc.", sentences(REPORT)) == (MAJOR, 87.0)


def test_largest_customer_wording_without_a_number():
    assert customer_importance("Target Corporation", sentences(REPORT)) == (MAJOR, None)


def test_unrelated_percentages_are_not_a_customer_share():
    s = sentences(REPORT)
    assert customer_importance("Kroger", s) == (None, None)            # "Revenue grew 26% ... Kroger"
    assert customer_importance("Costco Wholesale", s) == (None, None)  # not mentioned at all


def test_share_of_revenue_wording():
    s = sentences("Approximately 41% of our net sales were to Apple Inc. in fiscal 2025.")
    assert customer_importance("Apple Inc.", s) == (MAJOR, 41.0)
    s = sentences("Sales to Apple Inc. represented 9% of revenue.")       # below 10%: not major
    assert customer_importance("Apple Inc.", s) == (None, None)


def test_single_source_supplier():
    s = sentences(REPORT)
    assert supplier_importance("Harmonic Drive Systems", s) == SINGLE
    assert supplier_importance("Intel Corporation", s) is None


def _db(tmp_path):
    st = Store(tmp_path / "t.db")
    for i, n in [("CIK:1", "Symbotic Inc."), ("CIK:2", "WALMART INC."), ("ORG:harmonic drive systems", "Harmonic Drive Systems"),
                 ("CIK:3", "INTEL CORP")]:
        st.upsert_entity({"id": i, "name": n, "listed": i.startswith("CIK")})
    st.db.execute("INSERT INTO filings (accession, entity_id, form) VALUES ('a1', 'CIK:1', '10-K')")
    r = lambda s, rel, o: Relationship(s, rel, o, "evidence", 0.6)
    st.add_relationship("CIK:1", "supplies", "CIK:2", r("Symbotic", "supplies", "Walmart"), "a1", "m")
    st.add_relationship("ORG:harmonic drive systems", "supplies", "CIK:1", r("Harmonic", "supplies", "Symbotic"), "a1", "m")
    # customer_of is stored buyer -> seller: Symbotic buys from Intel.
    st.add_relationship("CIK:1", "customer_of", "CIK:3", r("Symbotic", "customer_of", "Intel"), "a1", "m")
    return st


def test_enrich_filing_sets_columns(tmp_path):
    st = _db(tmp_path)
    assert enrich_filing(st, "a1", "CIK:1", REPORT) == 2
    got = {(r["source_id"], r["target_id"]): (r["importance"], r["revenue_pct"])
           for r in st.db.execute("SELECT * FROM relationships")}
    assert got[("CIK:1", "CIK:2")] == (MAJOR, 87.0)
    assert got[("ORG:harmonic drive systems", "CIK:1")] == (SINGLE, None)
    assert got[("CIK:1", "CIK:3")] == (None, None)           # Intel is a plain supplier
    conf = {(r["source_id"], r["target_id"]): r["confidence"] for r in st.db.execute("SELECT * FROM relationships")}
    assert conf[("CIK:1", "CIK:2")] == 0.9                   # 87% of revenue confirms the link
    st.close()


def test_importance_reaches_the_viewer_json(tmp_path):
    st = _db(tmp_path)
    enrich_filing(st, "a1", "CIK:1", REPORT)
    nodes, rels = st.ego_graph("CIK:1", depth=1)
    st.close()
    edges = {(e["source"], e["target"]): e for e in graph_payload(build_graph(nodes, rels), {"name": "t"})["edges"]}
    assert edges[("CIK:1", "CIK:2")]["importance"] == MAJOR and edges[("CIK:1", "CIK:2")]["revenue_pct"] == 87.0
    assert edges[("ORG:harmonic drive systems", "CIK:1")]["importance"] == SINGLE
    assert edges[("CIK:3", "CIK:1")]["importance"] is None    # customer_of flipped to Intel supplies Symbotic


def test_real_symbotic_sentences():
    s = sentences(
        "Walmart, our largest customer, accounted for approximately 85% of our total revenue in the fiscal year "
        "ended September 27, 2025 and for a significant majority of our $22.5 billion backlog. "
        "As a result of this gross exercise, 446,741 Class A Common Units of Legacy Warehouse were issued to "
        "Walmart, which represented a 6.5 % ownership in the Company's outstanding and issuable Common Units.")
    assert customer_importance("Walmart Inc.", s) == (MAJOR, 85.0)


def test_ownership_stake_is_not_a_revenue_share():
    s = sentences("Units were issued to Walmart, which represented a 10.0 % ownership in the Company.")
    assert customer_importance("Walmart Inc.", s) == (None, None)


def test_largest_customer_must_be_said_of_that_company():
    gf = sentences("We recently joined our largest customer's American Manufacturing Program and are working with "
                   "both our customer and GlobalFoundries to develop new process technologies for our products.")
    assert customer_importance("GLOBALFOUNDRIES Inc.", gf) == (None, None)
    for text in ("Walmart, our largest customer, renewed its agreement.",
                 "Target Corporation is one of our largest customers.",
                 "Our largest customer, Walmart, renewed its agreement."):
        name = "Walmart Inc." if "Walmart" in text else "Target Corporation"
        assert customer_importance(name, sentences(text))[0] == MAJOR, text
