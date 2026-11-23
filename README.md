# Investment Terminal

A free, open-source research hub that maps an industry: its publicly listed companies, their supply chains, competitors and partners. It is built from public SEC filings and shown as an interactive knowledge graph.

**Search any company** (`itx company NVDA`) and get its industries plus everything it's connected to, one or more hops out. You can also start from an industry preset ("robotics", "space"). Either way you get:
- the industries the company actually participates in, as tags with a role (a chip maker is tagged `robotics · supplier`), plus its official SEC industry (SIC) code
- the listed companies around it, grouped into segments (components → makers → customers)
- who supplies whom, who competes with whom, partnerships, subsidiaries, government contracts
- headline financials (revenue, net income, R&D) from XBRL
- the exact sentence and filing behind every relationship, so it can be checked

**Everything here is free:** free data (SEC EDGAR), free software (MIT/BSD/GPL), and a local LLM (Ollama). No paid APIs and no API keys.

---

## Quick start

```bash
# 1. Python 3.10+
git clone <your-repo> investment-terminal && cd investment-terminal
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 2. Local LLM (free): install Ollama from https://ollama.com, then
ollama pull qwen2.5:7b          # ~4.7 GB; see docs/EXTRACTION.md for smaller/larger options

# 3. Config: the SEC requires a User-Agent with your name + email
cp .env.example .env            # then edit SEC_USER_AGENT

# 4a. Start from any company (open-ended)
itx company TER --depth 0       # just Teradyne: SIC code, industry tags, relationships
itx company TER --depth 1       # + process every listed company it links to
itx tags                        # industries discovered so far, across all searches
itx tag robotics                # every company tagged "robotics" + their partners

# 4b. ...or start from an industry preset
itx seed robotics               # resolve seed tickers, list 10-K/20-F filings, pull XBRL financials
itx extract robotics --limit 5  # try 5 filings first; ~1-5 min each on a laptop
itx export robotics             # writes web/data/robotics.json

itx review                      # names the resolver couldn't match -> add to config/aliases.yaml

# 5. Open the terminal (needs: pip install -e ".[api]")
itx serve                        # open http://127.0.0.1:8000 and search any US-listed company
```

The terminal is the Explorer (`web/prototype/explorer.html`). Served by `itx serve`, it searches SEC EDGAR live: nothing needs to be preloaded, and each company is cached after its first search. Opened directly as a file, it shows built-in **illustrative sample data**, not extracted from filings.

Run the tests (offline, no Ollama needed): `pytest`

---

## How it works

```
 SEC EDGAR (free)                       local, free
 ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
 │ tickers, 10-K│──▶│ sections.py  │──▶│ extract.py   │──▶│ resolve.py   │──▶│ store.py     │
 │ 20-F, XBRL,  │   │ HTML→text,   │   │ Ollama LLM → │   │ names → CIK /│   │ SQLite       │
 │ full-text    │   │ Item 1 / 1A, │   │ typed JSON + │   │ ORG ids,     │   │ (+ optional  │
 │ search       │   │ cue filter   │   │ evidence     │   │ aliases.yaml │   │  Neo4j CE)   │
 └──────────────┘   └──────────────┘   └──────────────┘   └──────────────┘   └──────┬───────┘
                                                                                     ▼
                                       Explorer via `itx serve` (api.py) ◀── graph.py (NetworkX
                                       graph, company panel, citations      analytics + JSON export)
```

**Company mode** (`itx company <ticker or name>`): find the company → save its SIC code → process its latest annual report (steps 4–6 below, plus an **industry-tagging** prompt) → follow its links breadth-first to `--depth` hops. Companies that are already processed come from the cache. Private and foreign-only companies appear as nodes but can't be expanded. Every search adds to one shared database, so industry tags build up across searches: after searching NVDA, TER and ISRG, `itx tag robotics` shows all of them together.

**Industry mode** (presets):

1. **Seed:** each industry is a YAML file (`config/industries/robotics.yaml`) with seed tickers, segments and keywords.
2. **Discover** (optional): `itx discover robotics --add` runs EDGAR full-text search on the keywords to find more companies.
3. **Fetch:** the latest annual reports (10-K, or 20-F for foreign filers) come from EDGAR and are cached on disk. Rate limit: 8 req/s, under the SEC's limit of 10.
4. **Cut:** the code pulls Item 1 (Business) and Item 1A (Risk Factors), then keeps only paragraphs with relationship cues ("supplier", "% of revenue", "compete with", …) plus a named organisation. This takes a 100k-word filing down to a few thousand words, with no LLM involved.
5. **Extract:** a local LLM reads each passage and returns relationships in a fixed JSON schema. Each one has an evidence sentence and a confidence score. Results whose names don't appear in the passage are dropped.
6. **Resolve:** names map to SEC CIKs (manual aliases first, then exact match, then fuzzy match). Unmatched names become private or foreign nodes (`ORG:...`).
7. **Store and export:** everything goes into SQLite. `export` builds a NetworkX graph, computes analytics (most-relied-on suppliers, chokepoints, clusters) and writes JSON for the viewer.

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

## Commands

| Command | What it does |
|---|---|
| `itx company <ticker\|name> [--depth 1 --max-per-level 15 --refresh]` | Open-ended search from one company → `web/data/company-<ticker>.json` |
| `itx tags` | List industry tags discovered so far, with company counts |
| `itx tag <tag>` | Every company with that tag + direct partners → `web/data/tag-<tag>.json` |
| `itx seed <industry> [--years 2]` | Resolve seed tickers, store companies, list annual filings, pull XBRL financials |
| `itx discover <industry> [--add --min-hits 2 --top 40]` | EDGAR full-text search on the industry keywords; `--add` adds matches |
| `itx extract <industry> [--limit 50 --max-passages 40 --model qwen2.5:7b]` | Download, cut, extract with Ollama, resolve, store |
| `itx review` | List unresolved organisation names by frequency |
| `itx reset-extract <industry>` | Clear relationships and re-queue filings (after editing aliases or switching models) |
| `itx export <industry> [--min-conf 0.5]` | Graph + analytics → `web/data/<industry>.json` |
| `itx neo4j <industry>` | Optional: mirror the graph into Neo4j Community Edition |
| `itx run <industry>` | `seed` + `extract` + `export` |

Every export updates `web/data/index.json`, so new company, tag and industry graphs show up in the viewer's picker automatically. In the viewer, clicking an industry tag on a company opens that tag's graph (if you've exported it) or highlights the matching companies in the current one.

Industry presets are optional. Add one by copying `config/industries/space.yaml`, changing the seeds and keywords, and running `itx run <name>`.

**Depth and speed:** depth 0 processes 1 company (a few minutes with a local model). Depth 1 processes it plus up to `--max-per-level` partners, so it can take an hour or more on CPU. Depth 2 can reach dozens of companies, so run it overnight. Re-runs are instant for cached companies.

---

## Project layout

```
investment-terminal/
├── README.md                 ← you are here
├── pyproject.toml            dependencies (all free/open source)
├── .env.example              settings: SEC user agent, Ollama model, paths, Neo4j
├── config/
│   ├── aliases.yaml          manual name → ticker mappings (grow this over time)
│   └── industries/           robotics.yaml, space.yaml (seeds, segments, keywords)
├── src/terminal/
│   ├── config.py             settings + industry loader
│   ├── edgar.py              SEC EDGAR client (cache, rate limit, XBRL, full-text search)
│   ├── sections.py           HTML → text, section cutting, passage filtering
│   ├── extract.py            Ollama extraction with JSON schema + hallucination guard
│   ├── resolve.py            entity resolution (aliases, exact, fuzzy)
│   ├── store.py              SQLite schema and queries
│   ├── graph.py              NetworkX analytics, JSON export, Neo4j loader
│   └── cli.py                `itx` command
├── web/
│   ├── prototype/explorer.html  the terminal UI: live search via `itx serve`, sample data if opened as a file
│   └── data/                 exported graphs (+ sample-robotics.json)
├── design/                   prototype notes + canvas source files (see design/README.md)
├── scripts/make_sample.py    regenerates the sample dataset
├── tests/                    offline tests (pipeline, company mode, CLI with fakes)
└── docs/
    ├── ARCHITECTURE.md       components, data flow, design decisions
    ├── DATA_SOURCES.md       every free source, endpoints, limits, terms
    ├── SCHEMA.md             database tables, graph model, JSON export format
    ├── EXTRACTION.md         prompting, models, accuracy, tuning
    ├── LICENSING.md          what's free, what to watch when publishing
    └── ROADMAP.md            next steps, ordered
```

---

## Free stack

| Layer | Tool | License / cost |
|---|---|---|
| Filings, financials, search | SEC EDGAR APIs | Free, public domain (User-Agent required) |
| Parsing | BeautifulSoup + lxml | MIT / BSD |
| LLM | Ollama + Qwen 2.5 / Llama 3.x | Free, runs locally (check each model's license) |
| Name matching | RapidFuzz | MIT |
| Storage | SQLite (default) | Public domain |
| Graph DB (optional) | Neo4j Community / Memgraph / Apache AGE | GPLv3 / BSL / Apache-2.0 |
| Analytics | NetworkX | BSD |
| Viewer | Cytoscape.js | MIT |
| Prices (optional) | yfinance | Free, unofficial, personal use only |

See [docs/LICENSING.md](docs/LICENSING.md) before publishing or making this commercial.

---

## Designed next (in the prototype, not yet in the code)

The **Explorer** prototype is the target interface. Open **`web/prototype/explorer.html`** in a browser (online copy: https://claude.ai/artifact/Fpw12P98t7xP9XiC2iy91H#play-Explorer). It shows a full-screen map with floating panels and a relationships table docked at the bottom. It adds four features the pipeline will need to support:

| Feature | What you see | What the pipeline needs |
|---|---|---|
| **Time** | "As of" year slider; links fade and turn dashed when they drop out of recent filings | `first_seen` / `last_seen` per relationship; extract 3–5 years of 10-Ks |
| **Importance** | Thick lines for major customers (≥10% of revenue), red for single-source suppliers; a Chokepoints table | `importance` (+ optional `revenue_pct`) in the extraction schema |
| **Geography** | "Exposure to Taiwan/Japan/…" filter; by-country layout (real map later) | Company HQ country; `country` per relationship (where it's made or sourced) |
| **Components** | Search "lidar" → makers ← component → buyers; component chips on each company | A normalised `components` table with synonyms |

Task lists for each are in [docs/ROADMAP.md](docs/ROADMAP.md) (Milestone 3b). The older prototype screens (industry graph, company search) and the three style directions are on the same canvas.

## Status and known limits

- **Version 0.2:** company-level search and open-ended industry tags added. The pipeline and viewer work end to end. Extraction quality depends on the local model; check the citations.
- **US/SEC filers only for now.** Many robotics suppliers (Fanuc, Yaskawa, Harmonic Drive, Nabtesco) list only in Tokyo. They show up as `ORG:` nodes when US filers name them. Japan's EDINET is the planned free source for them (see roadmap).
- **Disclosure gaps:** companies often hide supplier names ("a single-source supplier in Asia"). The graph shows what is disclosed, not the full supply chain.
- **Not investment advice:** this is a research tool.

Planning board and clickable design prototype: see the links in [docs/ROADMAP.md](docs/ROADMAP.md).
