# Goal + cron dogfood pattern

**Goal** = durable completion contract (what “done” means + evidence).  
**Cron / `/loop` / `scheduler_create`** = keep checking until evidence holds, without “keep going” spam.

## Why they pair

| Piece | Alone | Together |
|--------|--------|----------|
| Goal | Outcome can rot after one turn ends | Stays the finish line across ticks |
| Cron | Periodic work without a stop condition | Each fire audits the goal and stops when green |

Rule: **cron never invents a new objective** — it only advances or completes the one goal.

## Recipe (Grok Build)

1. Activate the goal (TUI/host):
   ```text
   /goal <outcome> verified by <commands/artifacts> while preserving <constraints>
   ```
2. Arm the loop (same session toolset):
   ```text
   scheduler_create(
     interval="2m",
     fire_immediately=true,
     recurring=true,
     prompt="""Active goal: <same objective>
     On fire: run checks → update_goal(message=…)
     If all green: update_goal(completed=true) + scheduler_delete(this id)
     If red: update_goal(message=fail detail); smallest fix only if trivial
     """
   )
   ```
3. UI (ClaudeCode Work strip): sticky `◎ goal · active …` + task steps; purple vs warm scopes.
4. When green: complete goal **and** delete the schedule (or `/loop:cancel`).

## Harness checks used in 2026-07-09 dogfood

Package: `~/prj/ai/sublime-claude` (Packages/ClaudeCode symlink).

1. `python3 tests/test_workflow_merge.py` → exit 0  
2. `output.py` has `def refresh_preserving_input`  
3. Work strip uses `◎ goal ·`, not dual `───── Tasks` / `───── Goal`  
4. `ClaudeToggleTasksFoldCommand` calls `refresh_preserving_input` in input mode  

## Session note

`update_goal(completed=true)` only works if the **host** has an Active goal (`/goal …`).  
Without that, progress messages may still log, but complete/blocked are rejected. Cron prompts should still call `update_goal` so a properly activated session closes the loop.

## Wakeup wiring (ClaudeCode + Grok ACP) — 2026-07-09

Observed failure mode during dogfood:

1. Agent called `scheduler_create(interval=2m, fire_immediately=true)`.
2. Host stored the job (`nextFireAt` ~+4m) but **did not** inject a turn
   (`no scheduled_task_fired` / no `x.ai/scheduled_task_inject_prompt` in bridge log).
3. Completed `tool_call_update` dropped `rawInput`, so the bridge never armed
   `loop_scheduled` / `next_wake_at` (banner stayed dark).
4. Agent then `scheduler_delete` before the first fire — so even host cron never ran.

Fixes in `bridge/acp_base.py` + `session.py`:

- Cache tool inputs by `toolCallId` so create payload survives the completed update.
- Arm `loop_scheduled` from interval/result on create.
- **Client-side backup timer** injects `notification_wake` (same path as Claude
  ScheduleWakeup) when host does not call inject — so goal+cron actually re-enters.
- Cancel backup timer on `scheduler_delete`.
- Wakeup banner no longer requires input mode (was hiding mid-turn).

**Restart the Grok bridge session** (sleep/wake or restart session) after pulling
these changes so the bridge process reloads.

### Dogfood checklist

1. Restart ClaudeCode Grok session (reload bridge).
2. `/goal` activate the objective (TUI/host) if you need complete/blocked.
3. `scheduler_create(interval="60s", fire_immediately=true, …)` — do **not** delete early.
4. Expect: `↻ cron · next at …` banner with red **Stop** chip + a new turn with the
   cron prompt ~1.5s later (immediate) then every 60s until Stop / deleted / completed.
   Stop stays visible mid-turn and in input mode.

## Anti-patterns

- Cron prompt that restates a vague “improve the plugin”
- Completing the goal because “looks fine” without running verification
- Leaving a recurring schedule after `completed=true`
- Merging workflow multi-agent panel into the Work strip (different density)
