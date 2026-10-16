"""MCP server exposing the Lineage tool catalogue (stdio by default, or streamable HTTP with --http).

  uv run python -m lineage.mcp_server          # for Claude Desktop / Claude Code / Cursor
"""
import sys

from mcp.server.mcpserver import MCPServer

from lineage import tools

mcp = MCPServer("lineage", instructions=(
    "Graph of Python's PEPs, people, decisions and CPython PRs, stored in FalkorDB. "
    "Call resolve() first when a PEP number is unknown. Every result includes the graph paths "
    "behind it; an outcome of not_in_graph means the graph doesn't record it, not that it's false."))


@mcp.tool()
def resolve(text: str) -> dict:
    """Find PEPs, people or topics matching free text, e.g. 'walrus operator'."""
    return tools.call("resolve", {"text": text})


@mcp.tool()
def why(pep: str, depth: int = 2) -> dict:
    """Decision lineage of a PEP: authors, ruling and who made it, earlier PEPs it builds on
    (depth 1-4), implementing CPython PRs, and anything that superseded it."""
    return tools.call("why", {"pep": pep, "depth": depth})


@mcp.tool()
def is_current(pep: str) -> dict:
    """Whether a PEP still describes Python, following the SUPERSEDES chain."""
    return tools.call("is_current", {"pep": pep})


@mcp.tool()
def who_knows(subject: str, limit: int = 5) -> dict:
    """People who know a topic (typing, packaging, governance, release) or a PEP's area."""
    return tools.call("who_knows", {"subject": subject, "limit": limit})


@mcp.tool()
def bus_factor(subject: str) -> dict:
    """Knowledge concentration in a topic or PEP area, and what is orphaned if the top person left."""
    return tools.call("bus_factor", {"subject": subject})


@mcp.tool()
def connection(a: str, b: str) -> dict:
    """Shortest weighted path between two PEPs and/or people (FalkorDB algo.SPpaths)."""
    return tools.call("connection", {"a": a, "b": b})


@mcp.tool()
def recall(pep: str) -> dict:
    """Findings saved about a PEP in earlier sessions."""
    return tools.call("recall", {"pep": pep})


@mcp.tool()
def remember(pep: str, text: str, session: str = "mcp") -> dict:
    """Save a finding about a PEP for future sessions."""
    return tools.call("remember", {"pep": pep, "text": text, "session": session})


if __name__ == "__main__":
    mcp.run("streamable-http" if "--http" in sys.argv else "stdio")
