"""Altron Sci-Fi Radar HUD Web Server & Standalone Desktop Window Host.

Security model (audit S1/S2):
- A random per-launch token is embedded only in the served HUD page and must be
  sent as X-HUD-Token on every API call (constant-time compared).
- Host must be 127.0.0.1:<port> / localhost:<port>; Origin, when present, must
  be the HUD's own origin. No CORS headers are ever sent.
- State-changing JSON endpoints require Content-Type: application/json.
"""
import os
import sys
import hmac
import json
import secrets
import socket
import shutil
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional, Callable, Any, Dict

from flask import Flask, send_from_directory, Response, request, jsonify

from src.config.settings import get_active_config_dict, get_config_value, is_masked_key, save_ai_config
from src.services.hud_status import get_hud_status, clear_hud_logs

BASE_DIR = Path(__file__).resolve().parent
UI_DIR = BASE_DIR / "ui"

# Per-launch secret, delivered only inside the served index.html.
_HUD_TOKEN: str = secrets.token_urlsafe(32)
_TOKEN_PLACEHOLDER = "__HUD_TOKEN__"
# Port the server is bound to; set by start_hud_server (used for Host checks).
_HUD_PORT: int = 5000

_API_PATHS = {"/status", "/status.json", "/config", "/test_config", "/command", "/clear_logs",
              "/approvals", "/approve"}
_JSON_ONLY_PATHS = {"/command", "/config", "/test_config", "/approve"}
_CONFIG_FIELDS = {"api_key", "model", "language"}

# Registered callbacks
_COMMAND_HANDLER: Optional[Callable[[str], None]] = None
_CONFIG_UPDATED_CALLBACK: Optional[Callable[[Dict[str, Any]], None]] = None
# ApprovalManager (src.agent.approvals) used by /approvals and /approve.
_APPROVAL_MANAGER: Any = None


def set_approval_manager(manager: Any) -> None:
    """Register the ApprovalManager whose pending requests the HUD can resolve."""
    global _APPROVAL_MANAGER
    _APPROVAL_MANAGER = manager


def set_command_handler(handler: Callable[[str], None]) -> None:
    """Register callback to handle interactive commands submitted from HUD."""
    global _COMMAND_HANDLER
    _COMMAND_HANDLER = handler


def set_config_updated_callback(cb: Callable[[Dict[str, Any]], None]) -> None:
    """Register callback triggered when AI configuration is updated via HUD modal."""
    global _CONFIG_UPDATED_CALLBACK
    _CONFIG_UPDATED_CALLBACK = cb


def get_active_config() -> Dict[str, Any]:
    """Retrieve current AI configuration with masked API key."""
    return get_active_config_dict()


def _get_status_payload_str() -> str:
    """Retrieve JSON string with status and live hardware metrics."""
    data = get_hud_status()
    # Enrich with active AI configuration
    cfg = get_active_config()
    data["ai_config"] = cfg
    return json.dumps(data, ensure_ascii=False)


def _get_default_browser() -> tuple[Optional[str], Optional[str]]:
    """Retrieve default browser ProgId and executable path from Windows registry."""
    if sys.platform != "win32":
        return None, None
    try:
        import winreg
        import shlex

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice"
        ) as key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command") as cmd_key:
            cmd, _ = winreg.QueryValueEx(cmd_key, "")
            parts = shlex.split(cmd, posix=False)
            exe = parts[0].strip('"') if parts else ""
            return prog_id, exe
    except Exception:
        return None, None


def _is_chromium_browser(prog_id: Optional[str], exe_path: Optional[str]) -> bool:
    """Check if the browser is Chromium-based (supports native --app mode)."""
    p = (prog_id or "").lower()
    e = (exe_path or "").lower()
    chromium_keywords = (
        "chrome", "edge", "brave", "vivaldi", "opera", "arc", "chromium", "centbrowser", "yandex"
    )
    return any(k in p or k in e for k in chromium_keywords)


def open_hud_window(url: str) -> None:
    """Launch HUD in the user's actual Windows default browser using --app mode if Chromium-based."""
    # 1. Detect actual default browser on Windows
    prog_id, exe_path = _get_default_browser()

    # 2. If default browser is Chromium-based, launch with --app mode
    if exe_path and os.path.isfile(exe_path) and _is_chromium_browser(prog_id, exe_path):
        try:
            subprocess.Popen([
                exe_path,
                f"--app={url}",
                "--window-size=1360,820",
                "--window-name=A.L.T.R.O.N"
            ])
            return
        except Exception:
            pass

    # 3. If not Chromium-based (e.g. Firefox) or resolution fails, open directly in default browser
    webbrowser.open(url)


# --- Flask application ---
# Flask is a hard requirement (requirements.txt): there is exactly one, hardened
# request path and no http.server fallback.
flask_app = Flask(__name__, static_folder=None)


def _allowed_hosts() -> set:
    return {f"127.0.0.1:{_HUD_PORT}", f"localhost:{_HUD_PORT}"}


def _error(code: int, message: str) -> Response:
    resp = jsonify({"error": message})
    resp.status_code = code
    return resp


@flask_app.before_request
def _enforce_hud_security():
    """Host / Origin / token / content-type gate applied to every request."""
    host = request.headers.get("Host", "").lower()
    if host not in _allowed_hosts():
        return _error(403, "forbidden host")

    origin = request.headers.get("Origin")
    if origin is not None and origin.lower() != f"http://{host}":
        return _error(403, "forbidden origin")

    if request.path in _API_PATHS:
        supplied = request.headers.get("X-HUD-Token", "")
        if not hmac.compare_digest(supplied.encode("utf-8"), _HUD_TOKEN.encode("utf-8")):
            return _error(401, "missing or invalid HUD token")
        if (
            request.method == "POST"
            and request.path in _JSON_ONLY_PATHS
            and request.mimetype != "application/json"
        ):
            return _error(415, "Content-Type must be application/json")
    return None


@flask_app.after_request
def _security_headers(resp: Response) -> Response:
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    if request.path in _API_PATHS:
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


def _json_object() -> Optional[Dict[str, Any]]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _validate_config_payload(data: Dict[str, Any]) -> Optional[str]:
    """Return an error message, or None if the payload is acceptable."""
    unknown = set(data) - _CONFIG_FIELDS
    if unknown:
        return f"unsupported field(s): {', '.join(sorted(unknown))}"
    for key in _CONFIG_FIELDS:
        value = data.get(key, "")
        if not isinstance(value, str):
            return f"{key} must be a string"
        if "\r" in value or "\n" in value:
            return f"{key} must not contain line breaks"
    api_key = data.get("api_key", "")
    # Masked/empty values mean "keep the stored key"; anything else must be a real key.
    if not is_masked_key(api_key):
        if not api_key.isascii() or any(c.isspace() for c in api_key.strip()):
            return "api_key must contain only ASCII characters and no whitespace"
    return None


@flask_app.route("/")
@flask_app.route("/index.html")
def index():
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    resp = Response(html.replace(_TOKEN_PLACEHOLDER, _HUD_TOKEN), mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@flask_app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(UI_DIR, filename)


@flask_app.route("/status")
@flask_app.route("/status.json")
def status_route():
    return Response(_get_status_payload_str(), mimetype="application/json")


@flask_app.route("/config", methods=["GET", "POST"])
def config_route():
    if request.method == "GET":
        return jsonify(get_active_config())

    data = _json_object()
    if data is None:
        return _error(400, "body must be a JSON object")
    problem = _validate_config_payload(data)
    if problem:
        return _error(400, problem)

    try:
        save_ai_config(
            model=data.get("model", ""),
            api_key=data.get("api_key", "").strip(),
            language=data.get("language", "English"),
        )
    except OSError:
        return _error(500, "could not write settings to .env")

    updated = get_active_config()
    if _CONFIG_UPDATED_CALLBACK:
        try:
            _CONFIG_UPDATED_CALLBACK(updated)
        except Exception as err:
            print(f"[Config Callback Error] {err}")
    return jsonify(updated)


@flask_app.route("/test_config", methods=["POST"])
def test_config_route():
    data = _json_object()
    if data is None:
        return _error(400, "body must be a JSON object")
    problem = _validate_config_payload(data)
    if problem:
        return _error(400, problem)

    model = data.get("model", "")
    api_key = data.get("api_key", "").strip()
    # Masked or empty key: test the stored one (canonical precedence, settings.py).
    if is_masked_key(api_key):
        api_key = get_config_value("AI_API_KEY", "ANTHROPIC_API_KEY")

    try:
        from src.services.ai_client import AIClient
        test_client = AIClient(api_key=api_key, model=model, timeout=6.0)
        ok, msg = test_client.test_connection()
        return jsonify({"ok": ok, "message": msg})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@flask_app.route("/command", methods=["POST"])
def command_route():
    data = _json_object()
    if data is None:
        return _error(400, "body must be a JSON object")
    cmd = data.get("command", "")
    if not isinstance(cmd, str):
        return _error(400, "command must be a string")
    cmd = cmd.strip()

    if cmd and _COMMAND_HANDLER:
        threading.Thread(target=_COMMAND_HANDLER, args=(cmd,), daemon=True).start()
    return jsonify({"status": "ok", "command": cmd})


@flask_app.route("/clear_logs", methods=["POST"])
def clear_logs_route():
    clear_hud_logs()
    return jsonify({"status": "ok"})


@flask_app.route("/approvals", methods=["GET"])
def approvals_route():
    pending = _APPROVAL_MANAGER.list_pending() if _APPROVAL_MANAGER is not None else []
    return jsonify({"pending": pending})


# outcome -> HTTP status for POST /approve
_APPROVE_STATUS = {
    "approved": 200, "denied": 200,
    "expired": 409, "already_used": 409,
    "hash_mismatch": 409, "forbidden_channel": 403, "unknown": 404,
}


@flask_app.route("/approve", methods=["POST"])
def approve_route():
    if _APPROVAL_MANAGER is None:
        return _error(503, "approvals are not available")
    data = _json_object()
    if data is None:
        return _error(400, "body must be a JSON object")
    if set(data) - {"id", "decision", "args_hash"}:
        return _error(400, "unsupported field(s)")
    req_id, decision, digest = data.get("id"), data.get("decision"), data.get("args_hash")
    if not isinstance(req_id, str) or not isinstance(digest, str) or decision not in ("approve", "deny"):
        return _error(400, "id and args_hash must be strings; decision must be 'approve' or 'deny'")

    result = _APPROVAL_MANAGER.resolve(req_id, approve=(decision == "approve"), via="hud", expected_hash=digest)
    resp = jsonify({"status": result.outcome})
    resp.status_code = _APPROVE_STATUS.get(result.outcome, 400)
    return resp


def _is_port_in_use(host: str, port: int) -> bool:
    """Check if a TCP port is currently in use."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            return s.connect_ex((host, port)) == 0
    except Exception:
        return False


def start_hud_server(
    host: str = "127.0.0.1",
    port: int = 5000,
    open_browser: bool = False,
    command_handler: Optional[Callable[[str], None]] = None,
    on_config_updated: Optional[Callable[[Dict[str, Any]], None]] = None,
    approval_manager: Any = None,
) -> threading.Thread:
    """Start the Altron HUD server in a background daemon thread with safe port fallback."""
    if command_handler:
        set_command_handler(command_handler)
    if approval_manager is not None:
        set_approval_manager(approval_manager)
    if on_config_updated:
        set_config_updated_callback(on_config_updated)

    global _HUD_PORT
    actual_port = port
    if _is_port_in_use(host, actual_port):
        fallback_port = actual_port + 1
        print(f"[ALTRON HUD] Port {actual_port} is already in use; falling back to port {fallback_port}")
        actual_port = fallback_port
    _HUD_PORT = actual_port

    def _run():
        import logging
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        flask_app.run(host=host, port=actual_port, debug=False, use_reloader=False)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    if open_browser:
        def _open():
            time.sleep(0.8)
            open_hud_window(f"http://{host}:{actual_port}")
        threading.Thread(target=_open, daemon=True).start()

    print(f"[ALTRON HUD] Radar interface online at http://{host}:{actual_port}")
    return thread


if __name__ == "__main__":
    server_thread = start_hud_server(host="127.0.0.1", port=5000, open_browser=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[ALTRON HUD] Server stopped by user.")
