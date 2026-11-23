"""XBRL financials: offline tests with a hand-made companyfacts payload."""
from terminal.edgar import annual_series, headline_financials
from terminal.graph import build_graph, export_json
from terminal.store import Store


def fact(start, end, val, form="10-K", filed="2025-02-01", accn="0000000000-25-000001"):
    d = {"end": end, "val": val, "form": form, "filed": filed, "accn": accn, "fp": "FY"}
    if start:
        d["start"] = start
    return d


FACTS = {"facts": {"us-gaap": {
    # Old tag, only used until FY2017: must NOT be reported as the latest revenue.
    "Revenues": {"units": {"USD": [fact("2017-01-01", "2017-12-31", 500, filed="2018-02-01")]}},
    # New tag (post-2018) with the recent years, plus a quarter that sits inside a 10-K.
    "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
        fact("2023-01-01", "2023-12-31", 900, filed="2024-02-01"),
        fact("2024-01-01", "2024-12-31", 1000),
        fact("2024-10-01", "2024-12-31", 300),          # Q4 only: must be ignored
    ]}},
    "Assets": {"units": {"USD": [
        fact(None, "2023-12-31", 4000, filed="2024-02-01"),
        fact(None, "2024-12-31", 5000),
    ]}},
    "EarningsPerShareDiluted": {"units": {"USD/shares": [fact("2024-01-01", "2024-12-31", 2.5)]}},
    "NetIncomeLoss": {"units": {"USD": [fact("2024-01-01", "2024-12-31", 120, form="10-Q")]}},
}}}


def test_latest_revenue_survives_concept_change():
    rev = annual_series(FACTS)["revenue"]
    assert rev[0]["value"] == 1000 and rev[0]["period_end"] == "2024-12-31"
    assert [r["period_end"] for r in rev] == ["2024-12-31", "2023-12-31", "2017-12-31"]


def test_quarters_are_dropped():
    values = [r["value"] for r in annual_series(FACTS)["revenue"]]
    assert 300 not in values


def test_balance_sheet_and_eps():
    s = annual_series(FACTS)
    assert [r["value"] for r in s["total_assets"]] == [5000, 4000]
    assert s["eps_diluted"][0]["value"] == 2.5


def test_quarterly_reports_are_ignored():
    assert "net_income" not in annual_series(FACTS)   # only came from a 10-Q


def test_years_limit_and_headline():
    assert len(annual_series(FACTS, years=2)["revenue"]) == 2
    head = headline_financials(FACTS)
    assert head["revenue"] == {"value": 1000, "period_end": "2024-12-31",
                               "concept": "RevenueFromContractWithCustomerExcludingAssessedTax"}


def test_restatement_uses_latest_filing():
    # FY2024 revenue first reported as 1000, then restated to 950 in the next year's 10-K.
    facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        fact("2024-01-01", "2024-12-31", 1000, filed="2025-02-01"),
        fact("2024-01-01", "2024-12-31", 950, filed="2026-02-01"),
    ]}}}}}
    rev = annual_series(facts)["revenue"]
    assert len(rev) == 1                      # one row per period, not one per filing
    assert rev[0]["value"] == 950
    assert rev[0]["filed"] == "2026-02-01"


def test_discontinued_tag_is_not_shown_as_current():
    # Like NVIDIA: the old capex tag stopped in 2012, the new one carries the recent years.
    facts = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [fact("2025-01-27", "2026-01-25", 215)]}},
        "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [
            fact("2011-01-31", "2012-01-29", 0.14)]}},
        "PaymentsToAcquireProductiveAssets": {"units": {"USD": [
            fact("2025-01-27", "2026-01-25", 6.0)]}},
        # A metric with no tag used since 2015 at all: dropped, not shown as "latest".
        "LongTermDebtNoncurrent": {"units": {"USD": [fact(None, "2015-01-25", 1.5)]}},
    }}}
    s = annual_series(facts)
    assert s["capex"][0] == {**s["capex"][0], "value": 6.0, "period_end": "2026-01-25"}
    assert "long_term_debt" not in s


# ---------- storage (step 2) ----------

def _store_with_company(tmp_path):
    st = Store(tmp_path / "t.db")
    st.upsert_entity({"id": "CIK:1", "name": "Test Co", "ticker": "TST", "cik": 1, "listed": True})
    return st


def test_history_is_stored_newest_first(tmp_path):
    st = _store_with_company(tmp_path)
    st.set_financials_history("CIK:1", annual_series(FACTS))
    h = st.financials_history(["CIK:1"])["CIK:1"]
    assert [r["value"] for r in h["revenue"]] == [1000, 900, 500]
    assert [r["value"] for r in h["total_assets"]] == [5000, 4000]
    st.close()


def test_saving_again_replaces_old_values(tmp_path):
    st = _store_with_company(tmp_path)
    st.set_financials_history("CIK:1", annual_series(FACTS))
    restated = {"revenue": [{"period_end": "2024-12-31", "value": 950}]}
    st.set_financials_history("CIK:1", restated)
    h = st.financials_history(["CIK:1"])["CIK:1"]
    assert h == {"revenue": [{"period_end": "2024-12-31", "value": 950}]}   # no stale rows left
    st.close()


def test_history_reaches_the_exported_json(tmp_path):
    st = _store_with_company(tmp_path)
    st.set_financials_history("CIK:1", annual_series(FACTS))
    nodes, rels = st.ego_graph("CIK:1", depth=1)
    st.close()
    data = export_json(build_graph(nodes, rels), {"name": "t"}, tmp_path / "out" / "t.json")
    node = data["nodes"][0]
    assert node["history"]["revenue"][0] == {"period_end": "2024-12-31", "value": 1000}
