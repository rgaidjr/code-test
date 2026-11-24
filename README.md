# PhilGEPS procurement explorer

Ask questions about `DATASETS_PHILGEPS.csv`, let a Python agent choose SQL queries,
then run the same investigation through a separate MCP client and server.

For the AWS deployment and CI/CD learning path, see
[the AWS DevOps walkthrough](docs/AWS_DEVOPS.md). It covers the HTTP API,
container, infrastructure, GitHub Actions, authentication, staging, production,
and rollback. The AWS foundation and GitHub environments are configured;
the first application deployment is being verified through GitHub Actions.

## Start with the demo — no API key needed

From PowerShell in this directory:

```powershell
.\.venv\Scripts\python.exe demo.py
```

This runs fixed, reproducible queries over the real dataset and prints:

1. Snapshot coverage and the difference between line-item rows and notices.
2. Top five regions by deduplicated approved budget.
3. The five largest notices, with source row and bid reference.
4. Top suppliers in the populated award subset.
5. How much the largest notice contributes to its region's budget.
6. That notice's original notice-budget and item-budget fields.
7. Counts excluded from the budget rankings.

The largest recorded notice budget makes a useful investigation: it is
PHP 220 billion for bid `13253363`, while its item budget is PHP 104,907.45.
The demo preserves both recorded values. A discrepancy is a reason to inspect
the source, not proof of an error or misconduct.

Repeat exactly the same demo through MCP:

```powershell
.\.venv\Scripts\python.exe demo.py --mcp
```

Both demos use real tool results. They do not simulate model reasoning.

## Let the agent investigate

Put your API key in `.env` beside `agent.py` (a blank file is already provided):

```dotenv
OPENAI_API_KEY=your-api-key-here
```

Both `agent.py` and `mcp_client.py` automatically load that file, even when run
from another working directory. Existing environment variables take priority.
You can also set `OPENAI_MODEL` in `.env`. Keep the key private; `.env` is ignored
by Git, and `.env.example` contains the shareable template. Then ask a question:

```powershell
.\.venv\Scripts\python.exe agent.py "Which five regions have the largest approved procurement budgets? Give examples of the biggest notices."
```

Good follow-up demo questions:

```powershell
.\.venv\Scripts\python.exe agent.py "Investigate the largest notice budget. Compare its notice and item budgets, and calculate how much it affects its region's total."
.\.venv\Scripts\python.exe agent.py "Find school computer procurement in Region IV-A. Show the largest notices, their entities, budgets, and source IDs."
.\.venv\Scripts\python.exe agent.py "Compare procurement modes by notice count and approved budget. What are the limitations of this comparison?"
.\.venv\Scripts\python.exe agent.py "Which suppliers have the largest recorded award amounts? Explain how complete the award data is."
```

Run any question through the separate MCP client:

```powershell
.\.venv\Scripts\python.exe mcp_client.py "Investigate the largest notice budget and show the original evidence."
```

The agent writes tool requests and returned JSON to stderr, and its final answer
to stdout. Watch it discover the schema, choose queries, inspect results, and
explain them. Every tool execution, including an invalid request, consumes one
of the default eight call slots. `--max-calls 12` changes that budget. At the
limit the loop requests a final answer with tools disabled; further calls stop
with an error. Query errors are returned so the model can correct its SQL.

The default model is `gpt-6.2-astra`; use `--model MODEL` or `OPENAI_MODEL` to
choose another Responses API model that supports function calls. Model calls
use API credits. The configured key was checked with a minimal request, which
returned `credit_balance_exhausted` / `insufficient_quota`. Add API credits before
running model-led investigations; no live model investigation has completed yet.

If the CLI reports `credit_balance_exhausted`, add credits in
[API billing](https://platform.openai.com/settings/organization/billing).
If it reports a request/token rate limit, wait for the limit to reset and reduce
concurrent requests. Both return HTTP 429, but require different fixes. The CLI
now distinguishes these cases without printing raw API responses or secrets.

## The two tools

| Tool | Input | What it returns |
| --- | --- | --- |
| `describe_dataset` | None | Snapshot coverage, SQL schema, regions, parse issues, counting rules, and limitations |
| `query_dataset` | `sql: str` | Column names, row values, exact SQL, source name, and truncation indicators |

`query_dataset` accepts one read-only SQLite query, including CTEs, joins,
filters, window functions, and aggregations. Writes, attachments, PRAGMAs,
internal tables, and extension loading are rejected. Queries have a five-second
execution budget, return at most 30 rows, and cap individual text cells and
returned row values. No Python or shell code is executed from model output.

The three SQL tables have different grains:

| Table | One row represents | Use it for |
| --- | --- | --- |
| `records` | One original CSV data record, usually a line item | Item searches, original evidence, all 47 mapped source columns |
| `notices` | One analytical notice identity | Notice counts, approved budgets, region/entity/category/mode comparisons |
| `awards` | One award reference plus procuring entity | Recorded award amounts and supplier comparisons |

Example SQL without a model:

```powershell
.\.venv\Scripts\python.exe philgeps.py
.\.venv\Scripts\python.exe philgeps.py --sql "SELECT region, COUNT(*) AS notices, SUM(approved_budget_cents)/100.0 AS budget_php FROM notices WHERE metadata_variant_count=1 AND approved_budget_cents IS NOT NULL GROUP BY region ORDER BY SUM(approved_budget_cents) DESC LIMIT 5"
```

Amounts use integer centavos so sums remain exact. Divide by `100.0` for PHP
display. `NULL` means unavailable; zero stays zero. Dates are normalized from
day/month/year to ISO dates. `source_row` is the one-based data-record number
after the header, not necessarily a physical line number in a multiline CSV.

## Data rules that matter

The supplied snapshot contains 196,202 line-item records, with published dates
from July 1 to September 27, 2026. The index derives 148,769 analytical notices
and 348 populated award identities. Only 618 raw rows have contract amounts.

Repeated line items repeat notice budgets and sometimes award amounts. Summing
those fields across `records` would double-count them. `notices` groups nonzero
bid references. Missing/zero references use entity, solicitation, title, and
published date; missing solicitation falls back to an individual source row.
These fallback identities are documented assumptions, not verified unique
contracts. `awards` excludes missing award references.

Seven notice identities have conflicting budget values; their deduplicated
budget is `NULL` instead of arbitrarily selecting a value. Metadata conflicts
are counted in `metadata_variant_count`; grouped rankings filter that to one.
Conflicting award amounts also become `NULL`. Supplier rankings require one
supplier variant and a populated amount/name. Both the agent and demo disclose
the sparse award coverage. Approved budgets are not actual expenditure.

The CSV is read as Windows-1252 by default to preserve its accented text.
Blank, `NULL`, and `N/A` fields become SQL NULL. Invalid amounts/dates become
NULL and are counted in `describe_dataset().parse_issues`. The original CSV is
never edited. Rankings retain outliers and show evidence for review.

The first call builds `.cache/philgeps.sqlite3` (about 181 MiB). Later calls
reuse it, including across the direct and MCP processes. A changed source path,
file size, modification time, encoding, or index version triggers a rebuild.
Stop active readers before replacing the source on Windows. The index is
disposable and `.cache/` is ignored by Git.

Configuration, shared with the MCP subprocess:

| Variable | Default |
| --- | --- |
| `PHILGEPS_CSV` | `DATASETS_PHILGEPS.csv` beside the Python files |
| `PHILGEPS_ENCODING` | `cp1252` |
| `PHILGEPS_CACHE_DIR` | `.cache` beside the Python files |

## MCP Inspector

Use a current supported Node version. The installed Node 22.16 gives an engine
warning with Inspector 2.16, although its CLI worked in the earlier exercise.
Use `@latest` explicitly to avoid npm selecting a deprecated Inspector release.

```powershell
npx.cmd -y @modelcontextprotocol/inspector@latest .\.venv\Scripts\python.exe mcp_server.py
```

Connect over stdio, discover both tools, call `describe_dataset` with `{}`, then
call `query_dataset` with:

```json
{"sql":"SELECT region, COUNT(*) AS notices FROM notices GROUP BY region ORDER BY notices DESC LIMIT 5"}
```

CLI equivalents:

```powershell
npx.cmd -y @modelcontextprotocol/inspector@latest --cli .\.venv\Scripts\python.exe mcp_server.py --method tools/list
npx.cmd -y @modelcontextprotocol/inspector@latest --cli .\.venv\Scripts\python.exe mcp_server.py --method tools/call --tool-name describe_dataset
npx.cmd -y @modelcontextprotocol/inspector@latest --cli .\.venv\Scripts\python.exe mcp_server.py --method tools/call --tool-name query_dataset --tool-arg "sql=SELECT COUNT(*) AS notices FROM notices"
```

## Setup and checks

Python 3.12 is used for cloud builds and CI. The project already has a virtual environment. To
recreate it on this machine, whose Windows `python` alias is inaccessible:

```powershell
& C:\Paeng\miniconda3\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pytest -q
```

Tests cover repeated line items, fallback identities, exact monetary sums,
missing and conflicting values, accented text, date parsing, cache refresh,
SQL restrictions, timeout enforcement, truncation, the tool loop, and real MCP subprocess parity.
Two optional live-model checks use a small fixture, one through each transport:

```powershell
$env:RUN_LIVE_TESTS = "1"
.\.venv\Scripts\python.exe -m pytest -q -k live_model_count
Remove-Item Env:RUN_LIVE_TESTS
```

Windows asyncio stalled in the restricted Codex sandbox during the original
exercise; successful async checks run outside that sandbox.

Verified: 49 tests passed, two live-model checks skipped, both demo transports
completed, and Inspector discovered both tools and executed a real SQL query.
An independent CSV aggregation reconciled all 18 regional budget sums and the
notice count to the indexed results. Dependency and syntax checks passed.

## Code map

- `philgeps.py`: source ingestion, cache, normalization, two business tools.
- `agent.py`: explicit Responses API loop and direct tool dispatch.
- `mcp_server.py`: exposes the same tools; translates expected SQL errors into MCP tool errors.
- `mcp_client.py`: discovers schemas and calls tools across stdio, reusing the agent loop.
- `demo.py`: repeatable presentation through either transport, no API key.
- `tests/test_exercise.py`: service, loop, MCP, and opt-in model checks.

The old purchase-order records and reconciliation code have been removed.

References: [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling),
[official MCP Python SDK](https://py.sdk.modelcontextprotocol.io/get-started/),
[MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector).
