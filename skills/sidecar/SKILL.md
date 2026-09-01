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

SUBLIME SIDECAR is MCP `spawn_session`:

1. `spawn_session(prompt=…, name=…, backend=…, model=…)` → `agent_id`
2. Steer with `send_to_session(agent_id=…)`
3. Child finishes with `signal_complete` only — do not mail the parent
4. You stay orchestrator

Do **not** `grok -p` / `kimi -p` / `codex exec` for a bare "sidecar".
"grok sublime sidecar" → `spawn_session(backend="grok")`.
"kimi sidecar" without sublime → delegate skill (CLI).
