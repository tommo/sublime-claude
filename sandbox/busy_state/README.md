# Host busy-state patterns

`working=True` without a closer is how sessions die after `@done` /
task-notification. Extract the wire, then drive `TurnState`.

```bash
python3 sandbox/busy_state/extract.py \
  --bridge-log "$TMPDIR/kimi_bridge.70945.log"
python3 -m unittest discover -s sandbox/busy_state -p 'test_*.py'
python3 -m unittest discover -s tests -p 'test_turn_state.py'
```

## Invariant

A session is busy only when the host owns a turn that will go idle via a
known closer:

| kind | how we enter | closer |
|---|---|---|
| `live` | `query()` → `session/prompt` | prompt RPC (`end_turn` / error / interrupt ack) |
| `compacting` | `/compact` RPC already returned | compact-done text |
| `rewinding` | undo restart | `start()` finished |

Not closers: leftover `terminal/create`, synth Bash, `task_notification`,
thinking/text after `end_turn`, parent `signal_complete` paint.

## gitapp (session_40b7e06e, kimi_bridge.70945)

1. `session/prompt` id=6 → later `stopReason: end_turn` (`@done`)
2. Host idle. Kimi keeps `terminal/output` on `term_1705fbc3a0` (bg `pil test`)
3. Kimi `terminal/kill` that term, then `terminal/create` `tail -20 gitapp_test4.log`
4. Host `synth host Bash … (no session tool_call)` with `bg=False`
5. Old host: `_adopt_agent_turn("⚙ task completed")` → `working=True`,
   `_awaiting_query_rpc=False` → no prompt result ever → session stuck

Correct: step 4 is `inbound_action("synth_bash") == paint_bg`, stay `idle`.
Kimi notify is `notify_action("kimi") == surface` (⚙ strip), not `query`.
