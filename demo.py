"""A repeatable, no-key presentation of real tool results; no model is simulated."""

import argparse
import asyncio
import sys

from philgeps import describe_dataset, query_dataset


QUERIES = [
    ("Top regions by approved notice budget (PHP)", """SELECT region,
       COUNT(*) AS notices, ROUND(SUM(approved_budget_cents)/100.0, 2) AS budget_php
       FROM notices WHERE metadata_variant_count=1 AND approved_budget_cents IS NOT NULL
       GROUP BY region ORDER BY SUM(approved_budget_cents) DESC, region LIMIT 5"""),
    ("Largest individual notice budgets (PHP)", """SELECT source_row, bid_reference,
       entity, title, approved_budget_cents/100.0 AS budget_php
       FROM notices WHERE metadata_variant_count=1 AND approved_budget_cents IS NOT NULL
       ORDER BY approved_budget_cents DESC, source_row LIMIT 5"""),
    ("Top suppliers in the populated award subset (PHP)", """SELECT awardee,
       COUNT(*) AS awards, ROUND(SUM(contract_amount_cents)/100.0, 2) AS awarded_php
       FROM awards WHERE awardee_variant_count=1 AND awardee IS NOT NULL
       AND contract_amount_cents IS NOT NULL GROUP BY awardee
       ORDER BY SUM(contract_amount_cents) DESC, awardee LIMIT 5"""),
    ("How much does the largest notice influence its region?", """WITH largest AS (
       SELECT * FROM notices WHERE metadata_variant_count=1 AND approved_budget_cents IS NOT NULL
       ORDER BY approved_budget_cents DESC, source_row LIMIT 1)
       SELECT l.source_row, l.bid_reference, l.region, l.approved_budget_cents/100.0 AS notice_budget_php,
       ROUND(100.0*l.approved_budget_cents/SUM(n.approved_budget_cents), 2) AS share_of_region_percent
       FROM largest l JOIN notices n ON n.region=l.region
       WHERE n.metadata_variant_count=1 AND n.approved_budget_cents IS NOT NULL
       GROUP BY l.notice_key"""),
    ("Source evidence for the largest notice (different budget fields)", """SELECT
       r.source_row, r.bid_reference, r.title, r.approved_budget_cents/100.0 AS notice_budget_php,
       r.item_budget_cents/100.0 AS item_budget_php
       FROM records r JOIN (SELECT notice_key FROM notices
       WHERE metadata_variant_count=1 AND approved_budget_cents IS NOT NULL
       ORDER BY approved_budget_cents DESC, source_row LIMIT 1) biggest
       ON r.notice_key=biggest.notice_key ORDER BY r.source_row LIMIT 5"""),
    ("Notice exclusions from budget rankings", """SELECT
       SUM(metadata_variant_count>1) AS conflicting_metadata,
       SUM(approved_budget_cents IS NULL) AS unavailable_or_conflicting_budget,
       SUM(metadata_variant_count>1 OR approved_budget_cents IS NULL) AS excluded_notices
       FROM notices"""),
]


def show(title, result):
    print(f"\n{title}\n{'=' * len(title)}")
    print("SQL: " + " ".join(result["sql"].split()))
    for row in result["rows"]:
        print("; ".join(f"{column}: {value:,.2f}" if isinstance(value, float)
                        else f"{column}: {value}" for column, value in zip(result["columns"], row)))


async def run(execute):
    info = await execute("describe_dataset", {})
    coverage = info["coverage"]
    print("PhilGEPS procurement explorer (fixed queries, no model or API key)")
    print(f"Published: {coverage['first_published']} to {coverage['last_published']}")
    print(f"{coverage['raw_rows']:,} line-item rows; {coverage['notices']:,} analytical notices; "
          f"{coverage['awards']:,} recorded awards")
    print(f"Only {coverage['rows_with_contract_amount']:,} rows contain contract amounts. "
          "Approved budgets are not actual spending.")
    for title, sql in QUERIES:
        show(title, await execute("query_dataset", {"sql": sql}))
    print("\nNotices use deduplicated references/fallback keys. Rankings exclude conflicting metadata "
          "and unavailable budgets. Supplier rankings cover only populated, unambiguous awards.")


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp", action="store_true", help="Run the same demo through a separate MCP server")
    args = parser.parse_args()
    if args.mcp:
        from mcp import Client
        from mcp_client import discover_tools, execute_mcp, server_parameters
        async with Client(server_parameters(), read_timeout_seconds=120) as session:
            print("MCP discovered: " + ", ".join(t["name"] for t in await discover_tools(session)))

            async def execute(name, arguments):
                return await execute_mcp(session, name, arguments)

            await run(execute)
    else:
        async def execute(name, arguments):
            return {"describe_dataset": describe_dataset, "query_dataset": query_dataset}[name](**arguments)
        await run(execute)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
