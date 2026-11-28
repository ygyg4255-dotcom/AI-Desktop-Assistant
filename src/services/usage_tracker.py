"""Daily Anthropic token/cost tracker: one small JSON file, thread-safe, atomic writes.

File shape: {"YYYY-MM-DD": {"<model>": {calls, input, cache_create, cache_read, output, cost}}}
"""
import json
import os
import threading
from datetime import date, timedelta
from typing import Any, Dict, Optional

from src.config.log import get_logger
from src.config.paths import runtime_file
from src.config.settings import get_config_value

logger = get_logger(__name__)

# USD per million tokens: (input, output). Cache write = 1.25x input, cache read = 0.1x input.
# shortcut: list prices as of writing, update when pricing changes; unknown models use the Sonnet row.
PRICES = {"opus": (15.0, 75.0), "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0)}
_lock = threading.Lock()
_warned_day = ""


def _price(model: str) -> tuple:
    m = (model or "").lower()
    return next((p for k, p in PRICES.items() if k in m), PRICES["sonnet"])


def estimate_cost(model: str, inp: int, cache_create: int, cache_read: int, out: int) -> float:
    pin, pout = _price(model)
    return (inp * pin + cache_create * pin * 1.25 + cache_read * pin * 0.1 + out * pout) / 1_000_000


def _load() -> Dict[str, Any]:
    try:
        data = json.loads(runtime_file("usage_stats.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        logger.warning("usage_stats.json unreadable, starting fresh: %s", err)
        return {}


def _budget() -> float:
    try:
        return float(get_config_value("ALTRON_DAILY_BUDGET_USD", default="0") or 0)
    except ValueError:
        return 0.0


def today_cost(data: Optional[Dict[str, Any]] = None, day: Optional[str] = None) -> float:
    data = _load() if data is None else data
    return sum(m.get("cost", 0.0) for m in data.get(day or date.today().isoformat(), {}).values())


def budget_notice(data: Optional[Dict[str, Any]] = None) -> str:
    budget = _budget()
    spent = today_cost(data)
    if budget > 0 and spent > budget:
        return f"Budget notice: today's estimated API spend ${spent:.2f} exceeds the daily budget ${budget:.2f}."
    return ""


def record(model: str, usage: Dict[str, Any]) -> None:
    """Add one API call to today's totals. Never raises (tracking must not break a reply)."""
    global _warned_day
    try:
        def n(key: str) -> int:
            try:
                return int(usage.get(key) or 0)
            except (TypeError, ValueError):
                return 0
        inp, cc, cr, out = (n("input_tokens"), n("cache_creation_input_tokens"),
                            n("cache_read_input_tokens"), n("output_tokens"))
        day = date.today().isoformat()
        with _lock:
            data = _load()
            row = data.setdefault(day, {}).setdefault(
                model or "unknown", {"calls": 0, "input": 0, "cache_create": 0, "cache_read": 0, "output": 0, "cost": 0.0})
            row["calls"] += 1
            row["input"] += inp
            row["cache_create"] += cc
            row["cache_read"] += cr
            row["output"] += out
            row["cost"] += estimate_cost(model, inp, cc, cr, out)
            path = runtime_file("usage_stats.json")
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, path)
            notice = budget_notice(data)
        if notice and _warned_day != day:
            _warned_day = day
            logger.warning(notice)
    except Exception:
        logger.debug("usage tracking failed", exc_info=True)


def summarize(days: int = 1) -> Dict[str, Any]:
    """Per-model totals over the last ``days`` days (1 = today) plus cache hit rate and cost."""
    days = max(1, min(int(days or 1), 365))
    data = _load()
    wanted = {(date.today() - timedelta(d)).isoformat() for d in range(days)}
    models: Dict[str, Dict[str, Any]] = {}
    for day in wanted & data.keys():
        for model, r in data[day].items():
            t = models.setdefault(model, {"calls": 0, "input": 0, "cache_create": 0, "cache_read": 0, "output": 0, "cost": 0.0})
            for k in t:
                t[k] += r.get(k, 0)
    for t in models.values():
        prompt = t["input"] + t["cache_create"] + t["cache_read"]
        t["cache_hit_rate"] = f"{(t['cache_read'] / prompt * 100) if prompt else 0:.0f}%"
        t["cost_usd"] = round(t.pop("cost"), 4)
    out: Dict[str, Any] = {"days": days, "models": models,
                           "total_cost_usd": round(sum(t["cost_usd"] for t in models.values()), 4)}
    notice = budget_notice(data)
    if notice:
        out["notice"] = notice
    return out
