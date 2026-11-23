"""XBRL financials: offline tests with a hand-made companyfacts payload."""
from terminal.edgar import annual_series, headline_financials


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
