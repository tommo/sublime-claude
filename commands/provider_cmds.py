"""Claude Code commands for Sublime Text."""
import os
import sublime
import sublime_plugin
import platform

from ..core import get_active_session, get_session_for_view, create_session
from ..session import Session, load_saved_sessions, load_bookmarks, toggle_bookmark
from ..prompt_builder import PromptBuilder
from ..command_parser import CommandParser
from .. import backends

# Fallback model lists per backend (used when no cache/settings available).
# Snapshot of built-ins at import time; custom providers are looked up live via
# backends.get(backend).default_models in ClaudeSelectModelCommand._get_models.
DEFAULT_MODELS = backends.default_models_dict()


# ─── Custom Anthropic-compatible providers ────────────────────────────────────
# Manage arbitrary Anthropic-compat endpoints (base URL + auth + model aliases),
# mirroring the ccm model-switcher's env surface. Config is persisted to
# settings.custom_providers and picked up live by backends.all_backends().

_SETTINGS_FILE = "ClaudeCode.sublime-settings"
# Fields collected by the add/edit wizard, in order. Each entry:
# (key, label, example, required, is_secret)
#   example  — prefilled on Add so the user edits instead of typing from blank;
#              also shown in the input title as a format hint. Never prefilled
#              for the name field or secret fields (auth_token).
_PROVIDER_FIELDS = [
    ("name",         "Provider name (unique key)",                 "deepseek",   True,  False),
    ("label",        "Display label (blank = use name)",           "DeepSeek",   False, False),
    ("abbrev",       "Tab abbreviation (blank = first 2 chars)",   "DS",         False, False),
    ("base_url",     "Anthropic-compatible base URL",              "https://api.deepseek.com/anthropic", True, False),
    ("auth_token",   "Auth token (blank → read from auth_env_var)", "sk-...",    False, True),
    ("auth_env_var", "Env var holding the auth token",             "DEEPSEEK_API_KEY", False, False),
    ("opus_model",   "Opus alias → model id",                      "deepseek-v4-pro[1m]", False, False),
    ("sonnet_model", "Sonnet alias → model id",                    "deepseek-v4-pro",     False, False),
    ("haiku_model",  "Haiku alias → model id",                     "deepseek-v4-flash",   False, False),
    ("effort",       "Reasoning effort override (low/medium/high/max, blank = global)", "high", False, False),
]


def _load_custom_providers() -> dict:
    s = sublime.load_settings(_SETTINGS_FILE)
    providers = s.get("custom_providers", {}) or {}
    return providers if isinstance(providers, dict) else {}


def _save_custom_providers(providers: dict) -> None:
    s = sublime.load_settings(_SETTINGS_FILE)
    s.set("custom_providers", providers)
    sublime.save_settings(_SETTINGS_FILE)


def _mask_secret(v: str) -> str:
    if not v:
        return ""
    if len(v) <= 6:
        return "<set>"
    return v[:3] + "…" + v[-3:]


def _provider_summary(name: str, cfg: dict) -> str:
    base = (cfg.get("base_url") or "").strip()
    token = (cfg.get("auth_token") or "").strip()
    auth_env_var = (cfg.get("auth_env_var") or "").strip()
    if token:
        cred = _mask_secret(token)
    elif auth_env_var:
        cred = f"${auth_env_var}"
    else:
        cred = "<no auth>"
    effort = (cfg.get("effort") or "").strip()
    effort_tag = f"  •  effort:{effort}" if effort else ""
    return f"{base}  •  {cred}{effort_tag}"


class ClaudeManageProvidersCommand(sublime_plugin.WindowCommand):
    """Manage user-defined Anthropic-compatible providers (quick-panel wizard)."""

    def run(self) -> None:
        self._show_main()

    # ── Main menu ────────────────────────────────────────────────────────────
    def _show_main(self) -> None:
        providers = _load_custom_providers()
        items = []
        actions = []  # ("edit", name) | ("add",) | ("raw",) | ("delete", name) | ("dup", name) | ("test", name) | ("pin", name)
        # Pinned providers first (the ones that surface in the quick panels),
        # then the rest. 📌 marks pinned entries.
        ordered = sorted(providers.items(), key=lambda kv: not bool((kv[1] or {}).get("pinned", False)))
        for name, cfg in ordered:
            cfg = cfg or {}
            label = (cfg.get("label") or name)
            pin = "📌 " if cfg.get("pinned") else "    "
            note = "pinned → shows in quick panel" if cfg.get("pinned") else "not pinned"
            items.append([f"✎  {pin}{label}", "{}  •  {}".format(_provider_summary(name, cfg), note)])
            actions.append(("edit", name))
        items.append(["+  Add Provider…", "Define a new Anthropic-compatible endpoint"])
        actions.append(("add", None))
        items.append(["{ }  Edit raw JSON…", "Open custom_providers in the settings file"])
        actions.append(("raw", None))        # Per-provider actions surfaced as a second-level region.
        if providers:
            for name, cfg in ordered:
                cfg = cfg or {}
                label = (cfg.get("label") or name)
                pin_label = "Unpin" if cfg.get("pinned") else "Pin"
                items.append([f"{'📌' if cfg.get('pinned') else '📍'}  {pin_label}: {label}",
                              "Toggle whether it shows in the quick panels"])
                actions.append(("pin", name))
                items.append([f"📋 Duplicate: {label}", "Copy this provider's config"])
                actions.append(("dup", name))
                items.append([f"🎯 Generate model config: {label}", "Fetch live models and set opus/sonnet/haiku aliases"])
                actions.append(("genmodels", name))
                items.append([f"🔍 Test config: {label}", "Validate base_url + auth presence"])
                actions.append(("test", name))
                items.append([f"🗑 Delete: {label}", "Remove this provider"])
                actions.append(("delete", name))

        def on_select(idx):
            if idx < 0:
                return
            action, data = actions[idx]
            if action == "edit":
                self._run_wizard(existing=data)
            elif action == "add":
                self._run_wizard(existing=None)
            elif action == "raw":
                self.window.run_command("edit_settings", {
                    "base_file": "${packages}/ClaudeCode/ClaudeCode.sublime-settings",
                })
            elif action == "dup":
                self._duplicate(data)
            elif action == "test":
                self._test(data)
            elif action == "genmodels":
                self.window.run_command("claude_generate_provider_models", {"provider": data})
            elif action == "delete":
                self._delete(data)
            elif action == "pin":
                self._toggle_pin(data)

        self.window.show_quick_panel(items, on_select, placeholder="Manage Anthropic providers")

    def _toggle_pin(self, name: str) -> None:
        providers = _load_custom_providers()
        cfg = providers.get(name) or {}
        cfg["pinned"] = not bool(cfg.get("pinned", False))
        providers[name] = cfg
        _save_custom_providers(providers)
        state = "pinned → shows in quick panel" if cfg["pinned"] else "unpinned"
        sublime.status_message("'{}' {}".format(name, state))
        self._show_main()

    # ── Add / Edit wizard ────────────────────────────────────────────────────
    def _run_wizard(self, existing: str = None) -> None:
        cfg = dict(_load_custom_providers().get(existing, {}) or {}) if existing else {}
        self._fields = list(_PROVIDER_FIELDS)
        self._editing = existing
        self._values = {}
        # Seed values: edit → from existing cfg; add → from the field's example.
        # The name key (chosen by the user) and secret fields are left blank on
        # add — their examples still show in the input title as format hints.
        for key, label, example, required, secret in self._fields:
            if key == "name":
                self._values[key] = existing or ""
            elif existing and key in cfg:
                self._values[key] = str(cfg.get(key, ""))
            elif secret:
                self._values[key] = ""
            else:
                self._values[key] = example
        self._step = 0
        self._return_to_review = False
        self._prompt_field()

    def _prompt_field(self, return_to_review: bool = False) -> None:
        # return_to_review: invoked from the review screen to edit one field;
        # after the field is entered we go back to the review instead of the
        # next field in the chain.
        self._return_to_review = return_to_review
        if self._step >= len(self._fields):
            self._review()
            return
        key, label, example, required, secret = self._fields[self._step]
        current = self._values.get(key, "")
        title = label
        if example and not current:
            title += "   e.g. {}".format(example)
        if required:
            title += "  [required]"
        if secret:
            # show_input_panel can't mask; accept plaintext but steer to env var.
            title += "  (stored in settings — prefer auth_env_var)"

        def reopen(attempt):
            self.window.show_input_panel(title, attempt, on_done, None, on_cancel)

        def on_done(value):
            value = (value or "").strip()
            if required and not value:
                sublime.status_message("{} is required".format(label))
                reopen(value)
                return
            if key == "base_url" and value:
                low = value.lower()
                if not (low.startswith("http://") or low.startswith("https://")):
                    sublime.status_message("base_url must start with http:// or https://")
                    reopen(value)
                    return
            if key == "name":
                providers = _load_custom_providers()
                if value != self._editing and value in providers:
                    sublime.status_message("A provider named '{}' already exists".format(value))
                    reopen(value)
                    return
            if key == "effort" and value and value not in ("low", "medium", "high", "max"):
                sublime.status_message("effort must be one of: low, medium, high, max (or blank)")
                reopen(value)
                return
            self._values[key] = value
            if self._return_to_review:
                self._review()
                return
            self._step += 1
            self._prompt_field()

        def on_cancel():
            # Drop back to the review if mid-edit-from-review, else main menu.
            if self._return_to_review:
                self._review()
            else:
                sublime.status_message("Provider wizard cancelled")

        reopen(current)

    # ── Review / confirm before commit ───────────────────────────────────────
    def _assembled_cfg(self) -> dict:
        """Build the cfg dict (everything except the name key) from _values,
        dropping blanks. Preserves raw-JSON-only keys (extra_env /
        auth_via_api_key / subagent_model) from the existing entry on edit."""
        cfg = {}
        for key, label, example, required, secret in self._fields:
            if key == "name":
                continue
            v = (self._values.get(key) or "").strip()
            if v:
                cfg[key] = v
        if self._editing:
            old = _load_custom_providers().get(self._editing, {}) or {}
            for preserve in ("extra_env", "auth_via_api_key", "subagent_model", "pinned"):
                if preserve in old and preserve not in cfg:
                    cfg[preserve] = old[preserve]
        return cfg

    def _auth_display(self, cfg: dict, name: str) -> str:
        if (cfg.get("auth_token") or "").strip():
            return "token " + _mask_secret(cfg["auth_token"])
        env = (cfg.get("auth_env_var") or "").strip()
        if env:
            resolved = backends.resolve_auth_token(cfg, name)
            return "${} {}".format(env, "✓ set" if resolved else "⚠ UNSET")
        return "⚠ no auth"

    def _review(self) -> None:
        name = self._values.get("name") or self._editing
        if not name:
            sublime.status_message("Provider not saved: no name")
            return
        cfg = self._assembled_cfg()
        label = cfg.get("label") or name
        auth = self._auth_display(cfg, name)
        model_parts = []
        for key, human in (("opus_model", "opus"), ("sonnet_model", "sonnet"), ("haiku_model", "haiku")):
            v = cfg.get(key)
            if v:
                model_parts.append("{}: {}".format(human, v))
        models = "  ".join(model_parts) if model_parts else "(no aliases — picker falls back to opus/sonnet/haiku)"

        items = [
            ["✓  Save '{}'".format(label),
             "{}  •  {}  •  {}".format(cfg.get("base_url", "<no base_url>"), auth, models)],
            ["✎  Edit a field…", "Re-prompt any field, then return here"],
            ["✗  Cancel", "Discard — nothing saved"],
        ]
        actions = [("save", None), ("edit", None), ("cancel", None)]

        def on_select(idx):
            if idx < 0:
                return
            action, _ = actions[idx]
            if action == "save":
                self._commit()
            elif action == "edit":
                self._pick_field_to_edit()
            # cancel: drop silently

        self.window.show_quick_panel(items, on_select,
                                     placeholder="Review provider '{}'".format(name))

    def _pick_field_to_edit(self) -> None:
        items = []
        for key, label, example, required, secret in self._fields:
            v = self._values.get(key, "")
            shown = _mask_secret(v) if secret else (v or "(blank)")
            items.append(["{}".format(label), shown])
        def on_select(idx):
            if idx < 0:
                self._review()
                return
            self._step = idx
            self._prompt_field(return_to_review=True)
        self.window.show_quick_panel(items, on_select, placeholder="Edit which field?")

    def _commit(self) -> None:
        name = self._values.get("name") or self._editing
        if not name:
            sublime.status_message("Provider not saved: no name")
            return
        cfg = self._assembled_cfg()
        providers = _load_custom_providers()
        # Rename: editing under a different name drops the old key.
        if self._editing and self._editing != name:
            providers.pop(self._editing, None)
        providers[name] = cfg
        _save_custom_providers(providers)
        label = cfg.get("label", name)
        sublime.status_message("Saved provider '{}'".format(label))
        self._show_main()

    # ── Duplicate / Delete / Test ────────────────────────────────────────────
    def _duplicate(self, name: str) -> None:
        providers = _load_custom_providers()
        cfg = dict(providers.get(name, {}) or {})
        i = 2
        new_name = "{}_copy".format(name)
        while new_name in providers:
            new_name = "{}_copy{}".format(name, i)
            i += 1
        providers[new_name] = cfg
        _save_custom_providers(providers)
        sublime.status_message("Duplicated '{}' → '{}'".format(name, new_name))
        self._show_main()

    def _delete(self, name: str) -> None:
        providers = _load_custom_providers()
        if name not in providers:
            return
        # Confirm via a quick panel (yes/no).
        def on_confirm(idx):
            if idx == 0:
                providers.pop(name, None)
                _save_custom_providers(providers)
                sublime.status_message("Deleted provider '{}'".format(name))
            self._show_main()
        self.window.show_quick_panel(
            ["Yes, delete", "Cancel"],
            on_confirm,
            placeholder="Delete provider '{}'?".format(name),
        )

    def _test(self, name: str) -> None:
        """Validate config: base_url is a URL and auth is present (inline or env).

        Does NOT make a network request — just confirms the provider will resolve
        to a usable env via backends.get(name), which is what session start does.
        """
        providers = _load_custom_providers()
        cfg = providers.get(name, {}) or {}
        problems = []
        warnings = []
        base = (cfg.get("base_url") or "").strip()
        auth_env_var = (cfg.get("auth_env_var") or "").strip()
        if not base:
            problems.append("missing base_url")
        elif not (base.lower().startswith("http://") or base.lower().startswith("https://")):
            problems.append("base_url is not a valid URL")
        # Resolve the effective token (inline, env, or deepseek legacy) via the
        # shared helper so this stays in lockstep with session start.
        eff_token = backends.resolve_auth_token(cfg, name)
        if not eff_token:
            problems.append("no auth_token and auth_env_var '{}' is unset".format(auth_env_var or "<blank>"))
        # Catch the classic ccm footgun: ~/.ccm_config ships placeholder values
        # like 'sk-your-deepseek-api-key' that leak into the env and silently
        # 401 at the provider. Flag any token that looks like a template.
        if eff_token:
            low = eff_token.lower()
            if ("your-" in low or "your_" in low or "yourkey" in low
                    or "sk-your" in low or "replace" in low or "xxxx" in low
                    or eff_token in ("your-api-key", "your-api_key")):
                warnings.append(
                    "resolved token looks like a PLACEHOLDER ('{}…') — likely from "
                    "~/.ccm_config clobbering the real key. Check `echo ${}` and "
                    "~/.ccm_config.".format(eff_token[:20], auth_env_var or "<var>"))
        available = backends.is_available(name)
        # Build the env the bridge would receive, masked, so the user can confirm.
        try:
            spec = backends.get(name)
            overwrite, defaults = spec.dynamic_env({
                "custom_providers": providers,
            }) if spec.dynamic_env else ({}, {})

            def _m(k, v):
                if any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
                    return _mask_secret(v) if v else "<empty>"
                return v
            env_preview = {k: _m(k, v) for k, v in {**defaults, **overwrite}.items()}
        except Exception as e:
            env_preview = {"<error>": str(e)}
        status = "OK" if (not problems and available and not warnings) else ("PROBLEM" if problems or not available else "WARNING")
        lines = ["Provider '{}': {}".format(name, status)]
        if problems:
            lines.append("  Issues: {}".format("; ".join(problems)))
        if warnings:
            for w in warnings:
                lines.append("  ⚠ {}".format(w))
        if not available:
            lines.append("  backends.is_available → False")
        lines.append("  Resolved env (masked):")
        for k in sorted(env_preview):
            lines.append("    {} = {}".format(k, env_preview[k]))
        sublime.message_dialog("\n".join(lines))


class ClaudeStartCustomProviderCommand(sublime_plugin.WindowCommand):
    """Start a session on a chosen custom Anthropic-compatible provider."""

    def run(self) -> None:
        providers = _load_custom_providers()
        # Only pinned providers surface here — pinning is the opt-in that keeps
        # the picker from bloating with every seeded endpoint. Manage Providers
        # lists all of them (pinned or not) for configuration.
        pinned = {n: c for n, c in providers.items() if (c or {}).get("pinned")}
        if not pinned:
            sublime.error_message(
                "No providers are pinned to the quick panel.\n\n"
                "Run 'Claude: Manage Anthropic Providers' → Pin a provider.")
            return
        items = []
        names = []
        for name, cfg in pinned.items():
            cfg = cfg or {}
            label = cfg.get("label", name)
            avail = backends.is_available(name)
            mark = "●" if avail else "○"
            items.append(["{}  {}".format(mark, label), _provider_summary(name, cfg)])
            names.append(name)

        def on_select(idx):
            if idx < 0:
                return
            name = names[idx]
            if not backends.is_available(name):
                sublime.error_message(
                    "Provider '{}' is not usable (missing base_url or auth).\n"
                    "Run 'Claude: Manage Anthropic Providers' → Test config.".format(name))
                return
            create_session(self.window, backend=name)

        self.window.show_quick_panel(items, on_select, placeholder="Start session on provider…")


class ClaudeGenerateProviderModelsCommand(sublime_plugin.WindowCommand):
    """Per-provider sub-model picker: fetch a provider's live model list and let
    the user set the opus/sonnet/haiku alias → model-id mappings via quick panel.

    'generate' = the alias config is produced from the provider's real model
    list rather than typed by hand. OpenRouter uses its public models endpoint
    (no auth); other providers hit {base_url}/v1/models with the resolved auth.
    Falls back to manual entry if the fetch fails or returns nothing.
    """

    ALIASES = [("opus_model", "Opus"), ("sonnet_model", "Sonnet"), ("haiku_model", "Haiku")]

    def run(self, provider: str = None) -> None:
        providers = _load_custom_providers()
        if not providers:
            sublime.error_message(
                "No custom providers configured.\n\n"
                "Run 'Claude: Manage Anthropic Providers' to add one first.")
            return
        if provider and provider in providers:
            self._fetch_then_pick(provider)
            return
        # No provider arg → quick-panel to choose one.
        items = []
        names = []
        for name, cfg in providers.items():
            cfg = cfg or {}
            label = cfg.get("label", name)
            cur = " / ".join((cfg.get(a) or "—") for a, _ in self.ALIASES)
            items.append([label, "current: {}".format(cur)])
            names.append(name)

        def on_select(idx):
            if idx < 0:
                return
            self._fetch_then_pick(names[idx])

        self.window.show_quick_panel(items, on_select, placeholder="Pick provider to configure models…")

    # ── Resolve auth for a provider (shared helper in backends) ──────────────────
    def _resolve_auth(self, cfg: dict, name: str = None):
        """Returns (header_name, header_value) or (None, None) if no auth."""
        token = backends.resolve_auth_token(cfg, name)
        if not token:
            return None, None
        if cfg.get("auth_via_api_key", False):
            return "x-api-key", token
        return "Authorization", "Bearer {}".format(token)

    # ── Fetch models in a background thread ────────────────────────────────────
    def _fetch_then_pick(self, name: str) -> None:
        providers = _load_custom_providers()
        cfg = providers.get(name, {}) or {}
        base_url = (cfg.get("base_url") or "").strip()
        if not base_url:
            sublime.error_message("Provider '{}' has no base_url.".format(name))
            return

        sublime.status_message("Fetching models for '{}'…".format(name))
        import threading

        def work():
            models = self._fetch_models(name, cfg, base_url)
            sublime.set_timeout(lambda: self._pick_aliases(name, models), 0)

        threading.Thread(target=work, daemon=True).start()

    def _fetch_models(self, name: str, cfg: dict, base_url: str):
        """Return a list of [id, label] model entries. Empty list on failure."""
        import json as _json
        import urllib.request

        # OpenRouter: public models endpoint, no auth, returns data[].id.
        if "openrouter.ai" in base_url.lower():
            try:
                req = urllib.request.Request("https://openrouter.ai/api/v1/models")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = _json.loads(resp.read().decode())
                out = []
                for m in data.get("data", []):
                    mid = m.get("id", "")
                    if not mid:
                        continue
                    # OpenRouter ids are provider-qualified (e.g. anthropic/claude-sonnet-4.5)
                    out.append([mid, mid])
                if out:
                    out.sort(key=lambda x: x[0])
                    return out
            except Exception as e:
                print("[Claude] fetch openrouter models error: {}".format(e))

        # Generic Anthropic-compat: GET {base_url}/v1/models with resolved auth.
        auth_h, auth_v = self._resolve_auth(cfg, name)
        url = base_url.rstrip("/") + "/v1/models"
        try:
            headers = {"anthropic-version": "2023-06-01"}
            if auth_h:
                headers[auth_h] = auth_v
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = _json.loads(resp.read().decode())
            out = []
            for m in data.get("data", []):
                mid = m.get("id", "")
                if not mid:
                    continue
                mname = m.get("display_name") or m.get("name") or mid
                out.append([mid, mname])
            if out:
                out.sort(key=lambda x: x[0])
                return out
        except Exception as e:
            print("[Claude] fetch {} models error: {}".format(name, e))

        return []  # caller falls back to manual entry

    # ── Alias picker ───────────────────────────────────────────────────────────
    def _pick_aliases(self, name: str, models: list) -> None:
        """For each alias, show a quick panel of fetched models (pre-selecting
        the current value) plus a manual-entry option. Persist at the end."""
        providers = _load_custom_providers()
        cfg = providers.get(name, {}) or {}
        self._new_cfg = dict(cfg)
        self._models = models  # [[id, label], ...]
        self._alias_idx = 0
        self._pick_one_alias(name)

    def _pick_one_alias(self, name: str) -> None:
        if self._alias_idx >= len(self.ALIASES):
            self._commit(name)
            return
        key, label = self.ALIASES[self._alias_idx]
        current = (self._new_cfg.get(key) or "").strip()

        items = []
        actions = []  # ("model", id) | ("manual",)
        # Pre-select current by listing it first with a marker.
        if current:
            items.append(["● {}  (current)".format(current), "keep current {}".format(label)])
            actions.append(("model", current))
        for mid, mname in self._models:
            mark = "● " if mid == current else "  "
            # Keep the id visible (it's what gets stored) alongside the label.
            items.append(["{}{}".format(mark, mname), mid])
            actions.append(("model", mid))
        items.append(["✎  Type a model id manually…", "enter an arbitrary model id"])
        actions.append(("manual", None))

        def on_select(idx):
            if idx < 0:
                # Cancelled mid-way: abort without saving.
                sublime.status_message("Model config cancelled (nothing saved)")
                return
            action, data = actions[idx]
            if action == "model":
                self._new_cfg[key] = data
                self._alias_idx += 1
                self._pick_one_alias(name)
            elif action == "manual":
                def on_done(value):
                    value = (value or "").strip()
                    if not value:
                        # Empty manual entry: re-open this alias.
                        sublime.status_message("Empty model id; {} not changed".format(label))
                        self._pick_one_alias(name)
                        return
                    self._new_cfg[key] = value
                    self._alias_idx += 1
                    self._pick_one_alias(name)
                self.window.show_input_panel(
                    "{} alias → model id".format(label), current, on_done, None, None)

        if self._models:
            placeholder = "{} alias ({} models fetched)".format(label, len(self._models))
        else:
            placeholder = "{} alias (no models fetched — type manually)".format(label)
        self.window.show_quick_panel(items, on_select, placeholder=placeholder)

    def _commit(self, name: str) -> None:
        providers = _load_custom_providers()
        providers[name] = self._new_cfg
        _save_custom_providers(providers)
        summary = " / ".join("{}={}".format(l, self._new_cfg.get(k, "—"))
                             for k, l in self.ALIASES)
        sublime.status_message("Saved {} models: {}".format(name, summary))




class ClaudeChangeProviderCommand(sublime_plugin.WindowCommand):
    """Change the active session's provider on the fly to a different Claude-
    bridge backend, carrying the conversation over. Claude-bridge family only
    (claude + custom Anthropic-compatible providers)."""
    def run(self) -> None:
        s = get_active_session(self.window)
        if not s:
            sublime.error_message("No active session to change.")
            return
        items, names = [], []
        for name, spec in backends.all_backends().items():
            if spec.bridge_script != "main.py":
                continue  # only Claude-bridge backends are eligible
            label = spec.label or name
            avail = backends.is_available(name)
            cur = "   (current)" if name == s.backend else ""
            avail_tag = "" if avail else "   (unavailable)"
            detail = "backend: {}".format(name) + ("" if avail else " — missing base_url/auth")
            # "with X…" reads as "continue this conversation WITH X" — clearer
            # about the contextual (carry-over) nature of the change than a bare
            # provider name or "switch to X".
            items.append(["with {}…{}{}".format(label, cur, avail_tag), detail])
            names.append(name)
        if not items:
            sublime.error_message("No Claude-bridge providers configured.")
            return

        def on_select(idx):
            if idx < 0:
                return
            s.change_backend(names[idx])

        self.window.show_quick_panel(items, on_select,
                                     placeholder="Change provider for current session…")

    def is_enabled(self) -> bool:
        return get_active_session(self.window) is not None


class ClaudeSelectEffortCommand(sublime_plugin.WindowCommand):
    """Change reasoning effort for current session (persists via settings, applied on next restart)."""
    LEVELS = ["low", "medium", "high", "max"]

    def run(self) -> None:
        s = get_active_session(self.window)
        if not s:
            sublime.status_message("No active session")
            return
        if s.backend != "claude":
            sublime.status_message("Effort only supported for claude backend")
            return

        def on_select(idx):
            if idx < 0:
                return
            level = self.LEVELS[idx]
            settings = sublime.load_settings("ClaudeCode.sublime-settings")
            settings.set("effort", level)
            sublime.save_settings("ClaudeCode.sublime-settings")
            sublime.status_message(f"Effort set to {level} — takes effect on next session restart")

        self.window.show_quick_panel(self.LEVELS, on_select)

    def is_enabled(self):
        s = get_active_session(self.window)
        return s is not None and s.backend == "claude"


class ClaudeSelectModelCommand(sublime_plugin.WindowCommand):
    """Quick panel to select model for current session."""
    def run(self) -> None:
        s = get_active_session(self.window)
        if not s:
            sublime.error_message("No active Claude session")
            return
        if s.working:
            sublime.error_message("Session is busy — wait for the current request to finish")
            return
        backend = s.backend
        models = self._get_models(backend)
        if not models:
            sublime.error_message(f"No models for {backend}.\nRun 'Claude: Refresh Models' first.")
            return
        items = []
        model_ids = []
        for m in models:
            if isinstance(m, str):
                mid, mname = m, m
            elif isinstance(m, list) and len(m) >= 2:
                mid, mname = m[0], m[1]
            else:
                continue
            items.append([mname, mid])
            model_ids.append(mid)

        def on_select(idx):
            if idx < 0:
                return
            mid = model_ids[idx]
            from ..session import _resolve_model_id
            real_model, ctx = _resolve_model_id(mid)
            if ctx:
                if sublime.ok_cancel_dialog(
                    f"Context limit ({ctx // 1000}K) requires session restart.\n\nRestart session with {mid}?",
                    "Restart"
                ):
                    settings = sublime.load_settings("ClaudeCode.sublime-settings")
                    default_models = settings.get("default_models", {})
                    default_models[s.backend] = mid
                    settings.set("default_models", default_models)
                    sublime.save_settings("ClaudeCode.sublime-settings")
                    s.restart()
                return
            if s.client:
                s.client.send("set_model", {"model": real_model})
            sublime.status_message(f"Model: {mid}")

        self.window.show_quick_panel(items, on_select)

    def _get_models(self, backend):
        import os
        settings = sublime.load_settings("ClaudeCode.sublime-settings")
        all_models = settings.get("models", {})
        # Merge cached
        cached_file = os.path.expanduser("~/.claude/sublime_cached_models.json")
        if os.path.exists(cached_file):
            try:
                import json as _json
                with open(cached_file) as f:
                    cached = _json.load(f)
                for b, models in cached.items():
                    if b not in all_models:
                        all_models[b] = models
            except Exception:
                pass
        if backend not in all_models:
            # Live lookup so custom providers (added via the UI after plugin
            # load) are picked up; DEFAULT_MODELS is just the built-in snapshot.
            try:
                all_models[backend] = [list(m) for m in backends.get(backend).default_models]
            except Exception:
                all_models[backend] = DEFAULT_MODELS.get(backend, [])
        return all_models.get(backend, [])


class ClaudeSetDefaultModelCommand(sublime_plugin.WindowCommand):
    """Set default model per backend in settings."""
    def run(self) -> None:
        # Built-ins + user custom providers, in a stable order: built-ins first
        # (claude, codex, copilot, pi, dsr) then any custom providers not yet
        # listed. Excludes claude from the per-backend picker (it's the default
        # and has its own legacy default_model key handled below).
        seen = set()
        backends_list = []
        for name in ("claude", "codex", "copilot", "pi", "dsr", "grok", "grok_cc"):
            if backends.is_available(name) or name == "claude":
                backends_list.append(name)
                seen.add(name)
        for name, spec in backends.all_backends().items():
            if name in seen:
                continue
            # Custom providers are opt-in via the pin flag; built-ins (pinned
            # default True) are unaffected.
            if not spec.pinned:
                continue
            if spec.available is None or spec.available():
                backends_list.append(name)
                seen.add(name)
        items = [[b.title(), f"Set default model for {b}"] for b in backends_list]

        def on_backend(idx):
            if idx < 0:
                return
            backend = backends_list[idx]
            models = ClaudeSelectModelCommand._get_models(None, backend)
            if not models:
                sublime.status_message(f"No models for {backend}. Run Claude: Refresh Models first.")
                return
            model_items = []
            model_ids = []
            for m in models:
                if isinstance(m, str):
                    mid, mname = m, m
                elif isinstance(m, list) and len(m) >= 2:
                    mid, mname = m[0], m[1]
                else:
                    continue
                model_items.append([mname, mid])
                model_ids.append(mid)

            def on_model(midx):
                if midx < 0:
                    return
                mid = model_ids[midx]
                settings = sublime.load_settings("ClaudeCode.sublime-settings")
                defaults = settings.get("default_models", {})
                defaults[backend] = mid
                settings.set("default_models", defaults)
                # Also set legacy default_model for claude
                if backend == "claude":
                    settings.set("default_model", mid)
                sublime.save_settings("ClaudeCode.sublime-settings")
                sublime.status_message(f"Default {backend} model: {mid}")

            self.window.show_quick_panel(model_items, on_model)

        self.window.show_quick_panel(items, on_backend)


class ClaudeSetDefaultProviderCommand(sublime_plugin.WindowCommand):
    """Set the default provider (backend) used by a plain "New Session".

    The plugin's default is official Claude (backend 'claude'). This command
    lets the user point it at any available backend — a built-in (codex,
    copilot, pi, dsr) or a custom Anthropic-compatible provider — in a single
    quick-panel step. Selecting a provider sets `default_backend` and uses the
    backend's fallback model (it does NOT write a per-backend model override;
    use 'Claude: Set Default Model' for that).
    """

    def run(self) -> None:
        settings = sublime.load_settings("ClaudeCode.sublime-settings")
        current_backend = settings.get("default_backend", "claude")
        default_models = settings.get("default_models", {}) or {}

        # Build the backend list: built-ins first (claude always present), then
        # available custom providers. Same ordering as ClaudeSetDefaultModelCommand.
        seen = set()
        backends_list = []
        for name in ("claude", "codex", "copilot", "pi", "dsr", "grok", "grok_cc"):
            if name == "claude" or backends.is_available(name):
                backends_list.append(name)
                seen.add(name)
        for name, spec in backends.all_backends().items():
            if name in seen:
                continue
            # Custom providers are opt-in via the pin flag; built-ins (pinned
            # default True) are unaffected.
            if not spec.pinned:
                continue
            if spec.available is None or spec.available():
                backends_list.append(name)
                seen.add(name)

        items = []
        for name in backends_list:
            spec = backends.get(name)
            label = spec.label or name
            is_current = (name == current_backend)
            # Effective model = per-backend override if any, else the spec's fallback.
            effective = default_models.get(name) or spec.fallback_model or "—"
            mark = "● " if is_current else "  "
            detail = "current default · model: {}".format(effective) if is_current \
                else "model: {}".format(effective)
            items.append(["{}{}".format(mark, label), detail])

        def on_select(idx):
            if idx < 0:
                return
            self._save(backends_list[idx])

        placeholder = "Set default provider"
        cur_label = backends.get(current_backend).label or current_backend
        cur_model = default_models.get(current_backend) or backends.get(current_backend).fallback_model
        placeholder += "  →  {} / {}".format(cur_label, cur_model or "—")
        self.window.show_quick_panel(items, on_select, placeholder=placeholder)

    def _save(self, backend):
        settings = sublime.load_settings("ClaudeCode.sublime-settings")
        settings.set("default_backend", backend)
        # Don't write a per-backend model override — let Session.start fall back
        # to the spec's fallback_model. (Use 'Set Default Model' to override.)
        sublime.save_settings("ClaudeCode.sublime-settings")
        spec = backends.get(backend)
        label = spec.label or backend
        sublime.status_message("Default provider: {} (model: {})".format(label, spec.fallback_model or "—"))


class ClaudeRefreshModelsCommand(sublime_plugin.WindowCommand):
    """Fetch available models from backends and cache them."""
    def run(self) -> None:
        import threading

        def fetch():
            import os, json as _json
            cached = {}

            # Claude models (from Anthropic API)
            try:
                import urllib.request
                api_key = os.environ.get("ANTHROPIC_API_KEY", "")
                if api_key:
                    req = urllib.request.Request(
                        "https://api.anthropic.com/v1/models",
                        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"}
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = _json.loads(resp.read().decode())
                    result = []
                    for m in data.get("data", []):
                        mid = m.get("id", "")
                        name = m.get("display_name", mid)
                        result.append([mid, name])
                    if result:
                        cached["claude"] = result
            except Exception as e:
                print(f"[Claude] refresh models claude error: {e}")

            # Copilot models (live from SDK)
            try:
                import asyncio
                from copilot import CopilotClient

                async def get_copilot_models():
                    client = CopilotClient()
                    await client.start()
                    models = await client.list_models()
                    result = []
                    for m in models:
                        mid = getattr(m, 'id', '')
                        name = getattr(m, 'name', '')
                        billing = getattr(m, 'billing', None)
                        mult = getattr(billing, 'multiplier', 1) if billing else 1
                        label = f"{name} ({mult}x)" if mult != 1 else name
                        result.append([mid, label])
                    await client.stop()
                    return result

                cached["copilot"] = asyncio.run(get_copilot_models())
            except Exception as e:
                print(f"[Claude] refresh models copilot error: {e}")

            # Fallback for backends without list API
            for backend_name, fallback_models in DEFAULT_MODELS.items():
                if backend_name not in cached:
                    cached[backend_name] = fallback_models

            # Write cache
            cache_path = os.path.expanduser("~/.claude/sublime_cached_models.json")
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w") as f:
                _json.dump(cached, f, indent=2)

            count = sum(len(v) for v in cached.values())
            sublime.set_timeout(lambda: sublime.status_message(f"Cached {count} models"), 0)

        sublime.status_message("Fetching models...")
        threading.Thread(target=fetch, daemon=True).start()


