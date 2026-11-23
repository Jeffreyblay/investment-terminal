"""Extraction quality filters, using real model output from Teradyne's 10-K."""
import json

import pytest

from terminal.edgar import Edgar
from terminal.extract import Relationship, _mentioned, is_filer, is_named_org, keep_relationship, parse_response, review
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
        {"subject": FILER, "relation": "competes_with", "object": "Advantest Corporation", "evidence": "Competitors include Advantest Corporation.", "confidence": 1},
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


# ---------- relation must match the evidence (real Rocket Lab output) ----------
RKLB = "Rocket Lab Corp"
ACQ = ("We entered this market with our acquisition of leading spacecraft components manufacturer Sinclair "
       "Interplanetary and have since expanded our market participation with the acquisitions of Advanced Solutions, "
       "Incorporated, Planetary Systems Corporation, SolAero Technologies Corp. and GEOST.")


def rel(subject, relation, obj, evidence):
    return Relationship(subject, relation, obj, evidence, 0.9)


@pytest.mark.parametrize("target", ["Sinclair Interplanetary", "Advanced Solutions, Incorporated",
                                    "Planetary Systems Corporation", "SolAero Technologies Corp", "GEOST"])
def test_acquisitions_are_relabelled(target):
    out = review(rel(RKLB, "supplies", target, ACQ), RKLB)
    assert out and out.relation == "acquired" and out.subject == RKLB and out.object == target


def test_acquired_by_points_the_other_way():
    out = review(rel("Teradyne", "partners_with", "Big Holdings Inc.", "In 2025 we were acquired by Big Holdings Inc."), "Teradyne")
    assert out.relation == "acquired" and out.subject == "Big Holdings Inc." and out.object == "Teradyne"


def test_product_and_place_sentences_are_dropped():
    photon = ("Photon can be configured to operate as the upper stage of Electron (the kick stage) during launch, "
              "then with a simple command, transition into an operational spacecraft on orbit.")
    wallops = "On January 24, 2023, we successfully launched our first Electron from NASA Wallops Flight Facility."
    assert review(rel(RKLB, "supplies", "Photon", photon), RKLB) is None
    assert review(rel(RKLB, "supplies", "NASA Wallops Flight Facility", wallops), RKLB) is None


def test_real_relations_with_cues_are_kept():
    assert review(rel("NVIDIA Corporation", "supplies", "Teradyne", "Our controllers use GPUs supplied by NVIDIA."), "Teradyne")
    assert review(rel(RKLB, "supplies", "NASA", "We launched the CAPSTONE mission for NASA under a contract."), RKLB)
    assert review(rel("Teradyne", "competes_with", "Advantest", "Competitors include Advantest."), "Teradyne")
    assert review(rel("Symbotic Inc.", "supplies", "Walmart Inc.", "Walmart accounted for 87% of our revenue."), "Symbotic Inc.")


def test_competitor_list_with_cue_in_the_paragraph():
    ev = ("Companies that have introduced products in the field of robotic-assisted medical procedures include: "
          "CMR Surgical Ltd.; Distalmotion SA; Medicaroid Corporation.")
    para = "We face competition in robotic surgery. " + ev
    r = rel("INTUITIVE SURGICAL INC", "competes_with", "CMR Surgical Ltd.", ev)
    assert review(r, "INTUITIVE SURGICAL INC", para)                 # cue in the paragraph
    assert review(r, "INTUITIVE SURGICAL INC")                       # itx clean: no paragraph, kept
    assert review(r, "INTUITIVE SURGICAL INC", ev) is None           # no cue anywhere: dropped
