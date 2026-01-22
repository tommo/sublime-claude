# Query Lifecycle and Deferred Actions

## Query States

```
IDLE → WORKING → COMPLETED
                    ↓
            [success | error | interrupted]
```

## _on_done Completion Types

| Status | Meaning | Deferred Actions? |
|--------|---------|-------------------|
| (none/empty) | Success | YES |
| "error" in result | Error occurred | NO |
| "interrupted" | User interrupted | NO |

## Deferred Actions

Things that should execute ONLY on successful completion:

1. **Retain injection** (`_compact_query_count`)
   - Set when: `compact_boundary` system message received during query N
   - Execute when: query N completes successfully
   - Clear when: executed, OR query ends with error/interrupt

2. **Queued prompts** (`_queued_prompts`)
   - Set when: `queue_prompt()` called while working
   - Execute when: current query completes successfully
   - Clear when: executed, OR query ends with error/interrupt

3. **Response callback** (`_response_callback`)
   - Set when: channel mode sends a message
   - Execute when: query completes (ANY status - caller needs to handle)
   - This is DIFFERENT - caller needs to know about interrupts

## Current Problem

`_on_done` processes deferred actions AFTER handling status, but doesn't gate them on success:

```python
def _on_done(self, result):
    # ... handle error ...
    # ... handle interrupted ...
    # ... handle success ...

    # BUG: These run regardless of status!
    if self._compact_query_count == self.query_count:
        self._inject_retain_after_compact()

    if self._queued_prompts:
        # ...
```

## Clean Design

Single point of control for "should we process deferred actions?":

```python
def _on_done(self, result):
    self.working = False

    # 1. Determine completion type
    if "error" in result:
        completion = "error"
    elif result.get("status") == "interrupted":
        completion = "interrupted"
    else:
        completion = "success"

    # 2. Handle UI for each type
    if completion == "error":
        self._handle_error(result)
    elif completion == "interrupted":
        self._handle_interrupted()
    else:
        self._handle_success()

    # 3. Response callback fires for ALL completions (channel needs to know)
    self._fire_response_callback()

    # 4. GATE: Only process deferred actions on success
    if completion != "success":
        self._clear_deferred_state()
        return

    # 5. Process deferred actions
    if self._should_inject_retain():
        self._inject_retain_after_compact()
        return

    if self._queued_prompts:
        self._process_queued_prompt()
        return

    # 6. Enter input mode
    self._enter_input_mode()
```

## Key Principle

**One gate, not many patches.**

Instead of:
- Checking `status == "interrupted"` before retain injection
- Checking `status == "interrupted"` before queued prompts
- Checking `status == "interrupted"` before X, Y, Z...

We have:
- One check: `if completion != "success": clear_and_return`
- All deferred action code after that point assumes success

## State Clearing

`_clear_deferred_state()`:
```python
def _clear_deferred_state(self):
    self._compact_query_count = -1
    self._queued_prompts.clear()
    # Note: don't clear _response_callback here, it's handled separately
```

This is also called in `interrupt()` for belt-and-suspenders safety.
