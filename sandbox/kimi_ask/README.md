# Kimi AskUserQuestion

`kimi acp` 0.37.2:

- Client `elicitation.form` → `elicitation/create` with every question.
- Enum filter keeps only declared option labels. Other/freeform is dropped.
- `session/prompt` during the ask turn is `turn.agent_busy`.
- Q1 Other reaches the model only via `session/cancel` then a followup prompt.
- Esc during an unanswered ask must `session/cancel` *without* first
  answering elicitation (that dismisses and the turn continues).

```bash
python3 sandbox/kimi_ask/check_host.py
python3 sandbox/kimi_ask/check_e2e.py q0 cancel_then interrupt_during
```
