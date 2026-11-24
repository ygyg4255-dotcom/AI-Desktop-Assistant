"""Thread-safe state helper for Altron HUD web interface with live hardware metrics."""
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional, Dict, Any, List

import psutil

from src.config.log import get_logger
from src.config.paths import runtime_file

logger = get_logger(__name__)

STATUS_FILE = runtime_file("status.json")
_LOCK = threading.Lock()

# Authoritative HUD state lives in memory; status.json is only a flushed snapshot.
_STATE: Dict[str, Any] = {"state": "Idle", "updated_at": time.time(), "logs": []}
_MAX_LOGS = 50

_METRICS_CACHE: Dict[str, Any] = {}
_LAST_METRICS_FETCH: float = 0.0
_WARNED: set = set()


def _warn_once(key: str, msg: str, *args: Any) -> None:
    """WARNING the first time a recurring failure happens, DEBUG afterwards (no log spam)."""
    if key in _WARNED:
        logger.debug(msg, *args)
    else:
        _WARNED.add(key)
        logger.warning(msg, *args)


def get_system_metrics() -> Dict[str, Any]:
    """Retrieve live CPU, RAM, Disk, and Battery hardware metrics, cached every 2 seconds."""
    global _METRICS_CACHE, _LAST_METRICS_FETCH
    now = time.time()
    if now - _LAST_METRICS_FETCH < 2.0 and _METRICS_CACHE:
        return dict(_METRICS_CACHE)

    try:
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        batt = psutil.sensors_battery()
        freq = psutil.cpu_freq()

        cpu_temp = None
        try:
            temps = getattr(psutil, "sensors_temperatures", lambda: {})()
            if temps:
                for name, entries in temps.items():
                    if entries:
                        cpu_temp = entries[0].current
                        break
        except Exception as err:
            logger.debug("CPU temperature unavailable: %s", err)

        _METRICS_CACHE = {
            "cpu_percent": round(cpu, 1),
            "cpu_temp": round(cpu_temp, 1) if cpu_temp is not None else (round(freq.current / 100, 1) if freq else 45.0),
            "cpu_freq_mhz": round(freq.current, 0) if freq else 0,
            "ram_used_gb": round(ram.used / (1024**3), 1),
            "ram_total_gb": round(ram.total / (1024**3), 1),
            "ram_percent": round(ram.percent, 1),
            "disk_used_gb": round(disk.used / (1024**3), 1),
            "disk_total_gb": round(disk.total / (1024**3), 1),
            "disk_percent": round(disk.percent, 1),
            "battery_percent": round(batt.percent, 0) if batt else 100,
            "battery_plugged": batt.power_plugged if batt else True,
            "net_online": True,
        }
    except Exception as err:
        _warn_once("metrics", "Could not read system metrics: %s", err)

    _LAST_METRICS_FETCH = now
    return dict(_METRICS_CACHE)


def _flush_locked() -> None:
    """Atomically write the in-memory state to status.json. Caller holds _LOCK."""
    tmp = STATUS_FILE.with_name(STATUS_FILE.name + ".tmp")
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(_STATE, ensure_ascii=False, indent=2), encoding="utf-8")
        # A reader (e.g. the HUD's /status.json) can briefly lock the target on Windows.
        for attempt in range(5):
            try:
                os.replace(tmp, STATUS_FILE)
                return
            except PermissionError:
                time.sleep(0.01 * (attempt + 1))
        _warn_once("flush-locked", "status.json is locked; skipped this snapshot")
        tmp.unlink(missing_ok=True)
    except Exception as err:
        _warn_once("flush", "Could not write status.json: %s", err)
        try:
            tmp.unlink(missing_ok=True)
        except OSError as cleanup_err:
            logger.debug("Could not remove %s: %s", tmp.name, cleanup_err)


def get_hud_status() -> Dict[str, Any]:
    """Current HUD status (from memory) enriched with live hardware metrics."""
    with _LOCK:
        data = dict(_STATE)
        data["logs"] = list(_STATE.get("logs", []))
    metrics = get_system_metrics()
    data["metrics"] = metrics
    # Mirror keys at root level for flexible frontend access
    data.update(metrics)
    return data


def update_hud_status(state: Optional[str] = None, log_entry: Optional[str] = None) -> None:
    """Thread-safe update of HUD state and logs (deduplicating consecutive identical logs)."""
    with _LOCK:
        if state is not None:
            _STATE["state"] = state

        if log_entry:
            entry = log_entry.strip()
            if entry:
                logs: List[str] = _STATE.setdefault("logs", [])
                # Never append identical consecutive entries
                if not logs or logs[-1] != entry:
                    logs.append(entry)
                    _STATE["logs"] = logs[-_MAX_LOGS:]

        _STATE["updated_at"] = time.time()
        _flush_locked()


def push_hud_toast(text: str) -> None:
    """Silent HUD toast: the page shows each new ``toast.id`` once. Also logged to the chat feed."""
    with _LOCK:
        prev = _STATE.get("toast") or {}
        _STATE["toast"] = {"id": int(prev.get("id", 0)) + 1, "text": text.strip()[:300]}
    update_hud_status(log_entry=f"Altron: {text}")


def clear_hud_logs() -> None:
    """Clear conversation history logs."""
    with _LOCK:
        _STATE["logs"] = []
        _STATE["updated_at"] = time.time()
        _flush_locked()


def init_hud_status() -> None:
    """Reset to a clean default state and write it to status.json."""
    with _LOCK:
        _STATE.clear()
        _STATE.update({"state": "Idle", "updated_at": time.time(), "logs": []})
        _flush_locked()
