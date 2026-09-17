---
name: sidecar
description: >
  Unqualified "sidecar" in a Sublime session means SUBLIME SIDECAR:
  MCP sublime spawn_session (another sheet), not grok/kimi/codex CLI.
  Use when the user says sidecar, sublime sidecar, worker sheet,
  spawn a session, or fork a worker in the editor.
---

# Sublime sidecar (default)

In this Sublime-hosted session, **sidecar = SUBLIME SIDECAR** unless a CLI
driver is named (`kimi sidecar`, `codex sidecar`, `grok CLI` / `grok -p`).

SUBLIME SIDECAR is MCP `spawn_session` / `send_to_session`. You stay orchestrator.

## Reuse warm sheets

`list_sessions` first. If an idle or sleeping child already has the backend
**and model** you need, `send_to_session(agent_id=…)` — do **not** spawn another.
`spawn_session` only when none fit, or you need a different backend/model.

## Honor the user's model

If the user named a model (e.g. deepseek-v4-flash, kimi-code, grok-4.6),
pass it on spawn: `spawn_session(backend=…, model=<that id>)`.
`list_backends` if the id is unclear. Do **not** silently use the default
model. Reuse a warm child only when its model already matches.

## Project knowledge (required in the child)

The child prompt MUST tell the worker to use this project's knowledge before
inventing: `list_profile_docs` / `read_profile_doc`, irr (if indexed),
`get_symbols` / existing code. Do not guess APIs or file layout.

## Report done (required)

The child MUST finish with the Sublime MCP tool `signal_complete`
(`result_summary=…`) as its **own last tool step** after the final message —
not in parallel with other tools. That call IS the parent notification.

Do **not** `send_to_session` the parent with the same summary. Do **not** a
CLI complete, a fake wait-return, or any non-sublime "signal complete".

1. Reuse or `spawn_session(prompt=…, name=…, backend=…, model=…)` → `agent_id`
2. Steer with `send_to_session(agent_id=…)`
3. Child: project knowledge, then MCP `signal_complete` only

Do **not** `grok -p` / `kimi -p` / `codex exec` for a bare "sidecar".
"grok sublime sidecar" → `spawn_session(backend="grok")`.
"kimi sidecar" without sublime → delegate skill (CLI).
