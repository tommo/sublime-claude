# Goal + cron dogfood pattern

**Goal** = host-owned finish line (`/goal` in sublime-claude).  
**Cron / `/loop` / `scheduler_create`** = optional wall-clock recheck after turns end.

## Native goal mode (plugin harness)

sublime-claude owns the harness for **all backends** (Claude + Grok):

| Piece | Owner |
|-------|--------|
| `/goal <objective> [--budget N]` | Plugin (never forwarded as agent slash) |
| **Planning** | Host expands a structured plan **before** execute |
| Plan path | `{project}/.claude/goals/{goal_id}/plan.md` (plugin-owned) |
| `update_goal` MCP | Plugin drain → `GoalTracker` |
| Continuation | Host `query` while `phase=executing` and plan ready |
| Complete | Deferred → **plan-grounded verifier** → Achieved only |

### Lifecycle

1. **planning** — `/goal` creates the tracker; host materializes plan (Acceptance criteria + Verification plan, Grok-compatible sections). Continue loop is **off**.
2. **executing** — plan accepted; implementer kickoff + host continues until claim/pause.
3. **verifying** — `update_goal(completed=true)` → host verifier judges against the **plan**, not claim prose alone.
4. **complete / clear** — plan body cleared so a stale plan is not treated as active.

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
- `update_goal(completed=true, message="…")` — claim done (host re-checks vs plan)  
- `update_goal(blocked_reason="…")` — after real failures (3× → blocked pause)

Without an active `/goal`, complete/blocked are **rejected** (no sticky invent from tool alone). Complete is also rejected while still planning / without a plan.

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
