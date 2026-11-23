"""Command line: `itx <command> ...`.

Company-level (open-ended):
  itx company NVDA --depth 1     process a company, tag its industries, expand to its partners
  itx tag robotics               export every company tagged "robotics" (from any search)
  itx tags                       list tags discovered so far

Industry presets (optional shortcut):
  itx seed robotics        resolve seed tickers, list their annual filings, pull XBRL financials
  itx discover robotics    find more companies via EDGAR full-text search on the keywords
  itx extract robotics     download filings, cut sections, run local LLM, resolve names, store
  itx export robotics      build graph + analytics, write web/data/robotics.json
  itx run robotics         seed + extract + export

Maintenance:
  itx review               names the resolver could not match (add them to aliases.yaml)
  itx reset-extract <ind>  re-queue an industry's filings
  itx neo4j robotics       (optional) mirror the graph into Neo4j Community
"""
from __future__ import annotations

import argparse
import re
import sys

from .config import ROOT, load_industry, settings

WEB_DATA = ROOT / "web" / "data"


def _edgar():
    from .edgar import Edgar
    return Edgar()


def _llm(model=None):
    from .extract import OllamaExtractor
    llm = OllamaExtractor(model=model)
    llm.check()
    return llm


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def add_company(ed, st, info: dict, years: int, segment=None, industry=None, seed=False) -> str:
    """Store an SEC filer with its SIC profile, XBRL financials and annual filings."""
    from .edgar import EdgarError, headline_financials
    ent = {"id": f"CIK:{info['cik']}", "name": info["name"], "ticker": info["ticker"],
           "cik": info["cik"], "listed": True}
    fin = None
    try:
        fin = headline_financials(ed.company_facts(info["cik"]))
    except EdgarError:
        pass  # foreign filers often have no us-gaap facts
    st.upsert_entity(ent, fin)
    prof = ed.profile(info["cik"])
    st.set_profile(ent["id"], prof["sic"], prof["sic_desc"])
    if industry:
        st.add_member(industry, ent["id"], segment, seed)
    for f in ed.filings(info["ticker"], limit=years):
        st.add_filing(f, ent["id"])
    return ent["id"]


def process_filing(ed, st, llm, resolver, row, keywords=None, max_passages=40, tag=True) -> int:
    """Download one filing, extract relationships (and industry tags), store them.
    `row` needs: url, accession, form, company, entity_id."""
    from .edgar import EdgarError
    from .extract import tag_company
    from .sections import candidate_passages, html_to_text, split_sections
    try:
        html = ed.get(row["url"])
    except EdgarError as e:
        print(f"  ! {row['company']}: {e}")
        st.set_filing_status(row["accession"], "failed")
        return 0
    sections = split_sections(html_to_text(html), row["form"])
    if tag:
        business = sections.get("business") or sections.get("f20_info") or next(iter(sections.values()))
        try:
            st.add_tags(row["entity_id"], tag_company(llm, row["company"], business), row["accession"])
        except Exception as e:
            print(f"    tagging failed: {e}")
    passages = candidate_passages(sections, keywords)[:max_passages]
    n_rel = 0
    for p in passages:
        try:
            rels = llm.extract(row["company"], p)
        except Exception as e:  # network / timeout: keep going
            print(f"    passage {p.index}: {e}")
            continue
        for r in rels:
            src, tgt = resolver.resolve(r.subject), resolver.resolve(r.object)
            for raw, res in ((r.subject, src), (r.object, tgt)):
                st.upsert_entity(res)
                st.add_mention(raw, res)
            st.add_relationship(src["id"], r.relation, tgt["id"], r, row["accession"], llm.model)
            n_rel += 1
    st.set_filing_status(row["accession"], "extracted")
    st.mark_expanded(row["entity_id"])
    st.db.commit()
    print(f"  {row['company'][:40]:40} {row['form']} {len(passages)} passages -> {n_rel} relationships")
    return n_rel


def _latest_filing(st, entity_id: str):
    return st.db.execute(
        """SELECT f.*, e.name AS company FROM filings f JOIN entities e ON e.id=f.entity_id
           WHERE f.entity_id=? ORDER BY f.filed DESC LIMIT 1""", (entity_id,)).fetchone()


# ---------------- company-level ----------------

def cmd_company(args) -> None:
    """Open-ended search: start from one company, expand breadth-first over its links."""
    from .edgar import EdgarError
    from .resolve import Resolver
    from .store import Store
    ed, st = _edgar(), Store()
    llm = _llm(args.model)
    resolver = Resolver(ed.tickers())
    try:
        info = ed.find(args.query)
    except EdgarError as e:
        sys.exit(str(e))
    by_cik = {c["cik"]: c for c in ed.tickers().values()}
    center = f"CIK:{info['cik']}"
    print(f"Center: {info['name']} ({info['ticker']})")

    frontier, done = [center], set()
    for level in range(args.depth + 1):
        nxt = []
        for eid in frontier[: args.max_per_level]:
            if eid in done or not eid.startswith("CIK:"):
                continue  # private / foreign-only companies can't be expanded
            done.add(eid)
            cinfo = by_cik.get(int(eid[4:]))
            if not cinfo:
                continue
            if args.refresh or not st.is_expanded(eid):
                add_company(ed, st, cinfo, years=1)
                row = _latest_filing(st, eid)
                if row:
                    process_filing(ed, st, llm, resolver, dict(row), max_passages=args.max_passages)
                else:
                    print(f"  {cinfo['ticker']}: no 10-K/20-F on file")
            else:
                print(f"  {cinfo['ticker']:6} cached")
            if level < args.depth:
                nxt.extend(n for n in st.neighbors(eid, args.min_conf) if n not in done)
        frontier = nxt
    st.db.commit()
    _export_company(st, center, info, args.depth, args.min_conf)
    st.close()


def _export_company(st, center, info, depth, min_conf):
    from .graph import build_graph, export_json
    nodes, rels = st.ego_graph(center, depth=max(depth, 1), min_conf=min_conf)
    g = build_graph(nodes, rels)
    name = f"company-{_slug(info['ticker'])}"
    data = export_json(g, {"name": f"{info['name']} ({info['ticker']})", "segments": []},
                       WEB_DATA / f"{name}.json", {"mode": "company", "center": center})
    c = next((n for n in data["nodes"] if n["id"] == center), {})
    tags = ", ".join(f"{t['tag']} ({t['role']})" for t in c.get("tags") or []) or "none yet"
    print(f"SIC: {c.get('sic')} {c.get('sic_desc') or ''}\nTags: {tags}")
    print(f"{len(data['nodes'])} nodes, {len(data['edges'])} edges -> web/data/{name}.json")


def cmd_tag(args) -> None:
    from .extract import normalize_tag
    from .graph import build_graph, export_json
    from .store import Store
    tag = normalize_tag(args.tag)
    st = Store()
    nodes, rels = st.tag_graph(tag, args.min_conf, args.min_tag_conf)
    st.close()
    if not nodes:
        sys.exit(f"No companies tagged '{tag}' yet. Search some companies first (itx company ...).")
    data = export_json(build_graph(nodes, rels), {"name": f"Tag: {tag}", "segments": []},
                       WEB_DATA / f"tag-{_slug(tag)}.json", {"mode": "tag", "tag": tag})
    print(f"{len(data['nodes'])} nodes, {len(data['edges'])} edges -> web/data/tag-{_slug(tag)}.json")


def cmd_tags(args) -> None:
    from .store import Store
    st = Store()
    for r in st.all_tags():
        print(f"  {r['n']:4}  {r['tag']}")
    st.close()


# ---------------- industry presets ----------------

def cmd_seed(args) -> None:
    from .edgar import EdgarError
    from .store import Store
    ind = load_industry(args.industry)
    ed, st = _edgar(), Store()
    for s in ind["seeds"]:
        try:
            c = ed.lookup(s["ticker"])
        except EdgarError as e:
            print(f"  skip {s['ticker']}: {e}")
            continue
        add_company(ed, st, c, args.years, s.get("segment"), ind["slug"], True)
        print(f"  {c['ticker']:6} {c['name'][:40]}")
    st.close()


def cmd_discover(args) -> None:
    from .store import Store
    ind = load_industry(args.industry)
    ed = _edgar()
    found: dict[int, dict] = {}
    for kw in ind.get("keywords", []):
        for h in ed.full_text_search(kw, forms="10-K,20-F"):
            if h["cik"]:
                f = found.setdefault(h["cik"], {"company": h["company"], "hits": set()})
                f["hits"].add(kw)
    ranked = sorted(found.items(), key=lambda x: -len(x[1]["hits"]))
    by_cik = {c["cik"]: c for c in ed.tickers().values()}
    st = Store() if args.add else None
    for cik, f in ranked[: args.top]:
        c = by_cik.get(cik)
        tick = c["ticker"] if c else "-"
        print(f"  {tick:6} {f['company'][:50]:50} {len(f['hits'])} keyword(s): {', '.join(sorted(f['hits']))[:60]}")
        if st and c and len(f["hits"]) >= args.min_hits:
            add_company(ed, st, c, args.years, None, ind["slug"], False)
    if st:
        st.close()
        print("Added companies with >= %d keyword hits. Run `itx extract %s` next." % (args.min_hits, ind["slug"]))


def cmd_extract(args) -> None:
    from .resolve import Resolver
    from .store import Store
    ind = load_industry(args.industry)
    ed, st = _edgar(), Store()
    llm = _llm(args.model)
    resolver = Resolver(ed.tickers())
    pending = st.pending_filings(ind["slug"])[: args.limit]
    print(f"{len(pending)} filing(s) to process with {llm.model}")
    for row in pending:
        process_filing(ed, st, llm, resolver, dict(row), ind.get("keywords"), args.max_passages)
    st.close()


def cmd_review(args) -> None:
    from .store import Store
    st = Store()
    rows = st.unresolved(args.limit)
    if not rows:
        print("Nothing unresolved.")
    for r in rows:
        print(f"  {r['n']:4}  {r['raw_name']}")
    print("\nAdd the ones that matter to config/aliases.yaml, then re-run extraction "
          "(`itx reset-extract <industry>` or `itx company <ticker> --refresh`).")
    st.close()


def cmd_reset(args) -> None:
    from .store import Store
    st = Store()
    st.db.execute("""UPDATE filings SET status='fetched' WHERE entity_id IN
                     (SELECT entity_id FROM industry_members WHERE industry=?)""", (args.industry,))
    st.db.execute("""DELETE FROM relationships WHERE accession IN (SELECT accession FROM filings
                     WHERE entity_id IN (SELECT entity_id FROM industry_members WHERE industry=?))""",
                  (args.industry,))
    st.close()
    print("Filings marked for re-extraction.")


def cmd_export(args):
    from .graph import build_graph, export_json
    from .store import Store
    ind = load_industry(args.industry)
    st = Store()
    nodes, rels = st.industry_graph(ind["slug"], args.min_conf)
    st.close()
    data = export_json(build_graph(nodes, rels), ind, WEB_DATA / f"{ind['slug']}.json")
    print(f"{len(data['nodes'])} nodes, {len(data['edges'])} edges -> web/data/{ind['slug']}.json")
    return data


def cmd_neo4j(args) -> None:
    from .graph import load_neo4j
    if not settings.neo4j_uri:
        sys.exit("Set NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD in .env first.")
    data = cmd_export(args)
    load_neo4j(data, settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    print("Loaded into Neo4j.")


def cmd_run(args) -> None:
    cmd_seed(args)
    cmd_extract(args)
    cmd_export(args)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="itx", description="Investment Terminal pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, pos="industry"):
        sp = sub.add_parser(name)
        if pos:
            sp.add_argument(pos)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("company", cmd_company, "query")
    sp.add_argument("--depth", type=int, default=1, help="hops to expand (0 = this company only)")
    sp.add_argument("--max-per-level", type=int, default=15, help="cap companies expanded per hop")
    sp.add_argument("--max-passages", type=int, default=30)
    sp.add_argument("--min-conf", type=float, default=0.5)
    sp.add_argument("--model", default=None)
    sp.add_argument("--refresh", action="store_true", help="re-process even if cached")
    sp = add("tag", cmd_tag, "tag")
    sp.add_argument("--min-conf", type=float, default=0.5)
    sp.add_argument("--min-tag-conf", type=float, default=0.5)
    add("tags", cmd_tags, None)

    for name, fn in (("seed", cmd_seed), ("run", cmd_run)):
        sp = add(name, fn)
        sp.add_argument("--years", type=int, default=2, help="annual reports per company")
        if name == "run":
            sp.add_argument("--limit", type=int, default=50)
            sp.add_argument("--max-passages", type=int, default=40)
            sp.add_argument("--model", default=None)
            sp.add_argument("--min-conf", type=float, default=0.5)
    sp = add("discover", cmd_discover)
    sp.add_argument("--top", type=int, default=40)
    sp.add_argument("--add", action="store_true", help="add matches to the industry")
    sp.add_argument("--min-hits", type=int, default=2)
    sp.add_argument("--years", type=int, default=2)
    sp = add("extract", cmd_extract)
    sp.add_argument("--limit", type=int, default=50, help="max filings this run")
    sp.add_argument("--max-passages", type=int, default=40, help="cap per filing (speed)")
    sp.add_argument("--model", default=None, help="override OLLAMA_MODEL")
    sp = add("review", cmd_review, None)
    sp.add_argument("--limit", type=int, default=50)
    add("reset-extract", cmd_reset)
    for name, fn in (("export", cmd_export), ("neo4j", cmd_neo4j)):
        sp = add(name, fn)
        sp.add_argument("--min-conf", type=float, default=0.5)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
