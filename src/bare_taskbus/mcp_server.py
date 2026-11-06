"""TaskbusMCPServer - expose the task bus as Model Context Protocol tools.

Four tools, deliberately narrow:
    list_available_tasks(status)      -> compact rows
    claim_task(row_id)                -> the claim, with the criterion spelled out
    create_subtask(...)               -> row id
    submit_for_review(row_id, ...)    -> the review result

Two rules that are about safety rather than convenience:
  * no tool takes an API key argument. The server holds the key from the environment, so a prompt
    cannot exfiltrate it by calling a tool.
  * submit_for_review is only registered when the configured key is a dispatcher, because a tool that
    always answers 403 teaches the model to distrust the whole tool set.

Requires the optional dependency: pip install bare-taskbus[mcp]
"""
import json
import logging
import os
from typing import Optional

from .client import TaskbusClient
from .errors import TaskbusError

log = logging.getLogger("bare_taskbus.mcp")

TOOL_NAMES = ("list_available_tasks", "claim_task", "create_subtask", "submit_for_review")


def _compact(row):
    return {"id": row.id, "status": row.status, "priority": row.priority,
            "title": row.short_description, "criterion": row.success_criterion}


class TaskbusMCPServer:
    def __init__(self, client: Optional[TaskbusClient] = None, is_dispatcher=None,
                 server_name="bare-taskbus"):
        self.client = client or TaskbusClient()
        self.server_name = server_name
        if is_dispatcher is None:
            is_dispatcher = str(os.environ.get("TASKBUS_IS_DISPATCHER", "")).lower() in \
                ("1", "true", "yes")
        self.is_dispatcher = bool(is_dispatcher)

    # ---- tools -----------------------------------------------------------
    def list_available_tasks(self, status="new"):
        rows = self.client.list_tasks(status=status)
        return json.dumps([_compact(r) for r in rows])

    def claim_task(self, row_id):
        claim = self.client.claim_task(int(row_id))
        row = self.client.get_task(int(row_id))
        return json.dumps({"row_id": claim.row_id, "claimed": True,
                           "lease_expires_at": claim.lease_expires_at,
                           "title": row.short_description,
                           "criterion": row.success_criterion,
                           "note": "the nonce is held by the server; call submit_for_review "
                                   "or a block tool to use it"})

    def create_subtask(self, short_description, ai_prompt, success_criterion=None,
                       priority="medium", assigned_rep_id=None, depends_on=None):
        row = self.client.create_task(short_description, ai_prompt,
                                      success_criterion=success_criterion, priority=priority,
                                      assigned_rep_id=assigned_rep_id, depends_on=depends_on)
        return json.dumps({"id": row.id, "status": row.status})

    def submit_for_review(self, row_id, verdict, status, comments, verifier_rep_uuid=None):
        result = self.client.review_task(int(row_id), verdict, status, comments,
                                         verifier_rep_uuid=verifier_rep_uuid)
        return json.dumps(result)

    # ---- registration ----------------------------------------------------
    def _registered_tools(self):
        tools = ["list_available_tasks", "claim_task", "create_subtask"]
        if self.is_dispatcher:
            tools.append("submit_for_review")
        return tools

    def serve(self):
        try:
            from mcp.server import Server        # noqa: F401
            import mcp.server.stdio as stdio    # noqa: F401
            from mcp.types import Tool, TextContent
        except ImportError:
            raise SystemExit(
                "the mcp package is not installed. Install it with: pip install bare-taskbus[mcp]")
        import asyncio

        app = Server(self.server_name)

        @app.list_tools()
        async def list_tools():
            catalogue = {
                "list_available_tasks": {"status": {"type": "string", "default": "new"}},
                "claim_task": {"row_id": {"type": "integer"}},
                "create_subtask": {"short_description": {"type": "string"},
                                   "ai_prompt": {"type": "string"},
                                   "success_criterion": {"type": "string"},
                                   "priority": {"type": "string", "default": "medium"}},
                "submit_for_review": {"row_id": {"type": "integer"},
                                      "verdict": {"type": "string", "enum": ["Passed", "Failed"]},
                                      "status": {"type": "string",
                                                 "enum": ["Completed", "Blocked"]},
                                      "comments": {"type": "string"}},
            }
            return [Tool(name=n, description="Task Bus: " + n,
                         inputSchema={"type": "object", "properties": catalogue[n]})
                    for n in self._registered_tools()]

        @app.call_tool()
        async def call_tool(name, arguments):
            if name not in self._registered_tools():
                return [TextContent(type="text", text="tool not available: " + str(name))]
            try:
                fn = getattr(self, name)
                return [TextContent(type="text", text=fn(**(arguments or {})))]
            except TaskbusError as exc:
                return [TextContent(type="text",
                                    text="taskbus error (%s): %s" % (exc.code, exc))]

        async def _run():
            async with stdio.stdio_server() as (read, write):
                await app.run(read, write, app.create_initialization_options())

        asyncio.run(_run())


def main():
    logging.basicConfig(level=logging.INFO)
    TaskbusMCPServer().serve()
