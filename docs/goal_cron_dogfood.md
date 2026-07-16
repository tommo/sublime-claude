# Goal + cron dogfood pattern

**Goal** = host-owned finish line (`/goal` in sublime-claude).  
**Cron / `/loop` / `scheduler_create`** = optional wall-clock recheck after turns end.

## Native goal mode (plugin harness)

sublime-claude owns the harness for **all backends** (Claude + Grok):

| Piece | Owner |
|-------|--------|
| `/goal <objective> [--budget N]` | Plugin (never forwarded as agent slash) |
| `update_goal` MCP | Plugin drain → `GoalTracker` |
| Continuation | Host `query` after successful turn while Active |
| Complete | Deferred → **verifier turn** → Achieved only |

Commands:

```text
/goal ship the widget --budget 200000
/goal status
/goal pause
/goal resume
/goal clear
```

Model tools:

- `update_goal(message="…")` — progress  
- `update_goal(completed=true, message="…")` — claim done (host re-checks)  
- `update_goal(blocked_reason="…")` — after real failures (3× → blocked pause)

Without an active `/goal`, complete/blocked are **rejected** (no sticky invent from tool alone).

## Why pair with cron (optional)

| Piece | Alone | Together |
|--------|--------|----------|
| Goal | Multi-turn until verified | Finish line across ticks |
| Cron | Periodic work without stop | Each fire audits the same goal |

Rule: **cron never invents a new objective** — only advances/completes the host goal.

### Recipe

1. `/goal <outcome> verified by <checks>`
2. Optional: `scheduler_create(interval="2m", …)` with a prompt that calls `update_goal` and deletes the schedule on green.
3. On verified complete, host may cancel looping banners.

Primary autonomy is the **host continue loop**, not cron.

## UI

Work strip: `◎ goal · active|paused|blocked|verifying|budget …`  
Status bar chip: `goal:active` / `goal:verifying` / …

## Anti-patterns

- Completing because “looks fine” without evidence (verifier should catch)
- Forwarding raw `/goal` to Grok (double harness) — plugin intercepts
- Using `update_goal` in Quick Agent (use `quick_done`)
- Leaving a recurring schedule after complete
