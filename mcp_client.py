"""A separate client discovers tools over stdio and reuses the same agent loop."""

import asyncio
import os
from pathlib import Path
import sys

from mcp import Client, StdioServerParameters

from agent import (InvestigationError, argument_parser, investigate,
                   make_client, trace_call, format_cli_error)


def server_parameters():
    return StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).with_name("mcp_server.py"))],
        env={key: os.environ[key] for key in
             ("PHILGEPS_CSV", "PHILGEPS_ENCODING", "PHILGEPS_CACHE_DIR") if key in os.environ},
    )


async def discover_tools(session):
    tools = []
    cursor = None
    while True:
        page = await session.list_tools(cursor=cursor)
        for tool in page.tools:
            schema = dict(tool.input_schema)
            schema["additionalProperties"] = False
            schema.setdefault("required", [])
            tools.append({"type": "function", "name": tool.name,
                          "description": tool.description or tool.name,
                          "parameters": schema, "strict": True})
        cursor = page.next_cursor
        if cursor is None:
            return tools


async def execute_mcp(session, name, arguments):
    result = await session.call_tool(name, arguments)
    if result.is_error:
        details = "; ".join(block.text for block in result.content if block.type == "text")
        return {"status": "tool_error", "error": details or "MCP tool execution failed"}
    if result.structured_content is None:
        raise InvestigationError("MCP tool returned no structured data")
    return result.structured_content


async def main():
    parser = argument_parser("Analyze the PhilGEPS CSV using discovered MCP tools")
    args = parser.parse_args()
    async with Client(server_parameters(), read_timeout_seconds=120) as session:
        tools = await discover_tools(session)
        print("[MCP discovered] " + ", ".join(t["name"] for t in tools), file=sys.stderr)

        async def execute(name, arguments):
            return await execute_mcp(session, name, arguments)

        async with make_client() as client:
            print(await investigate(args.question, client, model=args.model, tools=tools,
                                    execute=execute, max_calls=args.max_calls, trace=trace_call))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"Investigation failed: {format_cli_error(exc)}", file=sys.stderr)
        sys.exit(1)
