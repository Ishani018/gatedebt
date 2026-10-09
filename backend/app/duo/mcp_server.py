"""GateDebt MCP server (stdio) for GitLab Duo Agentic Chat / Duo CLI.

    python -m app.duo.mcp_server [--enable-stateful-tools]

Built on the official MCP Python SDK (``mcp.server.lowlevel.Server``). It is a
LOCAL server started by the client; it is not a GitLab-hosted agent or flow
and holds no GitLab account identity of its own. All calls act as
``agent:duo-mcp`` (unverified) against the local GateDebt database.

Read-only tools are always listed. ``run_rehearsal`` and ``ingest_ci_evidence``
are listed only with ``--enable-stateful-tools`` and are marked non-read-only
so clients ask a human to confirm each call.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from pydantic import ValidationError

from app.config import ConfigError, Settings
from app.services.lifecycle import DomainError, Lifecycle
from app.store import Store

from .tools import AGENT, ToolSpec, tool_specs

log = logging.getLogger("gatedebt.mcp")

INSTRUCTIONS = (
    "GateDebt tracks temporary engineering exceptions (waivers) and decides, with a deterministic policy "
    "engine, whether they can be retired. Use these tools to read exceptions, evidence and decisions, and "
    "to explain them. Report GateDebt's recommendation and reason codes as given; never claim a check "
    "passed unless GateDebt's evidence says so. You cannot approve, propose, renew, or change status; "
    "tell the user which human steps remain."
)


class ToolRefused(Exception):
    """A refusal whose message is safe to show the client."""


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace local ``$ref``s with their definitions; some MCP clients do not resolve them."""
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(defs[node["$ref"].rsplit("/", 1)[-1]])
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


def _tool_definition(spec: ToolSpec) -> types.Tool:
    return types.Tool(
        name=spec.name,
        description=spec.description,
        inputSchema=_inline_refs(spec.args.model_json_schema()),
        annotations=types.ToolAnnotations(
            readOnlyHint=spec.read_only,
            destructiveHint=False,
            idempotentHint=spec.read_only,
            # Only ingest_ci_evidence reaches outside (GitLab API, via the server's own client).
            openWorldHint=spec.name == "ingest_ci_evidence",
        ),
    )


def _audit(lifecycle: Lifecycle, spec: ToolSpec, exception_id: str | None, outcome: str) -> None:
    store = lifecycle.store
    with store.transaction() as conn:
        if exception_id and store.get_exception(conn, exception_id) is None:
            exception_id = None
        store.append_audit(conn, AGENT.actor, "agent.tool_called", exception_id,
                           {"tool": spec.name, "outcome": outcome})


def execute(lifecycle: Lifecycle, specs: dict[str, ToolSpec], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate, run and audit one tool call. Raises ToolRefused with a safe message."""
    spec = specs.get(name)
    if spec is None:
        raise ToolRefused(f"UNKNOWN_TOOL: {name!r} is not a GateDebt tool")
    try:
        args = spec.args.model_validate(arguments or {})
    except ValidationError as err:
        fields = sorted({".".join(str(p) for p in e["loc"]) or "arguments" for e in err.errors()})
        raise ToolRefused(f"INVALID_ARGUMENTS: {', '.join(fields)}") from None
    exception_id = getattr(args, "exception_id", None)
    try:
        result = spec.handler(lifecycle, args)
    except DomainError as err:
        _audit(lifecycle, spec, exception_id, "refused")
        raise ToolRefused(f"REFUSED_BY_GATEDEBT ({err.status_code}): {', '.join(err.reason_codes)}") from None
    except Exception:  # noqa: BLE001 - never leak internals to the model
        log.exception("tool %s failed", name)
        _audit(lifecycle, spec, exception_id, "error")
        raise ToolRefused("INTERNAL_ERROR: see GateDebt server log") from None
    _audit(lifecycle, spec, exception_id, "ok")
    return result


def build_server(lifecycle: Lifecycle, enable_stateful: bool = False) -> Server:
    specs = {s.name: s for s in tool_specs(enable_stateful)}
    server: Server = Server("gatedebt", instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [_tool_definition(s) for s in specs.values()]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # Lifecycle work is synchronous (SQLite, subprocesses); keep the event loop free.
        return await anyio.to_thread.run_sync(execute, lifecycle, specs, name, arguments)

    return server


async def _serve(server: Server) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.duo.mcp_server")
    parser.add_argument("--enable-stateful-tools", action="store_true",
                        help="also expose run_rehearsal and ingest_ci_evidence")
    args = parser.parse_args(argv)
    # stdout carries the MCP protocol; logs go to stderr only.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        settings = Settings.from_env()
    except ConfigError as err:
        print(f"configuration error: {err}", file=sys.stderr)
        return 2
    lifecycle = Lifecycle(Store(settings.db_path), settings)
    anyio.run(_serve, build_server(lifecycle, args.enable_stateful_tools))
    return 0


if __name__ == "__main__":
    sys.exit(main())
