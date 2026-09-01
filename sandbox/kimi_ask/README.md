# Kimi AskUserQuestion

`kimi acp` 0.38.0 (`packages/acp-server/src/question.ts` in the binary):

- Client `elicitation.form` → `handleQuestion` calls `elicitation/create`
  with **every** question (`q0`, `q1`, …).
- `elicitationResponseToQuestionAnswers` keeps only **exact option labels**.
- Native engine Other (session_18b6df1a): `answers[question]=typed string`,
  `method=enter`. No `{kind:other}` wrapper. Host sends that string as `qN`
  and chains the same JSON as a followup `session/prompt` after `end_turn`.
- `request_permission` fallback is still `/^q0_opt_(\d+)$/` only.
- Do **not** `session/cancel` + prose followup. That is an anti-pattern.
  Listed Q1 is the elicitation accept body.

```bash
python3 sandbox/kimi_ask/check_host.py
python3 sandbox/kimi_ask/check_e2e.py listed other
python3 sandbox/kimi_ask/check_e2e.py interrupt_during
```

Live (`listed`): `{q0: procmotion, q1: dynamics only}` →
`SANDBOX_RESULT Q0=procmotion Q1=dynamics only` (no second prompt).

Live (`other`): host accept content includes the typed string (native).
ACP may still drop it from the tool result; followup `session/prompt`
carries `{"answers": {question: text}}`.

Live (`after_prompt`): first turn `Q1=MISSING`, then a second
`session/prompt` **without cancel** → `Q1=all`. Host chains that
followup inside `handle_query` after `end_turn` so the session stays
busy. Do not `session/cancel` the ask.
