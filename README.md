# ExitPilot 🧭

**How locked-in is your cloud?** ExitPilot reads a company's infrastructure code (Terraform for
AWS, Azure or Google Cloud), measures how dependent it is on each US hyperscaler, and produces a
sovereignty report with European alternatives, starting with Leaseweb.

Built at **Hackathon Portugal 2026 - InfraHack: AI for Cloud Infrastructure** (Leaseweb x Velv),
challenge track 02: *sovereign alternatives for locked-in hyperscale services*.

## Why

- Amazon, Microsoft and Google hold about 70% of the European cloud market.
- Data stored in an EU region of a US provider may still be subject to US laws with
  extraterritorial reach (e.g. the CLOUD Act).
- Under the EU Data Act, switching charges between cloud providers are prohibited from
  12 January 2027. The barrier to leaving becomes technical, not financial.

The first step of any exit is knowing **what** locks you in. That is phase 1 (this repo).
Phase 2 (planned) generates the migration to Leaseweb and validates it with OpenTofu.

## What it does (phase 1)

1. **Parse**: reads every `.tf` file into a common JSON representation (provider, type,
   dependencies, region). Files with syntax errors are reported, not fatal.
2. **Classify**: each resource gets a category and a lock-in level from `exitpilot/catalog.json`:
   - 0: standard, exists on every cloud (VMs, disks)
   - 1: compatible with a common standard (S3 API, Kubernetes)
   - 2: managed service with an open-source equivalent (managed PostgreSQL, queues)
   - 3: proprietary, no direct equivalent (DynamoDB, Lambda, Cognito)

   Unknown types are sent to an LLM (**Mistral**, a European provider) in one request. Every AI answer is
   validated (valid category, level 0-3, a type we actually asked about) or discarded.
3. **Score**: `S = 100 x (1 - N / (3 x R))`, where `R` = number of main resources and `N` = sum of
   their levels. Support configuration (network, IAM, firewall rules) is listed but not scored.
4. **Report**: Python computes every number and table, and the LLM only writes the executive summary and
   explanations from those facts. Risks that mention non-existent resources are dropped. Without
   AI, a template text is used, so the report is always produced.

## Run it

Requires **Python 3.11+**.

```bash
git clone <this repo> && cd exitpilot
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # then put your MISTRAL_API_KEY in .env
python scripts/check_mistral.py                        # optional: checks the key works
streamlit run app.py
```

Open http://localhost:8501, choose an example and press **Analyse**. The app also works without a
key (template text) and offline (`EXITPILOT_OFFLINE=1`, cached AI answers only).

Run the tests:

```bash
python -m pytest -q
```

## Examples (`examples/`)

| Example | What it shows | Expected score |
|---|---|---|
| `loja-simples` | Small shop on AWS Ireland: EC2, S3, RDS PostgreSQL, load balancer | 67 |
| `startup-serverless` | Serverless start-up on AWS USA: Lambda, DynamoDB, SQS, API Gateway, Cognito | 13 |
| `casos-dificeis` | Remote and local modules, `count` with a variable, data block, unknown type, a broken file | 44 |
| `azure-loja` | Shop on Azure Netherlands: VM, Blob Storage, PostgreSQL, Cosmos DB, Functions | 33 |

## Structure

```
app.py                     Streamlit interface
exitpilot/model.py         common representation (Resource, Analysis)
exitpilot/parser.py        Terraform -> common representation (python-hcl2 8.x)
exitpilot/catalog.json     categories, European alternatives, lock-in level per type
exitpilot/classifier.py    catalog lookup, prefixes, modules, validated AI fallback
exitpilot/scoring.py       sovereignty score
exitpilot/regions.py       region -> country / EU zone / jurisdiction note
exitpilot/llm.py           Mistral client with disk cache and offline mode
exitpilot/report.py        Markdown report (numbers by Python, text by AI)
exitpilot/analysis.py      pipeline, dependency graph, safe zip extraction
tests/                     41 tests (pytest), including a headless run of the app
```

## Adding a provider or a type

Add one line to `types` (or `prefixes`) in `catalog.json` with its `category` and `level`.
Categories and their alternatives are written once and shared by all providers.

## Limitations (honest list)

- Only Terraform input today. Infrastructure created by hand in a web console cannot be analysed yet.
- Remote modules are estimated from their name. Their internals are not downloaded.
- The report is technical information, not legal advice.
- The free Mistral plan may use requests for training: use it with fictitious data only.
  For real customers, use a paid plan or a self-hosted open-weight model.

## Roadmap

- Phase 2: generate the Leaseweb version (Terraform + cloud-init for managed services), validated
  with `tofu validate` and scanned with Checkov.
- Read a live cloud account through read-only APIs.
- Continuous vulnerability tracking with the EU Vulnerability Database (ENISA EUVD).

## License

Apache License 2.0. See `LICENSE`.
