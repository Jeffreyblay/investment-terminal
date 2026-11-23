"""Extraction quality filters, using real model output from Teradyne's 10-K."""
import json

import pytest

from terminal.edgar import Edgar
from terminal.extract import _mentioned, is_filer, is_named_org, keep_relationship, parse_response
from terminal.resolve import Resolver
from terminal.sections import Passage

FILER = "TERADYNE, INC"

# Real competitors the model found: all must be kept.
REAL = ["Advantest Corporation", "SPEA S.p.A.", "Cohu, Inc.", "KUKA Robotics Corporation", "ABB", "FANUC",
        "Yaskawa Electric Corporation", "Rohde & Schwarz GmbH & Co. KG", "National Instruments Corporation",
        "iTest", "Test Research, Inc.", "U.S. Department of Defense", "NASA", "Bank of America"]

# Junk the model returned as counterparties: all must be dropped.
JUNK = ["five largest direct customers", "additional direct customer", "business in China", "products",
        "customer of our Semiconductor Test segment", "companies in the high performance computing and data center",
        "OEM, IDM, or Fabless company", "IDMs", "OSATs", "Fabless customers", "cyber controls vendors",
        "third-party services", "suppliers in foreign locations", "defense and aerospace applications",
        "Five largest customers", "Certain suppliers", "contract manufacturing organizations and distributors"]


@pytest.mark.parametrize("name", REAL)
def test_real_names_are_kept(name):
    assert is_named_org(name)
    assert keep_relationship(FILER, name, FILER)


@pytest.mark.parametrize("name", JUNK)
def test_generic_counterparties_are_dropped(name):
    assert not keep_relationship(FILER, name, FILER)


def test_one_side_must_be_the_filer():
    assert keep_relationship("Teradyne", "Advantest Corporation", FILER)       # short form of the filer
    assert keep_relationship("NVIDIA Corporation", "Teradyne, Inc.", FILER)    # filer as object
    assert not keep_relationship("J750", "Microchip Technology", FILER)        # product as subject
    assert not keep_relationship("Teradyne", "TERADYNE, INC", FILER)           # self-loop


def test_mentioned_uses_whole_distinctive_words():
    assert _mentioned("The Boeing Company", "We sell to Boeing.")
    assert not _mentioned("The Boeing Company", "The market is large.")      # not fooled by "the"
    assert not _mentioned("Intel Corporation", "Our intelligent robots")       # whole words only


def test_parse_response_applies_filters_with_filer():
    p = Passage("risk_factors", 3, "Competitors include Advantest Corporation. Our five largest customers "
                                   "accounted for 44% of revenue.")
    items = [
        {"subject": FILER, "relation": "competes_with", "object": "Advantest Corporation", "evidence": "x", "confidence": 1},
        {"subject": FILER, "relation": "customer_of", "object": "five largest customers", "evidence": "x", "confidence": 1},
        {"subject": FILER, "relation": "competes_with", "object": "Keysight", "evidence": "x", "confidence": 1},  # not in text
    ]
    rels = parse_response(json.dumps({"relationships": items}), p, FILER)
    assert [r.object for r in rels] == ["Advantest Corporation"]


def test_is_filer_variants():
    for n in ("Teradyne", "TERADYNE, INC", "Teradyne Inc.", "we", "the Company"):
        assert is_filer(n, FILER)
    assert not is_filer("Advantest", FILER)


def test_all_caps_word_is_not_treated_as_a_ticker(tmp_path):
    sec = {"VIP": {"cik": 1, "ticker": "VIP", "name": "Vulcan Infrastructure & Power Inc."}}
    r = Resolver(sec, aliases_path=tmp_path / "none.yaml")
    assert r.resolve("VIP")["id"] == "ORG:vip"      # internal term, not the unrelated company


def test_primary_ticker_is_used_for_every_share_class(tmp_path, monkeypatch):
    raw = {"0": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
           "1": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet Inc."},
           "2": {"cik_str": 1360901, "ticker": "RKLB", "title": "Rocket Lab Corp"}}
    ed = Edgar(user_agent="Test test@example.com", cache_dir=tmp_path)
    monkeypatch.setattr(ed, "get_json", lambda url, **kw: raw)
    t = ed.tickers()
    assert t["GOOG"]["ticker"] == "GOOGL" and t["GOOGL"]["ticker"] == "GOOGL"
    assert len({c["cik"] for c in ed.search("goo")}) == 1      # one result per company
