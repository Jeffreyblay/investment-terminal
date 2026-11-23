# Roadmap

Planning board (Whiteboard): https://claude.ai/artifact/8rmKaSXTQujxNPTdbuAyAe
Clickable design prototype of the terminal: https://claude.ai/artifact/Fpw12P98t7xP9XiC2iy91H

## Milestone 1: first real robotics graph (you are here)
- [x] EDGAR client with cache + rate limit
- [x] Section cutting and passage filtering
- [x] Ollama extraction with JSON schema and guards
- [x] Entity resolution with aliases + fuzzy matching
- [x] SQLite store, NetworkX analytics, JSON export
- [x] Static Cytoscape.js viewer with citations
- [ ] Run on the 9 robotics seeds (2 years each), review unresolved names, grow `aliases.yaml`
- [ ] Hand-label 50 relationships to measure precision

## Milestone 1b: company-level search (done in v0.2)
- [x] `itx company <ticker|name> --depth N`: open-ended breadth-first expansion
- [x] SIC code and description saved per company
- [x] Industry tagging prompt → `company_tags` (tag, role, evidence, confidence)
- [x] `itx tags`, `itx tag <tag>` exports; ego-graph exports; `web/data/index.json`
- [x] Viewer: centres on the searched company, tag chips, picker built from the index
- [ ] Try it: `itx company TER --depth 1`, `itx company RKLB --depth 1`, then `itx tag robotics`
- [ ] Review tag quality; extend `TAG_VOCAB` / `SYNONYMS` in `extract.py` where tags split

## Milestone 2: quality
- [ ] `itx eval`: precision/recall against a labelled CSV, per model
- [ ] Second-pass verification prompt (yes/no on each extracted edge)
- [ ] Few-shot examples in the extraction prompt
- [ ] Stoplist for product names; filer-name post-fix for "the Company"
- [ ] Exhibit 21 parser for clean subsidiary edges

## Milestone 3: coverage
- [ ] EDINET client for Japanese suppliers (Fanuc, Yaskawa, Harmonic Drive, Nabtesco, Keyence)
- [ ] 10-Q / 8-K monitoring for new contracts and acquisitions
- [ ] USAspending contracts (space / defense)
- [ ] GLEIF + Wikidata for parent/subsidiary and IDs
- [ ] Space industry run; then a third industry (e.g. semiconductors, batteries)

## Milestone 3b: intelligence features (designed in the Explorer prototype)
Prototype: https://claude.ai/artifact/Fpw12P98t7xP9XiC2iy91H#play-Explorer

**Time**
- [ ] Store `first_seen` / `last_seen` per relationship (min/max filing year across citations)
- [ ] Viewer: "as of" year slider; hide links first seen after the year; dashed + faded when `last_seen < year` (stale)
- [ ] Extract from 3–5 years of 10-Ks so changes show up (`--years 5`)

**Importance**
- [ ] Extraction schema: add `importance` (`major` = named ≥10% of revenue, `single_source`, `mentioned`) and optional `revenue_pct`
- [ ] Line thickness/colour by importance; flags on nodes ("single source", "major customer")
- [ ] Chokepoints table: suppliers ranked by single-source dependants, then customers (uses `graph.analytics`)

**Geography**
- [ ] Entity HQ country (SEC submissions `addresses.business`, EDINET for Japan)
- [ ] Extraction schema: add `country` (where the component is made/sourced) to each relationship
- [ ] Viewer: "exposure to <country>" filter; "by country" layout; later a real map view (MapLibre GL, free)

**Components**
- [ ] Normalise `component` strings into a `components` table (synonyms: "graphics processors" → "GPUs")
- [ ] Search resolves components as well as companies; component view = makers ← component → buyers
- [ ] Profile panel: "components it supplies / buys" as clickable chips

## Milestone 4: terminal features (match the design prototype)
- [ ] Segment-column layout option alongside force layout
- [ ] Companies table view with sort/filter
- [ ] Industry search box driving `discover`
- [ ] Component nodes ("harmonic reducer", "lidar") and "who makes this part" view
- [ ] Board interlocks from proxy statements (DEF 14A)
- [ ] Optional prices panel (yfinance, personal use)
- [ ] Time slider: graph as of each filing year

## Milestone 5: packaging
- [ ] FastAPI backend: type a company in the browser → runs `company` in the background, streams the graph as it fills (still free, self-hosted)
- [ ] "Expand" button on unprocessed nodes in the viewer (calls the backend)
- [ ] GitHub Pages deploy of the viewer
- [ ] Docker compose: app + optional Neo4j Community

## Ideas to explore later
- Exposure scoring: how many of an industry's makers depend on one supplier or one country
- Alerts when a filing adds or drops a named supplier
- Embedding search over filing passages (sentence-transformers, free) for "find companies that mention X"
