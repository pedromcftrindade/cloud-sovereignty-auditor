# ExitPilot 🧭

**How locked-in is your cloud, and who really controls it?** ExitPilot reads a company's
infrastructure code (Terraform for AWS, Azure or Google Cloud) **and** the third-party services its
application really uses (configuration, code, dependencies, container images, network traffic). It
measures how dependent the company is on each provider, how many dependencies are controlled by
companies headquartered outside the EU, and produces a sovereignty report with European
alternatives, starting with Leaseweb.

Built at **Hackathon Portugal 2026 - InfraHack: AI for Cloud Infrastructure** (Leaseweb x Velv),
challenge track 02: *sovereign alternatives for locked-in hyperscale services*.

## Why

- Amazon, Microsoft and Google hold about 70% of the European cloud market.
- Data stored in an EU region of a US provider may still be subject to US laws with
  extraterritorial reach (e.g. the CLOUD Act). **Where the servers are is not who controls them.**
- Under the EU Data Act, switching charges between cloud providers are prohibited from
  12 January 2027. The barrier to leaving becomes technical, not financial.
- Terraform only shows the infrastructure you declared. Email, payments, analytics, error tracking,
  AI APIs and fonts are third-party services that never appear there, yet they lock you in too.

The first step of any exit is knowing **what** locks you in: that is phase 1. Phase 2 turns it
into a migration plan to Leaseweb and European alternatives, with the files to start it.

## What it does (phase 1)

1. **Read** from two sources into one common representation (`exitpilot/model.py`):
   - **2a. Terraform** (`parser.py`): every `.tf` file becomes a resource (provider, type,
     dependencies, region). Files with syntax errors are reported, not fatal.
   - **2b. Third-party services** (`services.py`): hosts, SDK packages and container registries
     found in `.env` files, docker-compose, code, `requirements*.txt`, `pyproject.toml`,
     `package.json`, Dockerfiles and an optional `network_snapshot.json`. Each finding keeps its
     **evidence** (file, line, kind). Secret values are never stored, only variable names and hosts.
     Local and private hosts, Docker service names, placeholder domains, comment lines and
     lookalike domains (e.g. `sendgrid.com.evil.io`) are ignored on purpose.
   - **2c. Suspected waste** (`waste.py`): things that look unused, so you do not pay to migrate them.
     Terraform: disabled resources (`count = 0`), disks and public IPs attached to nothing, security
     groups and target groups nothing uses. Application: services whose SDK is installed but never
     imported and whose variables are set but never read, unused packages, unused `.env` variables.
     Every finding is a **suspicion to review**: a reference anywhere (resource, module input, output),
     observed traffic, or a variable read by an SDK on its own (`SENTRY_DSN`...) counts as use. Found by
     Python, not by the AI: "unused" must be a fact we can show. Phase 2 can leave these items out.
2. **Classify**: each resource gets a category and a lock-in level:
   - 0: standard, exists everywhere (VMs, disks, font files)
   - 1: compatible with a common standard (S3 API, Kubernetes, SMTP, OCI images)
   - 2: managed service or proprietary API with an open-source or European equivalent
   - 3: proprietary, no direct equivalent (DynamoDB, Lambda, Cognito, Firebase)

   Terraform types come from `catalog.json`, services from `services.json` (service, company,
   headquarters). Unknown Terraform types are sent to an LLM (**Mistral**, a European provider, or a local model) in
   one request, and every AI answer is validated or discarded. **Unknown hosts are never sent to the
   LLM**: who owns a service is a fact, so it comes from the curated file or is reported as "not verified".
3. **Score**: `S = 100 x (1 - N / (3 x R))`, where `R` = number of main resources and services and
   `N` = sum of their levels. Support configuration (network, IAM, firewall rules) is listed but not scored.
   Next to it, the **jurisdiction exposure** counts how many dependencies are controlled by companies
   headquartered outside the EU.
4. **Report**: Python computes every number and table, and the LLM only writes the executive summary and
   explanations from those facts. Risks that mention non-existent resources are dropped. Without
   AI, a template text is used, so the report is always produced.

## What it does (phase 2: migrate)

`exitpilot/migrate.py` turns the analysis into a plan. **Nothing is applied**: it is a proposal to review.

1. **Target** for every dependency, by category: a Leaseweb Public Cloud instance, a Leaseweb managed
   service (object storage, CDN, load balancer), open-source software self-hosted on Leaseweb
   (PostgreSQL / MariaDB, Valkey, RabbitMQ, Keycloak...), or a provider headquartered in the EU from
   `services.json` (e.g. SendGrid -> Brevo, Stripe -> Mollie, OpenAI -> Mistral AI). Dependencies that are
   already European are kept; unknown hosts become a decision for a person, never a guess.
2. **Waves** by effort: quick wins, managed services, deep changes (proprietary databases, serverless, payments).
3. **Generated files**:

   | File | What it is |
   |---|---|
   | `terraform/main.tf` | Leaseweb Public Cloud instances with the official provider `leaseweb/leaseweb` |
   | `terraform/cloud-init.yaml` | Installs Docker on the services instance and starts the self-hosted services |
   | `services/docker-compose.yml` | Open-source replacements, pulled from your own registry mirror (e.g. Harbor) |
   | `services/.env.example` | The passwords those services need (empty) |
   | `app.env.example` | New values for the environment variables where a dependency was found |
   | `MIGRATION_PLAN.md` | The plan: before/after, waves, manual work, checks, how to use it |

4. **Projection**: sovereignty score and jurisdiction exposure as they would be after the plan.
5. **Checks**: Terraform syntax and the arguments the Leaseweb provider requires; the generated files are
   scanned with step 2b and must not add any dependency controlled outside the EU; optionally
   `tofu init` + `tofu validate` (needs OpenTofu and internet).

In the app, press **Generate migration plan** after an analysis and download the `.zip`. From the terminal:

```bash
python -m exitpilot examples/loja-mista --migrate out/loja-mista          # writes the plan and files
python -m exitpilot examples/loja-mista --migrate out/loja-mista --tofu   # also runs tofu validate
```

The generated files can be analysed again with ExitPilot: `python -m exitpilot out/loja-mista`.

## Run it

Requires **Python 3.11+**.

```bash
git clone https://github.com/pedromcftrindade/cloud-sovereignty-auditor.git
cd cloud-sovereignty-auditor
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # then put your MISTRAL_API_KEY in .env
python scripts/check_llm.py                            # optional: checks the configured AI works
streamlit run app.py
```

Open http://localhost:8501, choose an example and press **Analyse**. The app also works without a
key (template text) and offline (`EXITPILOT_OFFLINE=1`, cached AI answers only). To analyse your
own project, upload a `.zip` of it.

From the terminal:

```bash
python -m exitpilot examples/loja-mista            # Markdown report
python -m exitpilot examples/loja-mista --json     # JSON summary (score, exposure, services, evidence)
python -m exitpilot my-project --ai                # report text written by an LLM (Mistral by default)
```

### AI model: Mistral's API or a model on your own machine

The AI analyses each dependency and writes the report (see "What the AI does" below). Without it everything still
runs, with template text and without the AI analysis. `LLM_PROVIDER` chooses the model, in `.env` or in the shell:

| `LLM_PROVIDER` | Model | Needs |
|---|---|---|
| `ollama` (our demo) | Mistral 7B on this machine through Ollama's native API (`http://localhost:11434`), the same server as `ollama run mistral` | Ollama running, `ollama pull mistral` |
| `mistral` (code default) | Mistral's API (a European provider) | `MISTRAL_API_KEY` |
| `lmstudio` | A model served by LM Studio on this machine (`http://localhost:1234/v1`) | `LLM_MODEL` |
| `ollama-openai` | Ollama through its OpenAI-compatible endpoint (`http://localhost:11434/v1`) | `LLM_MODEL` |
| `local` | Any server with an OpenAI-compatible API (set `LLM_BASE_URL`) | `LLM_MODEL` |

With a local model nothing leaves the machine. **With Ollama** (what we use in the demo):

```bash
ollama pull mistral                                   # once
LLM_PROVIDER=ollama LLM_MODEL=mistral python scripts/check_llm.py
LLM_PROVIDER=ollama LLM_MODEL=mistral streamlit run app.py
```

The `ollama` provider uses Ollama's native API so it can set the context window (`LLM_NUM_CTX`, default 8192)
and JSON mode. Through the OpenAI-compatible endpoint Ollama keeps its smaller default context and cuts longer
prompts without any error, so the model would miss part of the facts.

Example with LM Studio (load the model and start its local
server first, with a context of at least 8192 tokens):

```bash
LLM_PROVIDER=lmstudio LLM_MODEL=mistral-nemo-instruct-2407 python scripts/check_llm.py   # one tiny request, with timing
LLM_PROVIDER=lmstudio LLM_MODEL=mistral-nemo-instruct-2407 streamlit run app.py
```

For a demo that does not depend on the model being fast or even running, **record the cache once, then run offline**:
answers are saved in `.cache/llm_cache.json`, keyed by the model name, so run both with the same `LLM_MODEL`.

```bash
python -m exitpilot examples/loja-mista --ai                      # records the answers
EXITPILOT_OFFLINE=1 python -m exitpilot examples/loja-mista --ai   # replays them: instant, no model needed
```

### What the AI does

The AI analyses the infrastructure and returns **structured data (JSON)** that the rest of the program uses.
Python extracts the facts (parsers, catalogs); the model then works on them in four ways:

1. **Analyses every dependency**, in small batches. For each one it returns `risk` (low/medium/high),
   `why_it_matters`, `migration_notes` (up to 4 steps specific to that dependency), `code_change_hint`
   (what changes in the application code) and `confidence`. This is stored with the dependency
   (`Resource.ai`, always marked `"source": "ai"`) and used by the report, the migration plan and `analysis.json`.
2. **Classifies Terraform resource types** that are not in the catalog.
3. **Suggests the owner of an unknown host** (`owner_guess`), only when it is sure. It is a suggestion to confirm: the
   host stays "unknown" and out of the score and the exposure until someone adds it to `services.json`.
4. **Writes the report**: executive summary, top risks, next steps and **what to change**: up to 6 concrete
   changes (replace a service with its European option, change a configuration, delete suspected waste), each
   tied to an item from the facts. Items the AI invents are dropped; without AI the list comes from the catalogs.

What keeps it trustworthy:

- The model only sees facts ExitPilot extracted (type, category, level, region, company, where it was found). Never the
  contents of configuration files: no passwords, keys or values.
- Every answer is validated. An address that was not asked, a risk outside the list, a text that is too long or the
  wrong type is dropped or trimmed.
- Levels, companies, jurisdictions, targets, waves and the score come from the curated catalogs, not from the model.
  The model adds analysis and wording: **the numbers are identical with and without it** (a test checks this).
- Every AI statement is labelled (`[AI]` in the plan, a labelled section in the report, `source: "ai"` in the JSON).
- Without a model the program still produces everything except the AI analysis.

### analysis.json: the complete result, to keep working with

```bash
python -m exitpilot examples/loja-mista --ai --out analysis.json
```

One file with the score and exposure, every dependency (facts plus its `ai` block), the report (summary, risks,
next steps, Markdown), and the migration plan (before and after, steps, checks, generated files). The app has the
same file under **Common JSON** > *Download analysis.json (complete)*. Use `--no-ai-analysis` to keep the AI for
the report text only.

Run the tests:

```bash
python -m pytest -q
```

## Examples (`examples/`)

| Example | What it shows | Score | Outside the EU | After the plan (projected) |
|---|---|---|---|---|
| `loja-simples` | Small shop on AWS Ireland: EC2, S3, RDS PostgreSQL, load balancer | 67 | 4 of 4 | 92, 0 of 4 |
| `startup-serverless` | Serverless start-up on AWS USA: Lambda, DynamoDB, SQS, API Gateway, Cognito | 13 | 5 of 5 | 100, 0 of 5 |
| `casos-dificeis` | Remote and local modules, `count` with a variable, data block, unknown type, a broken file | 44 | 3 of 3 | 100, 0 of 3 |
| `azure-loja` | Shop on Azure Netherlands: VM, Blob Storage, PostgreSQL, Cosmos DB, Functions | 33 | 5 of 5 | 93, 0 of 5 |
| `loja-mista` | Terraform on AWS Ireland **plus** the application: docker-compose, `.env.example`, Python code, HTML, Dockerfile, network snapshot. 10 known services, 1 unknown partner API, and traps (local MinIO, private IP, URL in a comment) | 62 | 12 of 14 | 82, 0 of 14 |
| `loja-lixo` | AWS Ireland + a Python app that grew without clean-ups: a disabled staging server, an unattached 500 GB disk, a reserved IP, an unused security group, Twilio and Mixpanel SDKs never imported, an old Maps URL never read | 64 | 12 of 12 | 8 suspected waste items, 7 left out of the plan |

The projection assumes every step is done. Self-hosting lowers lock-in but adds operations work.

## Structure

```
app.py                       Streamlit interface
exitpilot/
  __main__.py                command line (python -m exitpilot)
  model.py                   common representation (Resource, Analysis), shared by 2a and 2b
  parser.py                  2a: Terraform -> common representation (python-hcl2 8.x)
  services.py                2b: config, code, dependencies, images, traffic -> common representation
  waste.py                   2c: suspected waste (unused resources, SDKs, variables)
  catalog.json               categories, European alternatives, lock-in level per Terraform type
  services.json              third-party services: hosts, packages, company, headquarters, level
  classifier.py              catalog + services lookup, prefixes, modules, validated AI fallback
  scoring.py                 sovereignty score and jurisdiction exposure
  regions.py                 region -> country / EU zone / jurisdiction note
  llm.py                     Mistral API or local model server (LM Studio, Ollama), disk cache, offline mode
  report.py                  Markdown report (numbers by Python, text by AI)
  analysis.py                pipeline (2a + 2b -> classify -> score -> report), dependency graph, safe zip
  ai_analysis.py             the AI analyses every dependency: validated JSON used by report, plan and export
  export.py                  analysis.json: facts, AI analysis, report and migration plan in one file
  migrate.py                 phase 2: targets, waves, generated files, projection, checks
examples/                    input examples (see table above)
tests/                       140 tests (pytest), including headless runs of the app and a fake model server
docs/                        team plan and prompts used to generate test data
```

## Adding a provider, a type or a service

- Terraform type: add one line to `types` (or `prefixes`) in `catalog.json` with its `category` and `level`.
- Third-party service: add one entry to `services.json` with its `domains` (or `host_patterns`),
  `packages`, `company`, `country`, `zone` (`EU`, `Europe (non-EU)`, `Outside Europe`), `category`
  and `level`. Check the company's headquarters on its official website before adding it.

Categories and their alternatives are written once in `catalog.json` and shared by both.

## Limitations (honest list)

- Infrastructure created by hand in a web console cannot be analysed yet.
- Remote Terraform modules are estimated from their name. Their internals are not downloaded.
- Services are found by host, package or image. A service reached only through an IP address, or
  whose host is built at runtime from several variables, is only seen if it appears in a network snapshot.
- `services.json` covers a curated list of common services; anything else is reported as "not verified".
- The network snapshot is a file you provide (for example exported DNS logs); live capture is not built in.
- Phase 2 generates Leaseweb instances only. The Leaseweb Terraform provider has no managed database or
  object storage resources, so databases, caches and queues are self-hosted, and object storage, CDN and
  load balancers are steps to do by hand. Data is never moved automatically.
- The Leaseweb Public Cloud API (and so its Terraform provider) is in beta: pin a provider version after
  checking the registry. `tofu validate` only runs where OpenTofu is installed.
- The generated images come from your own registry mirror (`registry.internal`): mirror the upstream
  images named in the comments first.
- The report is technical information, not legal advice.
- The free Mistral plan may use requests for training: use it with fictitious data only.
  For real customers, use a paid plan or a self-hosted open-weight model.

## Roadmap

- Phase 2: generate Leaseweb load balancers, scan the generated Terraform with Checkov, and guide the
  data migration (database dumps, object copies).
- Read a live cloud account through read-only APIs, and live network traffic.
- Continuous vulnerability tracking with the EU Vulnerability Database (ENISA EUVD).

## License

Apache License 2.0. See `LICENSE`.
