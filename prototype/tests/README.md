# PtyEngineSession tests

Two scripts:

## `test_transcript.py` — pure unit tests
Fast (<1s), no `claude` needed. Validates `cc_transcript`:
- `project_slug` / `transcript_path` (incl. macOS realpath)
- Every `record_to_params` branch (assistant, user-with-tool_result, user-string-skip, isMeta/isSidechain-skip, all `system/*` subtypes, `AskUserQuestion` surface)
- `_format_ask_user`
- `Tailer` (live append + partial-line buffer)

Run: `python3 prototype/tests/test_transcript.py`

## `diagnose_spill.py` — static analyzer for the tool-result spill pattern
No claude needed. Scans real session transcripts for the CC spill marker
(`<persisted-output>` + sidefile) and confirms both naming conventions are
recognized:
- **A: tool_use_id** — sidefile = `<session>/tool-results/<tool_use_id>.txt` (Glob/Grep/Read)
- **B: persistedOutputPath** — sidefile path carried in `toolUseResult.persistedOutputPath` (Bash; short random id)

Run on a single transcript, a project dir, or default to "5 most recently active":
```
python3 prototype/tests/diagnose_spill.py
python3 prototype/tests/diagnose_spill.py ~/.claude/projects/<project-dir>/
python3 prototype/tests/diagnose_spill.py /path/to/session.jsonl
```
Exits 0 if every spill matches a known convention (missing sidefiles are
normal CC cleanup, not drift). Exits 1 if any spill uses an UNKNOWN convention
— that's a real format change, and the engine's sidefile-aware rendering
(future work) needs updating to match.

## `test_pty_probes.py` — **recalibration suite** for new CC versions
Slow (~5 min, one real `claude` API turn per probe). Each probe **triggers a
specific TUI pattern the engine depends on** and asserts the observable
outcome. Re-run after any Claude Code version bump to spot drift:

| Probe | What it pins | Where the engine relies on it |
|---|---|---|
| `dontask_denies_askuser` | `dontAsk` mode auto-denies AskUserQuestion | `_handle_ask_user_question` short-circuit |
| `acceptedits_resolves_askuser` | other modes auto-resolve | same |
| `default_dialog_drivable` | `default` mode dialog driven by Down+Enter | `_drive_next_answer` keystroke driver |
| `bash_permission_structural` | Bash permission menu has the SAME structural shape as AskUserQuestion | `_screen_has_menu` — one detector covers both Phase 5 surfaces |
| `bracketed_paste_multiline_submits` | multi-line paste + deferred Enter submits as one user msg | `query()` submission path |
| `image_inline_via_path` | TUI auto-inlines image attachments from pasted paths | `_materialize_images` + bare-path append |
| `transcript_schema_spot_check` | required transcript fields (`type`/`subtype`/`tool_use_id`/`durationMs`/`toolUseResult`) | `cc_transcript.record_to_params` mapping |
| `large_tool_result_spills_to_file` | large tool_results go to `<session>/tool-results/<tool_use_id>.txt` with a `<persisted-output>` marker in the main .jsonl | Engine tracking still works via tool_use_id, but rendering currently shows only the marker — sidefile reader is a future improvement; this probe catches format drift |

Run: `python3 prototype/tests/test_pty_probes.py`  
Subset: `python3 prototype/tests/test_pty_probes.py default bash` (substring match)

### How to recalibrate on a new CC version
1. Run the suite. Treat each `FAIL` as a drift signal.
2. For a structural-detection FAIL (e.g. `bash_permission_structural`): the TUI menu format changed. Inspect the screen, adjust `PtyEngineSession.DIALOG_OPTION_RE` / `_screen_has_menu`, re-run.
3. For a transcript-schema FAIL: a subtype/field name changed. Update `cc_transcript.record_to_params` and the test together.
4. For a mode-behavior FAIL (e.g. `dontask_denies_askuser` flips to PASS-but-different-content): the mode semantics changed — the engine's mode-agnostic screen detector should still cope, but verify the new branch is plumbed.

`INCONCLUSIVE` (≠ FAIL) means the test couldn't deterministically trigger the
condition (usually claude declined to invoke the tool under a test prompt or
the API flaked) — not a drift signal on its own. Retry with a sharper prompt.
