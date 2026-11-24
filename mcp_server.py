"""Local stdio MCP interface; stdout is reserved for protocol messages."""

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from typing import Any
import philgeps

mcp = MCPServer("PhilGEPS procurement explorer")
mcp.tool()(philgeps.describe_dataset)


def query_dataset(sql: str) -> dict[str, Any]:
    try:
        return philgeps.query_dataset(sql)
    except ValueError as exc:
        # Expected SQL errors are useful feedback for the model to correct.
        raise ToolError(str(exc)) from exc


mcp.tool(description=philgeps.query_dataset.__doc__)(query_dataset)

if __name__ == "__main__":
    mcp.run()
