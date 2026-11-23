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

Web:
  itx serve                local API + viewer at http://127.0.0.1:8000 (search companies live)

Maintenance:
  itx review               names the resolver could not match (add them to aliases.yaml)
  itx reset-extract <ind>  re-queue an industry's filings
  itx clean [--dry-run]    drop stored relationships that fail the quality filters (no LLM)
  itx enrich [--ticker X]  mark major customers / single-source suppliers from cached reports (no LLM)
  itx verify [--ticker X]  yes/no check by the model on stored relationships (--dry-run to preview)
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
    from .edgar import EdgarError, annual_series, headline_from_series
    ent = {"id": f"CIK:{info['cik']}", "name": info["name"], "ticker": info["ticker"],
           "cik": info["cik"], "listed": True}
    series = {}
    try:
        series = annual_series(ed.company_facts(info["cik"]))
    except EdgarError:
        pass  # foreign filers often have no us-gaap facts
    st.upsert_entity(ent, headline_from_series(series) or None)
    if series:
        st.set_financials_history(ent["id"], series)
    prof = ed.profile(info["cik"])
    st.set_profile(ent["id"], prof["sic"], prof["sic_desc"])
    if industry:
        st.add_member(industry, ent["id"], segment, seed)
    for f in ed.filings(info["ticker"], limit=years):
        st.add_filing(f, ent["id"])
    return ent["id"]


def process_filing(ed, st, llm, resolver, row, keywords=None, max_passages=40, tag=True,
                   progress=None, verify=True) -> int:
    """Download one filing, extract relationships (and industry tags), store them.
    `row` needs: url, accession, form, company, entity_id.
    `progress(step, done, total, found)`, if given, is called as work advances (used by the API's
    background jobs); relationships are committed after every passage so they can be shown live."""
    report = progress or (lambda *a: None)
    report("downloading", 0, 0, 0)
    from .edgar import EdgarError
    import dataclasses

    from .extract import VERDICT_CONFIDENCE, final_verdict, is_filer, review, tag_company
    from .sections import candidate_passages, html_to_text, split_sections
    try:
        html = ed.get(row["url"])
    except EdgarError as e:
        print(f"  ! {row['company']}: {e}")
        st.set_filing_status(row["accession"], "failed")
        return 0
    text = html_to_text(html)
    sections = split_sections(text, row["form"])
    if tag:
        report("tagging", 0, 0, 0)
        business = sections.get("business") or sections.get("f20_info") or next(iter(sections.values()))
        try:
            st.add_tags(row["entity_id"], tag_company(llm, row["company"], business), row["accession"])
        except Exception as e:
            print(f"    tagging failed: {e}")
    passages = candidate_passages(sections, keywords)[:max_passages]
    # Re-processing a filing (--refresh, "read again") replaces what it produced before, but only
    # at the end: old relationships stay visible meanwhile, and survive if this run fails.
    old_ids = {r[0] for r in st.db.execute("SELECT id FROM relationships WHERE accession=?",
                                           (row["accession"],))}
    kept: set[int] = set()
    filer = {"id": row["entity_id"], "method": "filer"}
    st.db.commit()
    n_rel = 0
    for i, p in enumerate(passages):
        report("reading", i, len(passages), n_rel)
        try:
            rels = llm.extract(row["company"], p)
        except Exception as e:  # network / timeout: keep going
            print(f"    passage {p.index}: {e}")
            continue
        for r in rels:
            # Same quality rules as parse_response, enforced here too so no extractor can bypass them.
            r = review(r, row["company"], p.text)
            if r is None:
                continue
            # Second opinion from the model on this one claim: "no" drops it, else sets confidence.
            if verify and hasattr(llm, "verify"):
                try:
                    verdict = final_verdict(llm.verify(row["company"], r, p.text), r, row["company"], p.text)
                except Exception as e:  # network / timeout: keep it, unverified
                    print(f"    verify failed: {e}")
                    verdict = "unclear"
                if verdict == "no":
                    continue
                r = dataclasses.replace(r, confidence=VERDICT_CONFIDENCE[verdict])
            # The filer's own name always maps to the filer; everything else goes through the resolver.
            src = filer if is_filer(r.subject, row["company"]) else resolver.resolve(r.subject)
            tgt = filer if is_filer(r.object, row["company"]) else resolver.resolve(r.object)
            if src["id"] == tgt["id"]:
                continue  # e.g. "Universal Robots" is an alias of the filer (Teradyne)
            for raw, res in ((r.subject, src), (r.object, tgt)):
                if res is not filer:
                    st.upsert_entity(res)
                    st.add_mention(raw, res)
            kept.add(st.add_relationship(src["id"], r.relation, tgt["id"], r, row["accession"], llm.model))
            n_rel += 1
        st.db.commit()   # each passage's results become visible right away
    # Finished: drop old relationships this run didn't find again.
    st.db.executemany("DELETE FROM relationships WHERE id=?", [(i,) for i in old_ids - kept])
    # Major customers / single-source suppliers, read from the whole report (no LLM).
    report("importance", len(passages), len(passages), n_rel)
    from .importance import enrich_filing
    enrich_filing(st, row["accession"], row["entity_id"], text)
    st.set_filing_status(row["accession"], "extracted")
    st.mark_expanded(row["entity_id"])
    st.db.commit()
    report("done", len(passages), len(passages), n_rel)
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


def cmd_clean(args) -> None:
    """Apply the current quality filters to relationships already in the database (no LLM)."""
    import sqlite3

    from .extract import Relationship, is_filer, review
    from .store import Store
    st = Store()
    rows = st.db.execute(
        """SELECT r.*, e.name AS filer, f.entity_id AS filer_id FROM relationships r
           JOIN filings f ON f.accession = r.accession JOIN entities e ON e.id = f.entity_id""").fetchall()
    dropped = relabelled = 0
    for r in rows:
        # The ids are the truth about direction (raw names keep whatever order the model used).
        filer_is_source = r["source_id"] == r["filer_id"]
        raws = [r["raw_source"] or "", r["raw_target"] or ""]
        other = raws[1] if is_filer(raws[0], r["filer"]) else raws[0]
        subj, obj = (r["filer"], other) if filer_is_source else (other, r["filer"])
        rel = Relationship(subj, r["relation"], obj, r["evidence"] or "", r["confidence"] or 0)
        new = None if r["source_id"] == r["target_id"] else review(rel, r["filer"])
        if new is None:
            dropped += 1
            print(f"  drop:    {subj} --{r['relation']}--> {obj}")
            if not args.dry_run:
                st.db.execute("DELETE FROM relationships WHERE id=?", (r["id"],))
            continue
        # review() may change the relation and/or flip the direction ("we purchase from X").
        flip = is_filer(new.subject, r["filer"]) != filer_is_source
        if new.relation != rel.relation or flip:
            relabelled += 1
            src, tgt = (r["target_id"], r["source_id"]) if flip else (r["source_id"], r["target_id"])
            print(f"  relabel: {subj} --{r['relation']}--> {obj}  =>  {new.subject} --{new.relation}--> {new.object}")
            if not args.dry_run:
                try:   # raw names follow the ids, so a later clean reads the corrected direction
                    st.db.execute("""UPDATE relationships SET relation=?, source_id=?, target_id=?,
                                     raw_source=?, raw_target=? WHERE id=?""",
                                  (new.relation, src, tgt, new.subject, new.object, r["id"]))
                except sqlite3.IntegrityError:   # the corrected row already exists
                    st.db.execute("DELETE FROM relationships WHERE id=?", (r["id"],))
    st.close()
    verb = "would be" if args.dry_run else "were"
    print(f"{len(rows)} relationships: {dropped} {verb} removed, {relabelled} {verb} relabelled.")


def cmd_verify(args) -> None:
    """Yes/no check by the model on relationships already stored: 'no' removes, else sets confidence.
    The paragraph around each quoted sentence is rebuilt from the cached report (it isn't stored),
    so competitor lists keep their introduction ("Competitors in our Robotics segment include ...")."""
    from .extract import VERDICT_CONFIDENCE, Relationship, final_verdict
    from .sections import html_to_text
    from .store import Store
    llm, st, ed = _llm(args.model), Store(), _edgar()
    texts: dict[str, list[str]] = {}

    def paragraph(accession: str, url: str, evidence: str) -> str:
        if accession not in texts:
            try:
                paras = re.split(r"\n\s*\n", html_to_text(ed.get(url)))
                texts[accession] = [" ".join(p.split()) for p in paras]
            except Exception:
                texts[accession] = []
        probe = " ".join(evidence.split())[:60].lower()
        return next((p for p in texts[accession] if probe and probe in p.lower()), "")

    q = """SELECT r.id, r.relation, r.evidence, r.accession, f.url, s.name AS s, t.name AS t, e.name AS filer, e.ticker
           FROM relationships r JOIN entities s ON s.id = r.source_id JOIN entities t ON t.id = r.target_id
           JOIN filings f ON f.accession = r.accession JOIN entities e ON e.id = f.entity_id"""
    params: tuple = ()
    if args.ticker:
        q += " WHERE e.ticker = ?"
        params = (args.ticker.upper(),)
    rows = st.db.execute(q + " ORDER BY e.ticker", params).fetchall()
    counts = {"yes": 0, "unclear": 0, "no": 0}
    for r in rows:
        rel = Relationship(r["s"], r["relation"], r["t"], r["evidence"] or "", 0)
        context = paragraph(r["accession"], r["url"], r["evidence"] or "")
        verdict = final_verdict(llm.verify(r["filer"], rel, context), rel, r["filer"], context)
        counts[verdict] += 1
        if verdict != "yes":
            print(f"  {verdict:7} {r['ticker'] or '':5} {r['s']} --{r['relation']}--> {r['t']}")
        if args.dry_run:
            continue
        if verdict == "no":
            st.db.execute("DELETE FROM relationships WHERE id=?", (r["id"],))
        else:
            st.db.execute("UPDATE relationships SET confidence=? WHERE id=?", (VERDICT_CONFIDENCE[verdict], r["id"]))
        st.db.commit()
    st.close()
    verb = "would be" if args.dry_run else "were"
    print(f"{len(rows)} checked: {counts['yes']} yes, {counts['unclear']} unclear, {counts['no']} no "
          f"({counts['no']} {verb} removed).")


def cmd_enrich(args) -> None:
    """Recompute importance (major customer / single source) for companies already read.
    Uses the cached annual reports: no LLM and, after the first download, no network."""
    from .importance import enrich_filing
    from .sections import html_to_text
    from .store import Store
    ed, st = _edgar(), Store()
    q = """SELECT f.accession, f.url, f.entity_id, e.ticker, e.name FROM filings f
           JOIN entities e ON e.id = f.entity_id WHERE f.status = 'extracted'"""
    params: tuple = ()
    if args.ticker:
        q += " AND e.ticker = ?"
        params = (args.ticker.upper(),)
    for f in st.db.execute(q, params).fetchall():
        n = enrich_filing(st, f["accession"], f["entity_id"], html_to_text(ed.get(f["url"])))
        st.db.commit()
        rows = st.db.execute(
            """SELECT r.importance, r.revenue_pct, s.name AS s, t.name AS t FROM relationships r
               JOIN entities s ON s.id = r.source_id JOIN entities t ON t.id = r.target_id
               WHERE r.accession = ? AND r.importance IS NOT NULL""", (f["accession"],)).fetchall()
        print(f"  {f['ticker'] or f['name']:6} {n} important link(s)")
        for r in rows:
            pct = f" ({r['revenue_pct']:g}% of revenue)" if r["revenue_pct"] else ""
            print(f"         {r['s']} -> {r['t']}: {r['importance']}{pct}")
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


def cmd_serve(args) -> None:
    try:
        import uvicorn
    except ImportError:
        sys.exit('The API needs extra packages: pip install -e ".[api]"')
    print(f"Investment Terminal on http://{args.host}:{args.port}  (API docs: /docs, Ctrl+C to stop)")
    uvicorn.run("terminal.api:app", host=args.host, port=args.port, reload=args.reload)


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
    sp = add("serve", cmd_serve, None)
    sp.add_argument("--host", default="127.0.0.1", help="127.0.0.1 = only this computer")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--reload", action="store_true", help="restart when code changes (dev)")

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
    sp = add("verify", cmd_verify, None)
    sp.add_argument("--ticker", help="only this company")
    sp.add_argument("--dry-run", action="store_true", help="show verdicts, change nothing")
    sp.add_argument("--model", default=None)
    sp = add("enrich", cmd_enrich, None)
    sp.add_argument("--ticker", help="only this company")
    sp = add("clean", cmd_clean, None)
    sp.add_argument("--dry-run", action="store_true", help="list what would be removed")
    for name, fn in (("export", cmd_export), ("neo4j", cmd_neo4j)):
        sp = add(name, fn)
        sp.add_argument("--min-conf", type=float, default=0.5)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
