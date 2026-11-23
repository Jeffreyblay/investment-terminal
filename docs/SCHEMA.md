# Schema

## Graph model
```
(Company)-[SUPPLIES {component, confidence, citations[]}]->(Company)
(Company)-[COMPETES_WITH]->(Company)
(Company)-[PARTNERS_WITH | SUBSIDIARY_OF | ACQUIRED | INVESTED_IN | CONTRACTED_BY | MANUFACTURES_FOR]->(Company)
(Company)-[MEMBER_OF {segment, is_seed}]->(Industry)          # stored as industry_members
```
- Node IDs: `CIK:<int>` (SEC filer) or `ORG:<normalised name>` (private, foreign-only, government).
- `customer_of` is stored as extracted, but **exported flipped** to `supplies`, so supply edges always point supplier → customer.

Planned node types (roadmap): `Segment`, `Component` (e.g. "harmonic reducer", "lidar"), `Person` (officers and directors, for board interlocks), `Filing`.

## SQLite tables (`data/terminal.db`)

### entities
| column | type | notes |
|---|---|---|
| id | TEXT PK | `CIK:…` or `ORG:…` |
| name | TEXT | SEC name or the name as found |
| ticker | TEXT | null for non-SEC entities |
| cik | INTEGER | |
| listed | INTEGER | 1 if an SEC filer |
| financials | TEXT (JSON) | `{"revenue": {"value", "period_end", "concept"}, "net_income": …, "rnd": …}` |

Added in v0.2 (migrated automatically on open): `sic` (TEXT), `sic_desc` (TEXT), `expanded_at` (TEXT, when this company's own filing was processed).

### company_tags (v0.2)
| column | notes |
|---|---|
| entity_id, tag | PK; `tag` normalised, e.g. `robotics`, `semiconductors`, `ai compute` |
| role | `supplier`, `maker`, `customer`, `operator` or `platform` |
| confidence | 0–1 (max kept across filings) |
| evidence | sentence from the Business section |
| accession | filing it came from |

Tags are open-ended industries: `itx tag robotics` = every company with that tag + direct partners.

### financials_annual (v0.3)
One row per company, metric and fiscal year, from XBRL `companyfacts` (`edgar.annual_series`, last 5 years).
| column | notes |
|---|---|
| entity_id, metric, period_end | PK; `metric` is a key of `XBRL_METRICS` (revenue, gross_profit, operating_income, net_income, rnd, eps_diluted, cash, total_assets, total_liabilities, equity, long_term_debt, operating_cash_flow, capex) |
| value | USD (EPS in USD per share) |
| concept | the us-gaap concept the value came from |
| filed, accession | the filing the value came from; the latest filing wins, so restatements apply |

A company's rows are replaced as a whole each time it's processed. `entities.financials` still holds the latest year for the viewer panel; exported nodes also carry `history: {metric: [{period_end, value}, ...]}`, newest first.

```sql
-- Revenue history for one company
SELECT period_end, value FROM financials_annual
WHERE entity_id = 'CIK:97210' AND metric = 'revenue' ORDER BY period_end;
```

### industry_members
`(industry, entity_id)` PK, `segment`, `is_seed`. Companies that are only linked to (not members) still appear in exports with `in_industry: false`.

### filings
`accession` PK, `entity_id`, `form`, `filed`, `period`, `url`, `status` (`fetched` / `extracted` / `failed`).

### relationships
| column | notes |
|---|---|
| source_id, relation, target_id | the edge |
| component | product/part if stated |
| evidence | exact supporting sentence |
| confidence | 0–1 from the model |
| accession, section | provenance (which filing, which item) |
| model | which LLM produced it, for comparing models |
| raw_source, raw_target | names before resolution, for debugging |
`UNIQUE(source_id, relation, target_id, accession, evidence)` stops duplicates on re-runs.

### mentions
Every raw name and how it was resolved (`alias`, `ticker`, `exact`, `fuzzy:NN`, `unresolved`). It powers `itx review`.

## Export JSON (`web/data/<name>.json`)
Names: `<industry>.json`, `company-<ticker>.json`, `tag-<tag>.json`. Every export also updates `web/data/index.json` (`{"datasets": [{"file", "label", "mode", "nodes"}]}`), which drives the viewer's picker.
```jsonc
{
  "mode": "industry",                    // "industry" | "company" | "tag"
  "center": "CIK:97210",                 // company mode only: the searched company
  "tag": "robotics",                     // tag mode only
  "industry": "Robotics",                // display label
  "segments": ["Components & chips", "Robot makers", "Customers"],
  "sample": false,                       // true only for the bundled sample
  "nodes": [{ "id": "CIK:97210", "name": "TERADYNE, INC", "ticker": "TER", "cik": 97210,
              "listed": true, "segment": "Robot makers", "is_seed": true, "in_industry": true,
              "sic": "3825", "sic_desc": "…", "expanded": true,
              "tags": [{ "tag": "robotics", "role": "maker", "confidence": 0.9 }],
              "financials": { "revenue": { "value": 0, "period_end": "YYYY-12-31", "concept": "Revenues" } } }],
  "edges": [{ "source": "CIK:1045810", "target": "CIK:97210", "relation": "supplies",
              "components": ["GPUs"], "confidence": 0.9,
              "citations": [{ "evidence": "…", "form": "10-K", "filed": "YYYY-MM-DD",
                              "filing_url": "https://www.sec.gov/Archives/…", "section": "business",
                              "confidence": 0.9 }] }],
  "analytics": { "customers_per_supplier": {"id": n}, "betweenness": {"id": x}, "communities": [["id", …]] }
}
```

## Useful queries
```sql
-- Most-cited suppliers in an industry
SELECT e.name, COUNT(DISTINCT r.target_id) customers
FROM relationships r JOIN entities e ON e.id = r.source_id
WHERE r.relation = 'supplies' AND r.confidence >= 0.6
GROUP BY e.id ORDER BY customers DESC LIMIT 20;

-- Low-confidence edges to review by hand
SELECT raw_source, relation, raw_target, confidence, evidence FROM relationships
WHERE confidence < 0.6 ORDER BY confidence;
```
```cypher
// Neo4j: two-hop supply chain into a company
MATCH p = (s:Company)-[:SUPPLIES*1..2]->(c:Company {ticker: 'SYM'}) RETURN p;
```
