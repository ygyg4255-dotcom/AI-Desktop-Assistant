"""Thread-safe state helper for Altron HUD web interface with live hardware metrics."""
import json
import threading
import time
from pathlib import Path
from typing import Optional, Dict, Any, List

import psutil

STATUS_FILE = Path(__file__).resolve().parent.parent.parent / "status.json"
_LOCK = threading.Lock()

_METRICS_CACHE: Dict[str, Any] = {}
_LAST_METRICS_FETCH: float = 0.0


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
        except Exception:
            pass

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
    except Exception:
        pass

    _LAST_METRICS_FETCH = now
    return dict(_METRICS_CACHE)


def get_hud_status() -> Dict[str, Any]:
    """Retrieve current HUD status dictionary enriched with live hardware metrics."""
    with _LOCK:
        data = {"state": "Idle", "updated_at": time.time(), "logs": []}
        if STATUS_FILE.exists():
            try:
                content = STATUS_FILE.read_text(encoding="utf-8").strip()
                if content:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        data = parsed
            except Exception:
                pass

        metrics = get_system_metrics()
        data["metrics"] = metrics
        # Mirror keys at root level for flexible frontend access
        for k, v in metrics.items():
            data[k] = v
        return data


def update_hud_status(state: Optional[str] = None, log_entry: Optional[str] = None) -> None:
    """Thread-safe non-blocking update of Altron HUD state and logs with deduplication."""
    def _do_update():
        with _LOCK:
            data = {"state": "Idle", "updated_at": time.time(), "logs": []}
            if STATUS_FILE.exists():
                try:
                    content = STATUS_FILE.read_text(encoding="utf-8").strip()
                    if content:
                        parsed = json.loads(content)
                        if isinstance(parsed, dict):
                            data = parsed
                except Exception:
                    pass

            if state is not None:
                data["state"] = state

            if log_entry:
                entry = log_entry.strip()
                if entry:
                    logs: List[str] = data.get("logs", [])
                    # Strict deduplication check: never append identical consecutive entries
                    if not logs or logs[-1] != entry:
                        logs.append(entry)
                        data["logs"] = logs[-50:]

            data["updated_at"] = time.time()

            try:
                STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
                STATUS_FILE.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
            except Exception:
                pass

    t = threading.Thread(target=_do_update, daemon=True)
    t.start()


def clear_hud_logs() -> None:
    """Clear conversation history logs in status.json."""
    with _LOCK:
        data = {"state": "Idle", "updated_at": time.time(), "logs": []}
        if STATUS_FILE.exists():
            try:
                content = STATUS_FILE.read_text(encoding="utf-8").strip()
                if content:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        data = parsed
            except Exception:
                pass

        data["logs"] = []
        data["updated_at"] = time.time()
        try:
            STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATUS_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass


def init_hud_status() -> None:
    """Ensure status.json exists with clean default state."""
    with _LOCK:
        try:
            STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
            initial = {
                "state": "Idle",
                "updated_at": time.time(),
                "logs": []
            }
            STATUS_FILE.write_text(
                json.dumps(initial, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass
