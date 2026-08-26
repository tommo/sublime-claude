# Kimi `turn.agent_busy`

```bash
python3 sandbox/kimi_busy/check_e2e.py overlap after0 overlap_retry bg_after0
```

Live 0.38.2:

| strategy | result |
|---|---|
| `overlap` | second `session/prompt` → `-32600` `turn.agent_busy` |
| `after0` | first `end_turn`, immediate second → `SANDBOX_BUSY_TWO` |
| `overlap_retry` | busy, wait for first `end_turn`, retry **without cancel** → TWO |
| `bg_after0` | bg bash `end_turn` then second → TWO |

Host: serialize `handle_query` on `_query_lock`. On busy, wait and retry — do not `session/cancel`.
