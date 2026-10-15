"""Exercise a real stdio subprocess, persistence across restart and setup probe."""

from __future__ import annotations

import asyncio
import os
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from teachonce.cli import probe


def test_stdio_discovery_import_and_history_after_restart(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TEACHONCE_", "TEACHCONE_"))}
    env["TEACHONCE_HOME"] = str(tmp_path)
    env["PYTHONIOENCODING"] = "utf-8"
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "teachonce.mcp_server"], env=env
    )

    async def run():
        async with Client(params) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert "teachonce_review_version" in tools and "teachonce_learning_context" in tools
            result = await client.call_tool(
                "teachonce_import_demonstration",
                {
                    "task_goal": "Group notes",
                    "steps": [{"action": "Group by course"}],
                },
            )
            demo_id = result.structured_content["demonstration_id"]
        async with Client(params) as client:
            saved = (await client.call_tool("teachonce_list_demonstrations", {})).structured_content
            assert saved["count"] == 1 and saved["demonstrations"][0]["id"] == demo_id

    asyncio.run(asyncio.wait_for(run(), timeout=30))


def test_probe_uses_actual_server_without_model_calls():
    result = asyncio.run(asyncio.wait_for(probe(), timeout=30))
    assert result["connected"] and result["transport"] == "stdio"
    assert result["tool_count"] == 22
    assert result["llm_called"] is False
