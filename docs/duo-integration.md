# GitLab Duo Agent Platform integration (proposal)

Status: **design only — nothing below is implemented or connected yet.**
Every capability listed was checked against GitLab's own documentation sources
(`gitlab-org/gitlab` `doc/user/duo_agent_platform/**`,
`doc/user/gitlab_duo/model_context_protocol/**`, and the flow registry v1 spec
in `gitlab-org/modelops/applied-ml/code-suggestions/ai-assist`), read on
2026-10-09. No account-level access could be checked from the development
environment (no GitLab credentials there), so each item marked *verify* must
be confirmed in the GitLab UI of the approved account.

## What the Duo Agent Platform actually offers (relevant parts)

| Capability | Status in docs | Usable for GateDebt? |
|---|---|---|
| **MCP clients**: Duo Agentic Chat (VS Code, JetBrains, Duo CLI) connects to MCP servers listed in `.gitlab/duo/mcp.json` (`stdio`, `http`, `sse`), per-tool approval via `approvedTools` | GA (18.8); group setting *Allow external MCP tools* | **Yes — primary path.** GateDebt ships a constrained stdio MCP server. |
| **Custom agents** (UI): display name, system prompt, curated tool list; usable in Chat (web UI, VS Code, JetBrains) | GA | **Yes** — "GateDebt Investigator" agent with a fixed system prompt. *Verify* that a custom agent selected in IDE chat can call the workspace MCP tools. |
| **MCP servers in the AI Catalog** (attach remote MCP servers to custom agents) | Experiment; on GitLab.com only centrally vetted partner servers, *arbitrary URLs are not allowed* | **No** for GateDebt's own server on GitLab.com. |
| **Custom flows** (flow registry v1 YAML: `AgentComponent`, `DeterministicStepComponent`, `HumanInputComponent`, tool options, `require_tool_approval`) | GA (19.2), `environment: ambient` only | **Yes — second path**, read-only over GitLab data. |
| **Triggers** (Mention, Assign, Assign reviewer, Pipeline events: Running/Passed/Failed/Canceled, Merge request, Work item) | GA; flows/external agents only; must be caused by a human action; runs as a service account (composite identity) | **Yes** — Pipeline *Passed*/*Failed* or Mention starts the flow. |
| **Built-in tools** incl. `gitlab_api_get` (GET under `/api/v4/` only), `get_job_logs`, `create_issue_note`/`create_merge_request_note` (`internal` flag), `run_command` | Documented | Read tools + one pinned note tool. **`run_command` is never used** (arbitrary commands). |
| **External agents** (Claude Code / Codex in CI via `agent-config.yml`) | Requires GitLab support to enable on GitLab.com | Not needed. |

A flow running on GitLab **cannot reach the GateDebt server** without
`run_command` (forbidden) or a vetted remote MCP server (not possible on
GitLab.com). So the two paths have different reach:

* **Path A (IDE/CLI, MCP)** reads GateDebt's real exception, evidence and
  decision interfaces.
* **Path B (flow, triggers)** reads only what GitLab holds: the pipeline's
  self-reported rehearsal summary artifact. It must label that data as
  unverified and point to GateDebt for the authoritative decision.

## Proposed architecture

```
 Developer IDE / Duo CLI                           GitLab.com
 ┌───────────────────────────────┐                ┌────────────────────────────────────┐
 │ Duo Agentic Chat              │                │ Pipeline (validate→test→rehearse→  │
 │  + custom agent               │                │   evaluate) ── artifacts           │
 │    "GateDebt Investigator"    │                │        │ Pipeline Passed/Failed     │
 │        │ MCP (stdio)          │                │        ▼ trigger (human push)       │
 │        ▼                      │                │ Custom flow "gatedebt-pipeline-     │
 │ gatedebt-mcp (this repo)      │                │  explainer" (ambient)              │
 │  actor = agent:duo-mcp        │                │  1 DeterministicStep gitlab_api_get │
 │  read: list/get exception,    │                │    (endpoint pinned) → summary.json │
 │        evidence, decision     │                │  2 AgentComponent: explain, no      │
 │  bounded: run_rehearsal(enum) │                │    decisions; toolset: read tools + │
 │           ingest_ci_evidence  │                │    create_merge_request_note        │
 │  explain: retirement_brief    │                │    (internal: true pinned)          │
 │        │                      │                └────────────────────────────────────┘
 │        ▼                      │
 │ GateDebt lifecycle + policy   │  ◀── authoritative; humans approve via API only
 └───────────────────────────────┘
```

### Path A — `gatedebt-mcp` (covers requirements 1–3)

A stdio MCP server inside `backend/app/duo/`, started by Duo from
`.gitlab/duo/mcp.json`. It calls the existing `Lifecycle` in-process with a
fixed identity `agent:duo-mcp` (verified=False). Tools:

| Tool | Effect | Guard |
|---|---|---|
| `list_exceptions(status?)` | read | – |
| `get_exception(exception_id)` | read + expiry state | ID pattern |
| `get_evidence(exception_id)` | read, incl. provenance/CI verification | – |
| `get_decision(exception_id, commit?)` | read-only `evaluate()` | same code as `GET /decision` |
| `run_rehearsal(exception_id, scenario_id)` | runs one **approved** scenario locally | `scenario_id` is a JSON-schema enum of the registry; dev only (403 in production); evidence binds to local HEAD, dirty tree excluded |
| `ingest_ci_evidence(exception_id, pipeline_id, scenario_id)` | server-verified ingestion | existing `verify_ci_evidence`; pointers only |
| `retirement_brief(exception_id, commit?)` | returns the deterministic decision plus a structured, template-generated explanation (reason codes → plain language, evidence IDs, failed/missing checks, required human steps) | never changes state |

Deliberately **absent**: propose, approve, verify, renew, create/edit
exceptions, any command or path input. Even if an LLM asked, the server-side
lifecycle already refuses `agent:*` approvals (`APPROVER_MUST_BE_HUMAN`).

`mcp.json` pre-approves only the read tools; `run_rehearsal` and
`ingest_ci_evidence` keep Duo's per-call human approval prompt.

Untrusted agent output: anything the agent writes back is treated as text.
`retirement_brief` is generated by GateDebt, not the LLM; the agent may
paraphrase it in chat, but nothing it says is read back as data or evidence.
Every MCP call is audited (`agent.tool_called`, tool name, exception ID,
outcome — no arguments beyond IDs, no secrets).

Dependency: the official MCP Python SDK (`mcp`) — needed to speak the
protocol correctly; tests drive the server through the SDK's own client.

### Path B — custom flow on pipeline events (optional, after A works)

`.gitlab/duo/flows/gatedebt-pipeline-explainer.yaml` (flow registry v1):

* `DeterministicStepComponent` with `tool_name: gitlab_api_get` and a pinned
  `endpoint` (latest `evidence-summary` artifact on `main`) — no LLM picks the
  URL.
* `AgentComponent` with a local prompt: explain the self-reported summary,
  state it is **not** GateDebt's decision, list next human steps. Toolset:
  `get_job_logs` and `create_merge_request_note` / `create_issue_note` with
  `internal: true` pinned via tool options. No `run_command`, no write tools
  beyond one note.
* Trigger: **Pipeline events → Passed, Failed**, service account as composite
  identity.

*Verify* before building B: that `gitlab_api_get` returns raw artifact JSON,
and whether the flow can be loaded from a configuration path (needs the
`ai_catalog_create_third_party_flows` flag) or must be created in the AI Catalog.

## What stays out of reach of any agent

* Evidence creation (only the harness and server-verified CI ingestion).
* CI provenance (only `verify_ci_evidence` with the server's token).
* Lifecycle state: propose/approve/verify/renew are human or policy only.
* Arbitrary commands, scenario paths, production rehearsals.

## Account setup checklist (manual)

1. GitLab Duo on for the top-level group; Agent Platform on
   (Settings → GitLab Duo).
2. For Path A: *Allow external MCP tools* in the group's GitLab Duo settings;
   GitLab for VS Code ≥ 6.35.6, JetBrains plugin ≥ 3.14.0 or Duo CLI ≥ 8.81.0.
3. Project must live in a **group** namespace with Developer+ role.
4. For Path B: Premium/Ultimate for triggers; a service account for the trigger.
