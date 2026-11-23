# Extraction with a free local LLM

## Setup
1. Install Ollama: https://ollama.com (macOS, Windows, Linux).
2. Pull a model: `ollama pull qwen2.5:7b`
3. Ollama serves on `http://localhost:11434`; the pipeline checks it's running before extracting.

## Choosing a model
| Model (Ollama tag) | Size on disk | Hardware | Notes |
|---|---|---|---|
| `qwen2.5:3b` | ~2 GB | any laptop, CPU ok | Fast first pass, misses more |
| `qwen2.5:7b` (default) | ~4.7 GB | 16 GB RAM; GPU or Apple Silicon helps a lot | Good balance; strong at JSON output |
| `llama3.1:8b` | ~4.9 GB | same | Alternative to compare against |
| `qwen2.5:14b` | ~9 GB | 32 GB RAM or 12 GB+ GPU | Noticeably better on dense legal text |

Switch with `--model` or `OLLAMA_MODEL`. Each relationship stores the model name, so you can compare models on the same filings (`itx reset-extract`, then re-run). Check each model's license before any commercial use. Sizes are approximate, and the model lineup changes; run `ollama list` and check the Ollama library for current tags.

## Speed
- The section-cutting step keeps 20–80 passages per 10-K.
- With a 7B model, expect very roughly a few seconds per passage on a GPU or Apple Silicon, and much longer on CPU only. Use `--max-passages` to cap work per filing, and `--limit` to process a few filings per run.
- The work resumes: stop at any time and run `itx extract` again.

## Prompt contract (see `extract.py`)
- The model gets the filer's name and treats "we/our/the Company" as the filer.
- It only extracts **named** counterparties.
- Output is forced to a JSON schema through Ollama's `format`, with `temperature: 0`.
- Each item has: `subject, relation, object, component, evidence, confidence`.

## Guards against bad output
- Unknown relation types and self-loops are dropped.
- **Mention check:** at least one of the two names must appear in the passage.
- Confidence is clamped to 0–1. `export --min-conf 0.5` hides weak edges; raise it to 0.7 for a cleaner graph.

## Improving accuracy (in order of payoff)
1. **Aliases:** run `itx review` after each batch and map the top unresolved names in `config/aliases.yaml`.
2. **Hand-check a sample:** pick 50 relationships, mark each correct or incorrect, and track precision per model. Store the labels in a CSV under `data/labels/` (planned: `itx eval`).
3. **Better passages:** add industry terms to `keywords` in the YAML; extend the `CUES` regex in `sections.py`.
4. **A bigger model** for the final run, with a small one for exploration.
5. **Two-pass checking** (planned): a second prompt asks "does this sentence support X supplies Y? yes/no" and drops the no's.
6. **Few-shot examples** (planned): add 3–4 worked examples from real filings to the system prompt.

## Common failure modes
| Symptom | Cause | Fix |
|---|---|---|
| Filer appears as "the Company" node | Model didn't substitute the name | Already in the prompt; add an alias `the Company: <TICKER>` per run, or a post-fix in `cli.py` |
| Direction reversed (customer listed as supplier) | Ambiguous sentences | Lower trust in `customer_of`; the planned two-pass check fixes most |
| Products extracted as companies ("Optimus", "da Vinci") | Capitalised product names | Add them to a stoplist (planned), or alias them to their maker |
| Same company split in two | Names differ ("ABB Ltd" vs "ABB Robotics") | Alias, or lower the fuzzy threshold in `Resolver(threshold=…)` carefully |
