"""Subscription usage from the local Service Manager (ai/service).

Polls ``GET {quota_service_url}/api/quotas`` (default
``http://127.0.0.1:3001``) and formats compact one-liners for the
"with XXX…" switch-panel detail rows.

Service Manager adapters (Grok Build, Kimi Code, …) own auth and meters;
this module is a thin read-only cache for the Sublime UI.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

# backend name → service provider id
_BACKEND_PROVIDER = {
    "grok": "grok",
    "grok_cc": "grok",
    "kimi": "kimi",
}

_DEFAULT_URL = "http://127.0.0.1:3001"
_CACHE_TTL_SEC = 60.0
_FETCH_TIMEOUT_SEC = 1.5

_lock = threading.Lock()
_cache: Dict[str, Any] = {}  # {fetched_at, by_provider, url}
_inflight = False


def _settings_url() -> str:
    try:
        import sublime  # type: ignore
        s = sublime.load_settings("ClaudeCode.sublime-settings")
        url = (s.get("quota_service_url") or "").strip()
        if url:
            return url.rstrip("/")
    except Exception:
        pass
    return _DEFAULT_URL


def _fetch_sync(url: str) -> Dict[str, dict]:
    """Return {provider_id: provider_dict} or {} on failure."""
    req = urllib.request.Request(
        f"{url}/api/quotas",
        headers={"Accept": "application/json", "User-Agent": "sublime-claude/quota"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8", "replace")
        data = json.loads(raw) if raw.strip() else {}
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError, ValueError):
        return {}
    out: Dict[str, dict] = {}
    for p in data.get("providers") or []:
        if isinstance(p, dict) and p.get("provider"):
            out[str(p["provider"])] = p
    return out


def _ensure_cache(force: bool = False) -> Dict[str, dict]:
    global _inflight
    url = _settings_url()
    now = time.time()
    with _lock:
        if (
            not force
            and _cache.get("url") == url
            and _cache.get("by_provider") is not None
            and (now - float(_cache.get("fetched_at") or 0)) < _CACHE_TTL_SEC
        ):
            return dict(_cache["by_provider"])
        if _inflight and not force:
            return dict(_cache.get("by_provider") or {})

    # Blocking fetch is short-timeout; callers on UI thread should prefer
    # peek / schedule_refresh which stay off the critical path after warm-up.
    by = _fetch_sync(url)
    with _lock:
        _cache.clear()
        _cache.update({
            "fetched_at": time.time(),
            "by_provider": by,
            "url": url,
        })
        _inflight = False
    return dict(by)


def schedule_refresh(force: bool = False) -> None:
    """Background refresh so the next panel open is warm."""
    global _inflight
    url = _settings_url()
    now = time.time()
    with _lock:
        if (
            not force
            and _cache.get("url") == url
            and _cache.get("by_provider") is not None
            and (now - float(_cache.get("fetched_at") or 0)) < _CACHE_TTL_SEC
        ):
            return
        if _inflight:
            return
        _inflight = True

    def _work():
        global _inflight
        try:
            by = _fetch_sync(url)
            with _lock:
                _cache.update({
                    "fetched_at": time.time(),
                    "by_provider": by,
                    "url": url,
                })
        finally:
            with _lock:
                _inflight = False

    threading.Thread(target=_work, daemon=True).start()


def peek_providers() -> Dict[str, dict]:
    """Return cached providers without network I/O (may be empty)."""
    with _lock:
        return dict(_cache.get("by_provider") or {})


def get_providers(force: bool = False) -> Dict[str, dict]:
    """Sync fetch (short timeout). Prefer schedule_refresh + peek for UI."""
    return _ensure_cache(force=force)


def _period_pct(provider: dict) -> Optional[float]:
    meters: List[dict] = provider.get("meters") or []
    for m in meters:
        if m.get("id") != "period_usage":
            continue
        fu = m.get("fraction_used")
        if fu is not None:
            try:
                return min(100.0, max(0.0, 100.0 * float(fu)))
            except (TypeError, ValueError):
                pass
        used, limit = m.get("used"), m.get("limit")
        unit = m.get("unit") or ""
        try:
            if unit == "%" and used is not None:
                return float(used)
            if used is not None and limit:
                return min(100.0, 100.0 * float(used) / float(limit))
        except (TypeError, ValueError):
            pass
    return None


def _eta_short(resets_at: Optional[str]) -> str:
    if not resets_at:
        return ""
    try:
        from datetime import datetime, timezone
        end = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00"))
        secs = int((end - datetime.now(timezone.utc)).total_seconds())
        if secs <= 0:
            return ""
        h, rem = divmod(secs, 3600)
        mins = rem // 60
        if h >= 24:
            return f"{h // 24}d"
        if h > 0:
            return f"{h}h"
        return f"{mins}m"
    except Exception:
        return ""


def format_provider_usage(provider: Optional[dict]) -> str:
    """Compact detail for a service provider payload, or '' if unknown."""
    if not provider:
        return ""
    if not provider.get("ok"):
        err = (provider.get("error") or "error").strip()
        # Keep one short token
        if "Not signed" in err or "not signed" in err:
            return "not signed in"
        if "expired" in err.lower() or "Auth" in err:
            return "auth expired"
        if err == "disabled":
            return "disabled"
        return (err[:28] + "…") if len(err) > 28 else err

    meters: List[dict] = provider.get("meters") or []
    period = next((m for m in meters if m.get("id") == "period_usage"), None)
    rate5h = next((m for m in meters if m.get("id") == "rate_5h"), None)

    parts: List[str] = []
    pct = _period_pct(provider)
    if pct is not None:
        label = "wk" if (period and (period.get("unit") == "requests")) else ""
        if label:
            parts.append(f"{pct:.0f}% {label}")
        else:
            parts.append(f"{pct:.0f}%")
        if period:
            eta = _eta_short(period.get("resets_at"))
            if eta:
                parts.append(eta)

    if rate5h is not None:
        r_pct = None
        fu = rate5h.get("fraction_used")
        try:
            if fu is not None:
                r_pct = min(100.0, max(0.0, 100.0 * float(fu)))
            elif rate5h.get("used") is not None and rate5h.get("limit"):
                r_pct = min(
                    100.0,
                    100.0 * float(rate5h["used"]) / float(rate5h["limit"]),
                )
        except (TypeError, ValueError):
            r_pct = None
        if r_pct is not None:
            parts.append(f"5h {r_pct:.0f}%")

    if not parts:
        return "ok"
    return " · ".join(parts)


def usage_detail_for_backend(backend: str, fallback: str = "") -> str:
    """Detail line for a switch-panel backend using cached service data."""
    provider_id = _BACKEND_PROVIDER.get(backend)
    if not provider_id:
        return fallback
    by = peek_providers()
    if not by:
        # Cold cache: one short sync attempt so the first panel open can
        # show usage; still fails soft if Service Manager is down.
        by = get_providers(force=False)
    else:
        # Keep cache warm for the next open without blocking this one.
        schedule_refresh(force=False)
    text = format_provider_usage(by.get(provider_id))
    return text or fallback


def warm_cache() -> None:
    """Call on plugin load / panel open to prefetch."""
    if peek_providers():
        schedule_refresh(force=False)
    else:
        # Prefer a quick sync warm so the following rows already have data.
        try:
            get_providers(force=False)
        except Exception:
            schedule_refresh(force=True)
