# Licensing and staying free

_Not legal advice. Check the current license text of each component before publishing or commercialising._

## Components
| Component | License | Cost | Notes |
|---|---|---|---|
| This project's code | MIT (suggested) | free | Change in `pyproject.toml` if you prefer another |
| SEC EDGAR data | US public domain | free | Fair-access rules: User-Agent, ≤10 req/s |
| requests, BeautifulSoup, lxml, PyYAML, NetworkX, RapidFuzz | Apache-2.0 / MIT / BSD | free | Permissive; fine for any use |
| SQLite | Public domain | free | |
| Ollama | MIT | free | The runtime only; models have their own licenses |
| Qwen 2.5 / Llama 3.x weights | Per-model licenses (e.g. Apache-2.0 for many Qwen sizes, the Llama Community License for Llama) | free | Read the model card; some add conditions for very large commercial users |
| Cytoscape.js | MIT | free | |
| Neo4j Community Edition (optional) | GPLv3 | free | Using it as a separate database server is fine; **bundling/redistributing it** brings GPL obligations. Enterprise is paid. |
| Memgraph (optional alternative) | Business Source License | free to self-host | Read the BSL terms if offering it as a service |
| Apache AGE on Postgres (optional alternative) | Apache-2.0 | free | Most permissive graph option |
| yfinance (optional) | Apache-2.0 (library) | free | **Yahoo's data terms**: personal use only, no redistribution |

## Publishing the project as open source
- Keep Neo4j optional, as it is now, so the core stays permissively licensed.
- Don't commit `data/` (it's in `.gitignore`): it holds your cache and database.
- Sharing exported graphs built from SEC filings is fine; the filings are public domain. Keep the citations so others can verify.
- Don't publish price data from yfinance or any paid provider.

## Free hosting
- **Viewer:** `web/` is static, so GitHub Pages, Netlify or Cloudflare Pages host it free. Commit the exported JSON you want to show.
- **Pipeline:** run it locally, since it needs Ollama. A scheduled GitHub Action could refresh EDGAR data, but CI runners are too small for LLM extraction. Keep extraction local.
