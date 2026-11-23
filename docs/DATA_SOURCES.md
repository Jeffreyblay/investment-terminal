# Data sources (all free)

## Used now: SEC EDGAR
SEC filings are US government records in the public domain. No key is needed, but **every request must send a `User-Agent` with a name and email**. Stay under **10 requests per second**; the code defaults to 8. Breaking either rule gets your IP temporarily blocked.

| Data | Endpoint | Notes |
|---|---|---|
| Ticker → CIK → name | `https://www.sec.gov/files/company_tickers.json` | ~10k current filers, including ADRs |
| Filing index per company | `https://data.sec.gov/submissions/CIK##########.json` | `filings.recent` arrays: form, accession, dates, primary document |
| Filing documents | `https://www.sec.gov/Archives/edgar/data/<cik>/<accession-no-dashes>/<primaryDocument>` | 10-K / 20-F HTML (inline XBRL) |
| XBRL financials | `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` | All tagged facts; we read us-gaap Revenues, NetIncomeLoss, R&D |
| Full-text search | `https://efts.sec.gov/LATEST/search-index?q="phrase"&forms=10-K` | 2001 onward; used by `itx discover` |
| Bulk (optional) | `https://www.sec.gov/Archives/edgar/daily-index/` and nightly `submissions.zip` / `companyfacts.zip` | Use for thousands of companies instead of per-company calls |

### What each form gives you
- **10-K** (US annual report): Item 1 Business (products, customers, suppliers, competitors), Item 1A Risk Factors (single-source suppliers, customer concentration), Item 7 MD&A (segments, major customers).
- **20-F**: the foreign-filer equivalent (Item 4 Information on the Company, Item 3.D Risk Factors).
- **10-Q / 8-K** (later): new contracts, acquisitions and supply deals as they happen.
- **S-1 / F-1** (later): new IPOs, which often have very detailed customer and supplier sections.
- **Exhibit 21** (later): the list of subsidiaries attached to the 10-K, a clean source for `subsidiary_of`.

### Customer concentration
US accounting rules (ASC 280) require disclosure of customers that make up 10% or more of revenue. They are often unnamed ("Customer A"), but when named they are high-confidence `supplies` edges.

## Planned: other free sources
| Source | What it adds | Access | Watch out |
|---|---|---|---|
| **EDINET** (Japan FSA) | Fanuc, Yaskawa, Harmonic Drive, Nabtesco, Keyence annual reports (有価証券報告書) | Free API; free key from the EDINET site | Japanese text; most Qwen models read it well |
| **SEDAR+** (Canada) | Canadian-listed companies | Public website | Check terms before automated bulk downloads |
| **Companies House** (UK) | UK company records, officers, ownership | Free API key | Records only, not supply-chain text |
| **USPTO / PatentsView** | Who patents what; co-assignees mean partnerships | Free API / bulk downloads | Assignee names need the same resolution step |
| **USAspending.gov** | Government contracts (big for space and defense) | Free API, no key | Map recipient names / UEI to companies |
| **Wikidata** | Parent/subsidiary, industry tags, cross IDs (LEI, ISIN) | Free SPARQL endpoint | Crowd-sourced; use as a hint, not as evidence |
| **GLEIF LEI** | Legal entity IDs and direct/ultimate parent relations | Free API | Great for resolving subsidiaries |
| **Company IR press releases** | Partnerships and contracts | Public pages | Site-by-site; respect robots.txt |

## Prices (optional)
- `yfinance` (`pip install .[prices]`): free but **unofficial**. It scrapes Yahoo Finance, can break without warning, and Yahoo's terms allow personal use only. Fine for a personal terminal; don't redistribute the data.
- A free official alternative with limits: Stooq CSV downloads (end-of-day). Check its terms.

## Data quality notes
- Filings name only some suppliers; many say "a limited number of suppliers". Treat the graph as a **lower bound** on the true supply chain.
- Relationships are dated by filing. An edge from a 2023 10-K may no longer be true; `filed` is stored on every citation.
- Use the most recent 2–3 annual reports per company. Older ones mostly repeat the same text.
