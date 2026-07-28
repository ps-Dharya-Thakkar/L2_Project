"""
MCP CLIENT — connects our agent process to the mcp_server.py tool box.

It starts mcp_server.py as a subprocess, talks to it over stdio using the
MCP protocol, and gives us two simple methods:

    list_tools_for_ollama()   -> tool schemas in the format Ollama expects
    call_tool(name, args)     -> actually invoke a tool and get the result

This is the piece that makes "MCP" real in the project rather than just a
buzzword — the agent never calls requests.get() directly, it always goes
through this MCP session.
"""

from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPToolClient:
    def __init__(self, server_script: str = "mcp_server.py"):
        self.server_script = server_script
        self.session: ClientSession | None = None
        self._stack = AsyncExitStack()

    async def connect(self):
        params = StdioServerParameters(command="python", args=[self.server_script])
        stdio_transport = await self._stack.enter_async_context(stdio_client(params))
        read, write = stdio_transport
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()

    async def list_tools_for_ollama(self) -> list[dict]:
        """Convert MCP tool definitions into Ollama's function-calling schema."""
        resp = await self.session.list_tools()
        tools = []
        for t in resp.tools:
            tools.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.inputSchema,
                },
            })
        return tools

    async def call_tool(self, name: str, arguments: dict) -> str:
        result = await self.session.call_tool(name, arguments)
        # MCP returns a list of content blocks; join their text parts
        return "\n".join(c.text for c in result.content if hasattr(c, "text"))

    async def close(self):
        await self._stack.aclose()
