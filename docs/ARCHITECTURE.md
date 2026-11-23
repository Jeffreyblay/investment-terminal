# Architecture

## Goals
1. **Free end to end:** no paid APIs, no keys, runs on a laptop.
2. **Every edge is checkable:** each relationship carries the sentence, filing, section and confidence it came from.
3. **Incremental and cached:** re-runs never re-download or re-extract finished work.
4. **Industry-agnostic:** a new industry is a YAML file, not new code.

## Two ways in, one database
- **Company mode** (`itx company`) is open-ended. Start anywhere, expand over links, and let industries emerge as tags.
- **Industry mode** (`itx seed/extract/export`) is a preset list of companies for a known industry.

Both write to the same tables, so a company processed in one mode is cached for the other.

### Company mode in detail
```
query ──find()──▶ CIK ──profile()──▶ SIC code
                   │
                   ├─ latest 10-K/20-F ─▶ sections ─┬─▶ tag_company()  ─▶ company_tags  (industries + role)
                   │                                └─▶ extract()       ─▶ relationships
                   │
                   └─ neighbors(min_conf) ─▶ next hop (listed CIK:* only, ≤ --max-per-level)
                                              repeat to --depth, skip companies with expanded_at set
export: ego_graph(center, depth) ─▶ web/data/company-<ticker>.json
        tag_graph(tag)           ─▶ web/data/tag-<tag>.json
```
- **`Edgar.find`**: exact ticker first, then fuzzy match (RapidFuzz `WRatio ≥ 85`) on SEC company names, so `itx company "intuitive surgical"` works.
- **SIC vs tags:** SIC is the SEC's official 4-digit code, but there's only one per company and it's coarse (NVIDIA is "semiconductors"). Tags come from the model reading the Business section. A company gets several, each with a role (`supplier | maker | customer | operator | platform`), evidence and confidence. Tags are normalised (lowercase, synonyms like "robots" → "robotics") and nudged toward a shared vocabulary (`TAG_VOCAB` in `extract.py`) so they merge across companies. The viewer shows both.
- **Caching:** `entities.expanded_at` is set when a company's own filing is processed. Later searches skip it unless you pass `--refresh`.
- **Cost control:** `--depth`, `--max-per-level` and `--max-passages` bound the work. Depth 1 is up to 1 + 15 companies by default.
- **Limits:** `ORG:` nodes (private, foreign-only, government) are leaves. EDINET support (roadmap) will make Japanese suppliers expandable.

## Components

### `edgar.py`: SEC client
- One `requests.Session` with the required `User-Agent`.
- **Rate limiting:** a minimum interval between requests (`SEC_MAX_RPS`, default 8; the SEC limit is 10). It backs off on 429/503.
- **Disk cache:** `data/cache/<sha1(url)>.(json|html)`. The ticker list refreshes weekly, submissions daily, and filing documents are kept forever, since filings never change.
- `filings(ticker, forms=("10-K","20-F"), limit)` reads `submissions/CIK##########.json` → `recent` arrays.
- `company_facts(cik)` + `headline_financials()` give the latest FY revenue, net income and R&D from the us-gaap XBRL facts. It tries several concept names, because companies tag revenue differently.
- `full_text_search(phrase)` uses `efts.sec.gov/LATEST/search-index` for discovery. Only the first page is used (about 100 hits).

### `sections.py`: cutting filings down (no LLM)
- `html_to_text` turns block elements into newlines, so paragraphs survive.
- `extract_section` finds every "Item 1. Business" heading and keeps the **longest** span to the next item heading. That skips the table of contents, where headings sit close together.
- 10-K: `business` (Item 1) and `risk_factors` (Item 1A). 20-F: Item 4 and risk factors. If headings aren't found, it falls back to the full text.
- `candidate_passages` keeps paragraphs that match a relationship **cue** regex AND contain ≥2 capitalised name-like tokens, or that match a cue plus an industry keyword. In practice that's 20–80 passages per 10-K.

### `extract.py`: local LLM
- Calls Ollama `/api/chat` with `format` set to a JSON schema and `temperature: 0`.
- The relation set is fixed: `supplies, customer_of, competes_with, partners_with, subsidiary_of, acquired, invested_in, contracted_by, manufactures_for`.
- The system prompt makes the filer the subject for "we/our", skips unnamed counterparties, and requires an evidence sentence.
- `parse_response` validates types, clamps confidence, drops unknown relations and self-loops, and drops results where neither name appears in the passage (a hallucination guard).

### `resolve.py`: entity resolution
Order: `config/aliases.yaml` → bare ticker → exact normalised name → RapidFuzz `token_sort_ratio ≥ 92` → private node.
- Normalisation lowercases, turns `&` into "and", strips punctuation, and removes legal suffixes (Inc, Corp, Ltd, plc, Holdings, Technologies…).
- IDs are `CIK:<int>` for SEC filers and `ORG:<normalised name>` for everyone else (private, foreign-only, government).
- `itx review` lists frequent unresolved names. Adding them to `aliases.yaml` is the main way quality improves over time.

### `store.py`: SQLite
Tables: `entities`, `industry_members`, `filings` (with status), `mentions` (raw names, for review), `relationships` (with provenance). See [SCHEMA.md](SCHEMA.md).

### `graph.py`: analytics and export
- Builds a `MultiDiGraph` and flips `customer_of` into `supplies`, so all supply edges point supplier → customer.
- Analytics: customers per supplier (dependence), betweenness centrality on the supply subgraph (chokepoints), and greedy-modularity communities (clusters).
- Export merges duplicate edges from different filings into one edge with a `citations` list.
- `load_neo4j` optionally `MERGE`s nodes and edges into Neo4j Community. Relationship types come from the fixed enum, so the Cypher can't be injected.

### `api.py` + `web/prototype/explorer.html`: the terminal
`itx serve` runs a local FastAPI app that serves the Explorer at `/` and three endpoints: `/api/health`, `/api/search?q=` (autocomplete) and `/api/company/{ticker or name}`. A company search calls the same `add_company` as the CLI: the first search fetches from EDGAR (profile, filings list, 5-year XBRL history) and saves it to SQLite, later searches come from the disk cache. Nothing is preloaded. The response is the export JSON format plus `company` and `filings`, which the Explorer converts (`fromApi`) into its network, relationships table and profile panel (financials with 5-year trends, annual report links). Opened as a plain file, the Explorer falls back to built-in sample data.

## Data flow and state
```
filings.status:  fetched ──extract──▶ extracted
                    ▲                    │
                    └──reset-extract─────┘        (failed on download errors)
```
`extract` only takes `fetched` filings, so it can be stopped and resumed at any time.

## Design decisions
| Decision | Why | Revisit when |
|---|---|---|
| SQLite over Postgres/Neo4j | Zero setup, single file, enough for 100k edges | Multi-user access or heavy graph queries → Neo4j CE / Postgres + AGE |
| Raw EDGAR endpoints over `edgartools` | Fewer dependencies, full control of caching | Want XBRL statements or 13F/insider data → `edgartools` (MIT) is a good drop-in |
| Regex section cutting before the LLM | Makes a local 7B model fast enough; cuts noise | Filings with unusual layouts → add an LLM-based fallback |
| Fixed relation enum | Comparable, queryable edges | A new relation type is needed → add it to `RELATIONS` and the viewer colours |
| Static viewer | Free hosting anywhere (GitHub Pages) | Need server-side search or auth → small FastAPI app |
