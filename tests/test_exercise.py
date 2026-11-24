import asyncio
import csv
import json
import os
from types import SimpleNamespace

import pytest
from mcp import Client

from agent import InvestigationError, TOOLS, investigate, make_client
from mcp_client import discover_tools, execute_mcp, server_parameters
from philgeps import COLUMNS, describe_dataset, ensure_database, query_dataset


@pytest.mark.parametrize("code,error_type,expected", [
    ("credit_balance_exhausted", "insufficient_quota", "Add credits"),
    ("insufficient_quota", "insufficient_quota", "Check API credits"),
    (None, "insufficient_quota", "Check API credits"),
    ("rate_limit_exceeded", "rate_limit_error", "request/token rate limit"),
    ("project_spend_limit_exceeded", "insufficient_quota", "project spend limit"),
])
def test_rate_limit_diagnostics_hide_api_body(code, error_type, expected):
    import httpx2
    from openai import RateLimitError
    from agent import format_cli_error
    error = RateLimitError("sensitive raw message", response=httpx2.Response(429,
        request=httpx2.Request("POST", "https://api.openai.com/v1/responses")),
        body={"code": code, "type": error_type, "message": "sensitive raw message"})
    text = format_cli_error(error)
    assert expected in text
    assert "sensitive" not in text


def test_wrapped_mcp_error_preserves_diagnosis():
    from agent import format_cli_error
    wrapped = ExceptionGroup("MCP cleanup", [ExceptionGroup("transport", [InvestigationError("Configured key missing")])])
    assert format_cli_error(wrapped) == "Configured key missing"


def test_dotenv_loads_from_script_location_before_model_default(tmp_path, monkeypatch):
    import agent
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=test-local-key\nOPENAI_MODEL=test-local-model\n", encoding="utf-8")
    monkeypatch.setattr(agent, "ENV_FILE", env_file)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    args = agent.argument_parser("test").parse_args(["Analyze"])
    assert os.environ["OPENAI_API_KEY"] == "test-local-key"
    assert args.model == "test-local-model"


def test_dotenv_preserves_existing_environment(tmp_path, monkeypatch):
    import agent
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=file-key\nOPENAI_MODEL=file-model\n", encoding="utf-8")
    monkeypatch.setattr(agent, "ENV_FILE", env_file)
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    monkeypatch.setenv("OPENAI_MODEL", "environment-model")
    args = agent.argument_parser("test").parse_args(["Analyze", "--model", "cli-model"])
    assert os.environ["OPENAI_API_KEY"] == "environment-key"
    assert os.environ["OPENAI_MODEL"] == "environment-model"
    assert args.model == "cli-model"


@pytest.fixture
def dataset(tmp_path, monkeypatch):
    path = tmp_path / "sample.csv"
    base = {header: "NULL" for header, _, _ in COLUMNS}

    def row(bid, budget, item, **overrides):
        return {**base, "Procuring Entity (PE)": "CITY OF BIÑAN", "Region": "Region IV-A",
                "Bid Reference No.": bid, "Notice Title": "School computers",
                "Solicitation No.": "SOL-1", "Published Date": "05/08/2026",
                "Approved Budget of the Contract": budget, "Line Item No": item,
                "Bid Notice Status": "Awarded", **overrides}

    records = [
        row("100", "100.10", "1", **{"Award Reference No.": "A-1", "Contract Amount": "90.05", "Awardee Organization Name": "Supplier A"}),
        row("100", "100.10", "2", **{"Award Reference No.": "A-1", "Contract Amount": "90.05", "Awardee Organization Name": "Supplier A"}),
        row("200", "200.20", "1"),
        row("300", "300.00", "1"),
        row("300", "400.00", "2"),
        row("0", "50.00", "1", **{"Solicitation No.": "FALLBACK"}),
        row("0", "50.00", "2", **{"Solicitation No.": "FALLBACK"}),
        row("0", "NULL", "1", **{"Solicitation No.": "NULL"}),
        row("400", "0", "1", **{"Published Date": "bad date"}),
        row("500", "10.00", "1", **{"Award Reference No.": "A-2", "Contract Amount": "10", "Awardee Organization Name": "Supplier B"}),
        row("500", "10.00", "2", **{"Award Reference No.": "A-2", "Contract Amount": "11", "Awardee Organization Name": "Supplier B"}),
    ]
    with path.open("w", encoding="cp1252", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=[c[0] for c in COLUMNS])
        writer.writeheader()
        writer.writerows(records)
    monkeypatch.setenv("PHILGEPS_CSV", str(path))
    monkeypatch.setenv("PHILGEPS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("PHILGEPS_ENCODING", "cp1252")
    return path


def test_deduplication_missing_values_dates_and_exact_money(dataset):
    info = describe_dataset()
    assert info["coverage"]["raw_rows"] == 11
    assert info["coverage"]["notices"] == 7
    assert info["coverage"]["awards"] == 2
    assert info["coverage"]["notices_with_conflicting_budgets"] == 1
    assert info["parse_issues"] == {"published_date": 1}
    assert query_dataset("SELECT SUM(approved_budget_cents) FROM notices")["rows"] == [[36030]]
    assert query_dataset("SELECT SUM(contract_amount_cents) FROM awards")["rows"] == [[9005]]
    assert query_dataset("SELECT entity, published_date FROM records WHERE source_row=1")["rows"] == [["CITY OF BIÑAN", "2026-08-05"]]
    assert query_dataset("SELECT approved_budget_cents FROM notices WHERE bid_reference='300'")["rows"] == [[None]]
    assert query_dataset("SELECT COUNT(*) FROM records WHERE bid_reference IS NULL")["rows"] == [[3]]


def test_cache_reused_and_rebuilt_after_source_change(dataset):
    path = ensure_database()
    first = path.stat().st_mtime_ns
    assert ensure_database().stat().st_mtime_ns == first
    contents = dataset.read_text(encoding="cp1252").replace("200.20", "250.20")
    dataset.write_text(contents, encoding="cp1252")
    assert query_dataset("SELECT approved_budget_cents FROM notices WHERE bid_reference='200'")["rows"] == [[25020]]


@pytest.mark.parametrize("sql", [
    "DELETE FROM records", "DROP TABLE notices", "PRAGMA table_info(records)",
    "ATTACH DATABASE ':memory:' AS other", "SELECT * FROM metadata",
    "SELECT * FROM sqlite_master", "SELECT load_extension('anything')",
    "SELECT 1; SELECT 2", "UPDATE notices SET entity='changed' RETURNING entity",
    "SELECT randomblob(2000000)",
])
def test_query_restrictions(dataset, sql):
    with pytest.raises(ValueError):
        query_dataset(sql)
    assert query_dataset("SELECT COUNT(*) FROM records")["rows"] == [[11]]


def test_empty_results_ctes_and_truncation(dataset):
    assert query_dataset("SELECT * FROM notices WHERE region='NO SUCH REGION'")["rows"] == []
    result = query_dataset("WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM nums WHERE n<35) SELECT n FROM nums ORDER BY n")
    assert result["returned_rows"] == 30
    assert result["truncated"] is True
    assert query_dataset("SELECT printf('%2000s', 'x') AS text")["cells_truncated"] is True


def test_conflicting_metadata_is_exposed(dataset):
    content = dataset.read_text(encoding="cp1252").replace("Region IV-A", "NCR", 1)
    dataset.write_text(content, encoding="cp1252")
    assert query_dataset("SELECT metadata_variant_count FROM notices WHERE bid_reference='100'")["rows"] == [[2]]


def test_expensive_query_is_interrupted(dataset, monkeypatch):
    ensure_database()
    ticks = iter([0, 6])
    monkeypatch.setattr("philgeps.time.monotonic", lambda: next(ticks, 6))
    with pytest.raises(ValueError, match="interrupted"):
        query_dataset("WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM nums WHERE n<1000000) SELECT SUM(n) FROM nums")


class ScriptedModel:
    """Tests loop plumbing, not a live model's analysis quality."""
    def __init__(self, steps):
        self.steps = iter(steps)
        self.responses = self
        self.requests = []

    async def create(self, **request):
        self.requests.append({**request, "input": list(request["input"])})
        return next(self.steps)


def call(name, args, call_id="call-1"):
    return SimpleNamespace(output=[SimpleNamespace(type="function_call", name=name,
        arguments=args, call_id=call_id)], output_text="")


def answer(text):
    return SimpleNamespace(output=[], output_text=text)


def test_agent_passes_question_and_actual_results_to_model(dataset):
    model = ScriptedModel([
        call("describe_dataset", "{}"),
        call("query_dataset", json.dumps({"sql": "SELECT COUNT(*) FROM notices"}), "call-2"),
        answer("Seven notices"),
    ])
    assert asyncio.run(investigate("How many notices?", model, model="test")) == "Seven notices"
    assert model.requests[0]["input"][0]["content"] == "How many notices?"
    output = model.requests[-1]["input"][-1]
    assert output["call_id"] == "call-2"
    assert json.loads(output["output"])["rows"] == [[7]]


@pytest.mark.parametrize("name,args", [
    ("unknown", "{}"), ("query_dataset", "broken JSON"),
    ("describe_dataset", "[]"), ("query_dataset", '{"sql":42}'),
    ("describe_dataset", '{"extra":"x"}'), ("query_dataset", '{}'),
])
def test_invalid_tool_calls_return_errors(name, args):
    model = ScriptedModel([call(name, args), answer("Insufficient evidence")])
    asyncio.run(investigate("Analyze", model, model="test"))
    assert json.loads(model.requests[-1]["input"][-1]["output"])["status"] == "tool_error"


def test_call_limit_stops_execution():
    model = ScriptedModel([call("describe_dataset", "{}")]*2)
    executions = []

    async def execute(name, args):
        executions.append(name)
        return {}

    with pytest.raises(InvestigationError, match="limit"):
        asyncio.run(investigate("Analyze", model, model="test", max_calls=1, execute=execute))
    assert executions == ["describe_dataset"]
    assert model.requests[-1]["tool_choice"] == "none"


def test_unexpected_tool_errors_hide_details():
    async def broken(name, args):
        raise RuntimeError("secret credential")
    model = ScriptedModel([call("describe_dataset", "{}"), answer("Failed")])
    asyncio.run(investigate("Analyze", model, model="test", execute=broken))
    assert "secret" not in model.requests[-1]["input"][-1]["output"]


def test_real_mcp_process_discovery_queries_and_agent_loop(dataset):
    async def check():
        async with Client(server_parameters(), read_timeout_seconds=30) as session:
            tools = await discover_tools(session)
            assert {t["name"] for t in tools} == {t["name"] for t in TOOLS}
            assert all(t["parameters"]["required"] == list(t["parameters"]["properties"]) for t in tools)
            assert await execute_mcp(session, "describe_dataset", {}) == describe_dataset()
            sql = "SELECT COUNT(*), SUM(approved_budget_cents) FROM notices"
            assert await execute_mcp(session, "query_dataset", {"sql": sql}) == query_dataset(sql)
            assert (await session.call_tool("query_dataset", {"sql": "DELETE FROM records"})).is_error
            error = await execute_mcp(session, "query_dataset", {"sql": "SELECT nonexistent FROM notices"})
            assert error["status"] == "tool_error"
            assert "no such column" in error["error"]
            model = ScriptedModel([call("describe_dataset", "{}"),
                call("query_dataset", json.dumps({"sql": sql}), "call-2"), answer("MCP analysis")])

            async def execute(name, args):
                return await execute_mcp(session, name, args)

            assert await investigate("Analyze", model, model="test", tools=tools, execute=execute) == "MCP analysis"
            assert json.loads(model.requests[-1]["input"][-1]["output"])["rows"] == [[7, 36030]]
    asyncio.run(check())


@pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="Opt-in billable live API checks")
@pytest.mark.parametrize("transport", ["direct", "mcp"])
def test_live_model_count(dataset, transport):
    async def run():
        async with make_client() as client:
            options = {"model": os.getenv("OPENAI_MODEL", "gpt-6.2-astra")}
            question = "How many raw line-item rows are in this dataset? Write the number as digits."
            if transport == "direct":
                return await investigate(question, client, **options)
            async with Client(server_parameters(), read_timeout_seconds=30) as session:
                async def execute(name, args):
                    return await execute_mcp(session, name, args)
                return await investigate(question, client, **options, tools=await discover_tools(session), execute=execute)
    assert "11" in asyncio.run(run())
