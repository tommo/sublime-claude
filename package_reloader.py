"""Correct in-process package reload for ClaudeCode (no ST restart).

Adapted from Terminus/GitSavvy/AutomaticPackageReloader patterns:
unload → purge sys.modules → reload root plugins with import interception
so submodules refresh instead of sticking as stale class/function objects.

Also supports a hard cycle via Preferences.ignored_packages (full ST unload).
"""
from __future__ import annotations

import builtins
import functools
import importlib
import importlib.machinery
import importlib.util
import os
import sys
import threading
import types
from contextlib import contextmanager
from typing import Any, Dict, Optional

import sublime
import sublime_plugin

PKG = "ClaudeCode"


def dprint(*args, **kwargs):
    print("[Claude Reload]", *args, **kwargs)


def package_modules(pkg_name: str = PKG) -> Dict[str, Any]:
    """All loaded modules belonging to the package (with or without __init__)."""
    out = {}
    prefix = pkg_name + "."
    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        if name == pkg_name or name.startswith(prefix):
            out[name] = mod
    return out


def reload_package(pkg_name: str = PKG, dummy: bool = True, verbose: bool = True) -> dict:
    """Soft reload: unload plugins/modules and re-import everything fresh."""
    modules = package_modules(pkg_name)
    if not modules:
        return {"ok": False, "error": f"{pkg_name} is not loaded", "mode": "soft"}

    if verbose:
        dprint("begin soft reload", "=" * 40)
        dprint(f"{len(modules)} module(s)")

    # 0) Clear view phantoms before modules die (sleep banner is the usual sticky)
    try:
        from .session import clear_all_claude_phantoms
        n = clear_all_claude_phantoms()
        if verbose:
            dprint(f"cleared phantom keys: {n}")
    except Exception as e:
        dprint(f"phantom clear before unload: {e}")

    # 1) plugin_unloaded hooks (once per unique callable)
    seen_unload = set()
    for name in sorted(modules, key=lambda n: n.count("."), reverse=True):
        mod = modules[name]
        fn = getattr(mod, "plugin_unloaded", None)
        if callable(fn) and id(fn) not in seen_unload:
            seen_unload.add(id(fn))
            try:
                if verbose:
                    dprint("plugin_unloaded", name)
                fn()
            except Exception as e:
                dprint(f"plugin_unloaded {name} error: {e}")

    # 2) Unregister commands/listeners + drop modules
    for name in sorted(modules, key=lambda n: n.count("."), reverse=True):
        mod = modules.get(name) or sys.modules.get(name)
        if mod is None:
            continue
        try:
            sublime_plugin.unload_module(mod)
        except Exception as e:
            dprint(f"unload_module {name}: {e}")
        sys.modules.pop(name, None)

    # 3) Re-import root plugins (ST plugin files) with interception so
    #    fromlist submodules are also reloaded, not restored stale.
    try:
        with intercepting_imports(modules, verbose), importing_fromlist_aggressively(modules):
            loaded = reload_root_plugins(pkg_name, verbose=verbose)
    except Exception as e:
        dprint("reload failed — restoring missing modules")
        for name, mod in modules.items():
            if name not in sys.modules:
                sys.modules[name] = mod
        if verbose:
            dprint("end (FAILED)", "-" * 40)
        return {"ok": False, "error": str(e), "mode": "soft"}

    # 4) Dummy package poke so ST re-runs package load bookkeeping
    if dummy:
        try:
            load_dummy(verbose=verbose)
        except Exception as e:
            dprint(f"dummy package: {e}")

    n_after = len(package_modules(pkg_name))
    if verbose:
        dprint(f"end soft reload — {n_after} module(s)", "-" * 40)

    return {
        "ok": True,
        "mode": "soft",
        "unloaded": len(modules),
        "reloaded_plugins": loaded,
        "modules_after": n_after,
    }


def reload_root_plugins(pkg_name: str = PKG, verbose: bool = True) -> list:
    """Reload every top-level .py in the package (ST plugin entry points)."""
    pkg_path = os.path.join(os.path.realpath(sublime.packages_path()), pkg_name)
    plugins = []
    if not os.path.isdir(pkg_path):
        raise FileNotFoundError(pkg_path)
    for file_path in sorted(os.listdir(pkg_path)):
        if not file_path.endswith(".py"):
            continue
        if file_path.startswith("."):
            continue
        # Skip pure CLI entry that is not a ST plugin surface
        if file_path in ("devtools_cli.py",):
            continue
        plugins.append(f"{pkg_name}.{os.path.splitext(file_path)[0]}")

    loaded = []
    for plugin in plugins:
        if verbose:
            dprint("reload_plugin", plugin)
        try:
            sublime_plugin.reload_plugin(plugin)
            loaded.append(plugin)
        except Exception as e:
            dprint(f"reload_plugin {plugin} failed: {e}")
            loaded.append(f"{plugin} ERROR: {e}")
    return loaded


def load_dummy(verbose: bool = True) -> None:
    """Touch a temporary top-level plugin so ST flushes plugin load state."""
    if verbose:
        dprint("installing dummy package")
    dummy = "_claude_reload_dummy"
    dummy_py = os.path.join(sublime.packages_path(), f"{dummy}.py")
    with open(dummy_py, "w") as f:
        f.write("# temporary — package reload poke\n")

    done = threading.Event()

    def remove_dummy(trial: int = 0):
        if dummy in sys.modules or trial > 5:
            try:
                if os.path.exists(dummy_py):
                    os.unlink(dummy_py)
            except OSError:
                pass
            if verbose:
                dprint("removing dummy package")
            # After remove, ST unloads dummy — enough of a poke
            done.set()
        elif trial < 50:
            sublime.set_timeout(lambda: remove_dummy(trial + 1), 50)
        else:
            try:
                if os.path.exists(dummy_py):
                    os.unlink(dummy_py)
            except OSError:
                pass
            done.set()

    sublime.set_timeout(lambda: remove_dummy(0), 100)
    # Don't block main thread with wait — schedule only. Callers already async.


def hard_reload(pkg_name: str = PKG, reenable_ms: int = 900) -> dict:
    """Full ST package cycle: add to ignored_packages, then remove.

    This is the most complete unload ST supports (commands, menus, settings watchers).
    """
    prefs = sublime.load_settings("Preferences.sublime-settings")
    ignored = list(prefs.get("ignored_packages") or [])
    already = pkg_name in ignored
    if not already:
        ignored.append(pkg_name)
        prefs.set("ignored_packages", ignored)
        sublime.save_settings("Preferences.sublime-settings")
        dprint(f"hard reload: disabled {pkg_name}")

    def reenable():
        cur = list(prefs.get("ignored_packages") or [])
        if pkg_name in cur:
            cur.remove(pkg_name)
            prefs.set("ignored_packages", cur)
            sublime.save_settings("Preferences.sublime-settings")
            dprint(f"hard reload: re-enabled {pkg_name}")

    sublime.set_timeout(reenable, max(200, int(reenable_ms)))
    return {
        "ok": True,
        "mode": "hard",
        "was_ignored": already,
        "reenable_ms": reenable_ms,
        "note": "package disabled; re-enable scheduled on main thread",
    }


def schedule_reload(mode: str = "soft", delay_ms: int = 80, **kwargs) -> dict:
    """Schedule reload after the current MCP/eval response finishes.

    Running reload inside the eval stack would tear down the executing module.
    """
    mode = (mode or "soft").lower()
    result_holder = {"scheduled": True, "mode": mode}

    def run():
        try:
            if mode in ("hard", "full", "ignored"):
                r = hard_reload(PKG, reenable_ms=int(kwargs.get("reenable_ms", 900)))
            else:
                r = reload_package(PKG, dummy=bool(kwargs.get("dummy", True)))
            dprint("scheduled reload result:", r)
            # Mark devtools for re-start (plugin_loaded should also fire)
            try:
                st = getattr(sublime, "_claude_devtools", None)
                if isinstance(st, dict):
                    st["started"] = False
                    st["last_reload"] = r
            except Exception:
                pass
        except Exception as e:
            dprint("scheduled reload FAILED:", e)
            import traceback
            traceback.print_exc()

    sublime.set_timeout(run, max(0, int(delay_ms)))
    result_holder["delay_ms"] = delay_ms
    result_holder["ok"] = True
    result_holder["hint"] = "wait ~1–2s then ping; socket restarts with plugin"
    return result_holder


@contextmanager
def intercepting_imports(modules, verbose):
    finder = _FilterFinder(modules, verbose)
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        if finder in sys.meta_path:
            sys.meta_path.remove(finder)


@contextmanager
def importing_fromlist_aggressively(modules):
    orig = builtins.__import__

    @functools.wraps(orig)
    def __import__(name, globals=None, locals=None, fromlist=(), level=0):
        module = orig(name, globals, locals, fromlist, level)
        if fromlist and module.__name__ in modules:
            fl = list(fromlist)
            if "*" in fl:
                fl.remove("*")
                fl.extend(getattr(module, "__all__", []))
            for x in fl:
                if isinstance(getattr(module, x, None), types.ModuleType):
                    from_name = f"{module.__name__}.{x}"
                    if from_name in modules:
                        importlib.import_module(from_name)
        return module

    builtins.__import__ = __import__
    try:
        yield
    finally:
        builtins.__import__ = orig


class _FilterFinder:
    """Force re-exec of previously loaded package modules via their loader."""

    def __init__(self, modules, verbose):
        self._modules = modules
        self._verbose = verbose

    # py3.4+ importlib API
    def find_module(self, name, path=None):
        if name in self._modules:
            return self
        return None

    def find_spec(self, name, path=None, target=None):
        if name not in self._modules:
            return None
        mod = self._modules[name]
        origin = getattr(mod, "__file__", None)
        if not origin or not os.path.isfile(origin):
            return None
        loader = importlib.machinery.SourceFileLoader(name, origin)
        return importlib.util.spec_from_loader(name, loader)

    def load_module(self, name):
        """Legacy hook — prefer find_spec; keep for older ST embeds."""
        if self._verbose:
            dprint("reloading", "|--", name)
        origin = getattr(self._modules[name], "__file__", None)
        if not origin:
            raise ImportError(name)
        loader = importlib.machinery.SourceFileLoader(name, origin)
        return loader.load_module(name)
