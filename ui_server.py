"""Altron Sci-Fi Radar HUD Web Server & Standalone Desktop Window Host."""
import os
import sys
import json
import socket
import shutil
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional, Callable, Any, Dict

BASE_DIR = Path(__file__).resolve().parent
UI_DIR = BASE_DIR / "ui"
STATUS_FILE = BASE_DIR / "status.json"

# Import settings and hud helpers
try:
    from src.config.settings import Settings, save_ai_config
except Exception:
    Settings = None
    save_ai_config = None

try:
    from src.services.hud_status import get_hud_status, clear_hud_logs
except Exception:
    def get_hud_status():
        if STATUS_FILE.exists():
            try:
                return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"state": "Idle", "updated_at": time.time(), "logs": []}

    def clear_hud_logs():
        pass

# Registered callbacks
_COMMAND_HANDLER: Optional[Callable[[str], None]] = None
_CONFIG_UPDATED_CALLBACK: Optional[Callable[[Dict[str, Any]], None]] = None


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
    if Settings is not None:
        try:
            return Settings().get_config_dict()
        except Exception:
            pass
    return {
        "provider": os.environ.get("AI_PROVIDER", "anthropic"),
        "model": os.environ.get("AI_MODEL", "claude-3-5-sonnet-20241022"),
        "base_url": os.environ.get("AI_BASE_URL", "https://api.anthropic.com"),
        "api_key": "••••••••" if os.environ.get("AI_API_KEY") else "",
        "has_api_key": bool(os.environ.get("AI_API_KEY")),
        "language": os.environ.get("ALTRON_LANGUAGE", "English"),
    }


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


# --- Flask Implementation ---
try:
    from flask import Flask, send_from_directory, Response, request, jsonify

    flask_app = Flask(__name__, static_folder=str(UI_DIR))

    @flask_app.route("/")
    def index():
        return send_from_directory(UI_DIR, "index.html")

    @flask_app.route("/<path:filename>")
    def static_files(filename):
        return send_from_directory(UI_DIR, filename)

    @flask_app.route("/status")
    @flask_app.route("/status.json")
    def status_route():
        payload = _get_status_payload_str()
        resp = Response(payload, mimetype="application/json")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    @flask_app.route("/config", methods=["GET", "POST", "OPTIONS"])
    def config_route():
        if request.method == "OPTIONS":
            resp = Response()
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            return resp

        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            provider = data.get("provider", "anthropic")
            model = data.get("model", "")
            api_key = data.get("api_key", "").strip()
            base_url = data.get("base_url", "")
            language = data.get("language", "English")

            if save_ai_config:
                save_ai_config(provider, model, api_key, base_url, language)

            updated = get_active_config()
            if _CONFIG_UPDATED_CALLBACK:
                try:
                    _CONFIG_UPDATED_CALLBACK(updated)
                except Exception as err:
                    print(f"[Config Callback Error] {err}")

            resp = jsonify(updated)
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp

        # GET method
        resp = jsonify(get_active_config())
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    @flask_app.route("/test_config", methods=["POST", "OPTIONS"])
    def test_config_route():
        if request.method == "OPTIONS":
            resp = Response()
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            return resp

        data = request.get_json(silent=True) or {}
        provider = data.get("provider", "anthropic")
        model = data.get("model", "")
        base_url = data.get("base_url", "")
        api_key = data.get("api_key", "").strip()

        # If key is masked or empty, look up stored key
        if not api_key or any(c in api_key for c in ("•", "*")):
            api_key = os.environ.get("AI_API_KEY") or (Settings().ai_api_key if Settings else "")

        try:
            from src.services.ai_client import AIClient
            test_client = AIClient(
                provider=provider,
                model=model,
                base_url=base_url,
                api_key=api_key,
                timeout=6.0
            )
            ok, msg = test_client.test_connection()
            resp = jsonify({"ok": ok, "message": msg})
        except Exception as e:
            resp = jsonify({"ok": False, "message": str(e)})

        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    @flask_app.route("/command", methods=["POST", "OPTIONS"])
    def command_route():
        if request.method == "OPTIONS":
            resp = Response()
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            return resp

        data = request.get_json(silent=True) or {}
        cmd = data.get("command", "").strip()
        if not cmd and request.form:
            cmd = request.form.get("command", "").strip()

        if cmd and _COMMAND_HANDLER:
            threading.Thread(target=_COMMAND_HANDLER, args=(cmd,), daemon=True).start()

        resp = jsonify({"status": "ok", "command": cmd})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    @flask_app.route("/clear_logs", methods=["POST", "OPTIONS"])
    def clear_logs_route():
        if request.method == "OPTIONS":
            resp = Response()
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            return resp

        clear_hud_logs()
        resp = jsonify({"status": "ok"})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    _HAS_FLASK = True

except ImportError:
    _HAS_FLASK = False
    flask_app = None


# --- Zero-Dependency http.server Fallback ---
if not _HAS_FLASK:
    from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

    class HUDRequestHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(UI_DIR), **kwargs)

        def _send_json(self, data: Any, code: int = 200):
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(payload)

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()

        def do_GET(self):
            clean_path = self.path.split("?")[0]
            if clean_path in ("/status", "/status.json"):
                payload = _get_status_payload_str().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(payload)
            elif clean_path == "/config":
                self._send_json(get_active_config())
            elif clean_path in ("/", ""):
                self.path = "/index.html"
                return super().do_GET()
            else:
                return super().do_GET()

        def do_POST(self):
            clean_path = self.path.split("?")[0]
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b""

            if clean_path == "/command":
                cmd = ""
                try:
                    parsed = json.loads(body.decode("utf-8"))
                    cmd = parsed.get("command", "").strip()
                except Exception:
                    pass

                if cmd and _COMMAND_HANDLER:
                    threading.Thread(target=_COMMAND_HANDLER, args=(cmd,), daemon=True).start()

                self._send_json({"status": "ok", "command": cmd})

            elif clean_path == "/config":
                data = {}
                try:
                    data = json.loads(body.decode("utf-8"))
                except Exception:
                    pass

                provider = data.get("provider", "anthropic")
                model = data.get("model", "")
                api_key = data.get("api_key", "")
                base_url = data.get("base_url", "")
                language = data.get("language", "English")

                if save_ai_config:
                    save_ai_config(provider, model, api_key, base_url, language)

                updated = get_active_config()
                if _CONFIG_UPDATED_CALLBACK:
                    try:
                        _CONFIG_UPDATED_CALLBACK(updated)
                    except Exception as err:
                        print(f"[Config Callback Error] {err}")

                self._send_json(updated)

            elif clean_path == "/test_config":
                data = {}
                try:
                    data = json.loads(body.decode("utf-8"))
                except Exception:
                    pass

                provider = data.get("provider", "anthropic")
                model = data.get("model", "")
                base_url = data.get("base_url", "")
                api_key = data.get("api_key", "").strip()

                if not api_key or any(c in api_key for c in ("•", "*")):
                    api_key = os.environ.get("AI_API_KEY") or (Settings().ai_api_key if Settings else "")

                try:
                    from src.services.ai_client import AIClient
                    test_client = AIClient(
                        provider=provider,
                        model=model,
                        base_url=base_url,
                        api_key=api_key,
                        timeout=6.0
                    )
                    ok, msg = test_client.test_connection()
                    self._send_json({"ok": ok, "message": msg})
                except Exception as e:
                    self._send_json({"ok": False, "message": str(e)})

            elif clean_path == "/clear_logs":
                clear_hud_logs()
                self._send_json({"status": "ok"})
            else:
                self.send_error(404, "Endpoint not found")

        def log_message(self, format, *args):
            pass


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
) -> threading.Thread:
    """Start the Altron HUD server in a background daemon thread with safe port fallback."""
    if command_handler:
        set_command_handler(command_handler)
    if on_config_updated:
        set_config_updated_callback(on_config_updated)

    actual_port = port
    if _is_port_in_use(host, actual_port):
        fallback_port = actual_port + 1
        print(f"[ALTRON HUD] Port {actual_port} is already in use; falling back to port {fallback_port}")
        actual_port = fallback_port

    def _run():
        if _HAS_FLASK and flask_app is not None:
            import logging
            log = logging.getLogger("werkzeug")
            log.setLevel(logging.ERROR)
            flask_app.run(host=host, port=actual_port, debug=False, use_reloader=False)
        else:
            server = ThreadingHTTPServer((host, actual_port), HUDRequestHandler)
            server.serve_forever()

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
