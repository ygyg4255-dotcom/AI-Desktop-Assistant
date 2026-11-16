"""Tool Registry for AI Desktop Assistant.

Implements a structured tool system inspired by Claude Code's tool use pattern.
"""
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass
from enum import Enum
import os
import shutil
import subprocess
import platform
import re
import urllib.request
import urllib.parse
from pathlib import Path
import html
from src.services.telegram_service import send_telegram_message, send_telegram_message_async, send_telegram_document, send_telegram_photo_async
from src.config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


class ToolCategory(Enum):
    """Categories for organizing tools."""
    FILE_SYSTEM = "file_system"
    TERMINAL = "terminal"
    SYSTEM = "system"
    BROWSER = "browser"
    CODING = "coding"
    UTILITY = "utility"


@dataclass
class ToolParameter:
    """Represents a tool parameter definition."""
    name: str
    type: str  # "string", "number", "boolean", "array", "object"
    description: str
    required: bool = True
    default: Any = None
    enum: List[str] = None


@dataclass
class ToolResult:
    """Result of a tool execution."""
    success: bool
    output: Any
    error: Optional[str] = None
    requires_permission: bool = False
    permission_message: str = ""
    is_dangerous: bool = False
    message: Optional[str] = None

    def __post_init__(self):
        if self.message and isinstance(self.output, dict) and "message" not in self.output:
            self.output["message"] = self.message


@dataclass
class Tool:
    """Represents a registered tool."""
    name: str
    description: str
    category: ToolCategory
    parameters: List[ToolParameter]
    handler: Callable[[Dict[str, Any]], ToolResult]
    requires_permission: bool = True
    is_dangerous: bool = False

    def to_api_schema(self) -> Dict[str, Any]:
        """Convert to Anthropic tool use schema format."""
        properties = {}
        required = []

        for param in self.parameters:
            prop = {
                "type": param.type,
                "description": param.description
            }
            if param.enum:
                prop["enum"] = param.enum
            if param.default is not None:
                prop["default"] = param.default
            properties[param.name] = prop

            if param.required:
                required.append(param.name)

        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required
            }
        }


class ToolRegistry:
    """Registry for managing available tools."""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> List[Tool]:
        """List all registered tools."""
        return list(self._tools.values())

    def get_tools_schema(self) -> List[Dict[str, Any]]:
        """Get Anthropic-compatible tool schemas for all registered tools."""
        return [tool.to_api_schema() for tool in self._tools.values()]

    def get_tools_by_category(self, category: ToolCategory) -> List[Tool]:
        """Get tools by category."""
        return [t for t in self._tools.values() if t.category == category]

    def execute(self, name: str, **kwargs) -> ToolResult:
        """Execute a tool by name with arguments."""
        tool = self.get(name)
        if not tool:
            return ToolResult(
                success=False,
                output=None,
                error=f"Tool not found: {name}"
            )
        if len(kwargs) == 1 and "params" in kwargs and isinstance(kwargs["params"], dict):
            params = kwargs["params"]
        elif len(kwargs) == 1 and "input" in kwargs and isinstance(kwargs["input"], dict):
            params = kwargs["input"]
        else:
            params = kwargs
        return tool.handler(params)


# === Built-in Tool Handlers ===

def handle_file_search(params: Dict[str, Any]) -> ToolResult:
    """Handle file search tool."""
    query = params.get("query", "")
    directory = params.get("directory", str(Path.home()))

    try:
        results = []
        search_dir = Path(directory).expanduser()

        if not search_dir.exists():
            return ToolResult(
                success=False,
                output=None,
                error=f"Directory not found: {directory}"
            )

        for item in search_dir.rglob("*"):
            if query.lower() in item.name.lower():
                results.append(str(item))
                if len(results) >= 20:  # Limit results
                    break

        return ToolResult(
            success=True,
            output={"files": results, "count": len(results)}
        )
    except Exception as e:
        return ToolResult(
            success=False,
            output=None,
            error=str(e)
        )


def handle_file_read(params: Dict[str, Any]) -> ToolResult:
    """Handle file read tool."""
    file_path = params.get("path", "")
    max_lines = params.get("max_lines", 100)

    try:
        path = Path(file_path).expanduser()
        if not path.exists():
            return ToolResult(
                success=False,
                output=None,
                error=f"File not found: {file_path}"
            )

        if not path.is_file():
            return ToolResult(
                success=False,
                output=None,
                error=f"Not a file: {file_path}"
            )

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = []
            for i, line in enumerate(f):
                if i >= max_lines:
                    lines.append(f"... (truncated, {max_lines} lines shown)")
                    break
                lines.append(line.rstrip("\n"))

        return ToolResult(
            success=True,
            output={"content": "\n".join(lines), "path": str(path)}
        )
    except Exception as e:
        return ToolResult(
            success=False,
            output=None,
            error=str(e)
        )


def handle_file_write(params: Dict[str, Any]) -> ToolResult:
    """Handle file write tool - requires permission."""
    file_path = params.get("path", "")
    content = params.get("content", "")

    path = Path(file_path).expanduser()
    exists = path.exists()

    if not params.get("_approved"):
        return ToolResult(
            success=False,
            output=None,
            requires_permission=True,
            is_dangerous=True,
            permission_message=f"{'Overwrite' if exists else 'Create'} file: {file_path} ({len(content.encode('utf-8'))} bytes)"
        )

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return ToolResult(
            success=True,
            output={
                "path": str(path),
                "bytes_written": len(content.encode("utf-8")),
                "action": "overwritten" if exists else "created"
            }
        )
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def handle_file_delete(params: Dict[str, Any]) -> ToolResult:
    """Handle file delete tool - requires permission."""
    file_path = params.get("path", "")
    path = Path(file_path).expanduser()

    if not params.get("_approved"):
        return ToolResult(
            success=False,
            output=None,
            requires_permission=True,
            is_dangerous=True,
            permission_message=f"Delete file or directory: {file_path}"
        )

    if not path.exists():
        return ToolResult(success=False, output=None, error=f"File not found: {file_path}")

    try:
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        return ToolResult(
            success=True,
            output={"deleted": str(path), "message": "Deleted successfully"}
        )
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def handle_file_copy(params: Dict[str, Any]) -> ToolResult:
    """Handle file or directory copy."""
    src_str = params.get("source", "")
    dst_str = params.get("destination", "")

    if not src_str or not dst_str:
        return ToolResult(success=False, output=None, error="Both 'source' and 'destination' are required")

    src = Path(src_str).expanduser()
    dst = Path(dst_str).expanduser()

    if not src.exists():
        return ToolResult(success=False, output=None, error=f"Source path not found: {src_str}")

    try:
        if dst.is_dir():
            dst = dst / src.name
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)

        if src.is_file():
            shutil.copy2(src, dst)
        else:
            shutil.copytree(src, dst, dirs_exist_ok=True)

        return ToolResult(
            success=True,
            output={"source": str(src), "destination": str(dst), "message": "Copied successfully"}
        )
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def handle_file_move(params: Dict[str, Any]) -> ToolResult:
    """Handle file or directory move/rename - requires permission."""
    src_str = params.get("source", "")
    dst_str = params.get("destination", "")

    if not src_str or not dst_str:
        return ToolResult(success=False, output=None, error="Both 'source' and 'destination' are required")

    src = Path(src_str).expanduser()
    dst = Path(dst_str).expanduser()

    if not params.get("_approved"):
        return ToolResult(
            success=False,
            output=None,
            requires_permission=True,
            is_dangerous=True,
            permission_message=f"Move/rename: {src_str} -> {dst_str}"
        )

    if not src.exists():
        return ToolResult(success=False, output=None, error=f"Source path not found: {src_str}")

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return ToolResult(
            success=True,
            output={"source": str(src), "destination": str(dst), "message": "Moved successfully"}
        )
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def handle_file_open(params: Dict[str, Any]) -> ToolResult:
    """Open a file with its default Windows application."""
    path_str = params.get("path", "")
    if not path_str:
        return ToolResult(success=False, output=None, error="Path is required")

    path = Path(path_str).expanduser()
    if not path.exists():
        return ToolResult(success=False, output=None, error=f"File not found: {path_str}")

    try:
        if platform.system() == "Windows":
            os.startfile(str(path))
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

        return ToolResult(
            success=True,
            output={"path": str(path), "message": f"Opened {path.name} in default application"}
        )
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def handle_terminal_command(params: Dict[str, Any]) -> ToolResult:
    """Handle terminal command execution - requires permission."""
    command = params.get("command", "")
    shell = params.get("shell", "powershell").lower()

    if not params.get("_approved"):
        return ToolResult(
            success=False,
            output=None,
            requires_permission=True,
            is_dangerous=True,
            permission_message=f"Execute {shell} command: {command[:100]}{'...' if len(command) > 100 else ''}"
        )

    try:
        if shell == "cmd" or platform.system() != "Windows":
            cmd_args = ["cmd.exe", "/c", command] if platform.system() == "Windows" else ["/bin/sh", "-c", command]
        else:
            cmd_args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]

        proc = subprocess.run(
            cmd_args,
            capture_output=True,
            text=True,
            timeout=params.get("timeout", 60)
        )
        return ToolResult(
            success=(proc.returncode == 0),
            output={
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
                "exit_code": proc.returncode
            },
            error=proc.stderr.strip() if proc.returncode != 0 else None
        )
    except subprocess.TimeoutExpired:
        return ToolResult(success=False, output=None, error="Command execution timed out (60s limit)")
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def handle_app_open(params: Dict[str, Any]) -> ToolResult:
    """Handle application open tool (non-blocking)."""
    app_name = params.get("app_name", "").strip()
    app_path = params.get("path", "").strip()

    if not app_name and not app_path:
        return ToolResult(
            success=False,
            output=None,
            error="Either app_name or path is required"
        )

    try:
        target = app_path or app_name
        if platform.system() == "Windows":
            try:
                os.startfile(target)
            except Exception:
                if app_path:
                    subprocess.Popen(
                        [app_path],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        shell=True
                    )
                else:
                    subprocess.Popen(
                        f'start "" "{app_name}"',
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        shell=True
                    )
        elif platform.system() == "Darwin":
            subprocess.Popen(
                ["open", target],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True
            )
        else:
            subprocess.Popen(
                ["xdg-open", target],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True
            )

        return ToolResult(
            success=True,
            output={"message": f"Opened: {app_name or app_path}"}
        )
    except Exception as e:
        return ToolResult(
            success=False,
            output=None,
            error=str(e)
        )


def handle_app_close(params: Dict[str, Any]) -> ToolResult:
    """Handle application close tool - requires permission."""
    import psutil

    app_name = params.get("app_name", "")
    process_id = params.get("pid")
    target = f"process ID {process_id}" if process_id else app_name

    if not params.get("_approved"):
        return ToolResult(
            success=False,
            output=None,
            requires_permission=True,
            is_dangerous=True,
            permission_message=f"Close application: {target}"
        )

    closed_count = 0
    try:
        if process_id:
            p = psutil.Process(int(process_id))
            p.terminate()
            closed_count += 1
        elif app_name:
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    if app_name.lower() in p.info["name"].lower():
                        p.terminate()
                        closed_count += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

        return ToolResult(
            success=True,
            output={"closed": target, "count": closed_count, "message": f"Terminated {closed_count} process(es)"}
        )
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def handle_browser_open(params: Dict[str, Any]) -> ToolResult:
    """Handle browser open tool (non-blocking)."""
    url = params.get("url", "").strip()
    if not url:
        return ToolResult(
            success=False,
            output=None,
            error="URL is required"
        )

    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    try:
        if platform.system() == "Windows":
            try:
                os.startfile(url)
            except Exception:
                import webbrowser
                webbrowser.open(url)
        elif platform.system() == "Darwin":
            subprocess.Popen(
                ["open", url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True
            )
        else:
            subprocess.Popen(
                ["xdg-open", url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True
            )

        return ToolResult(
            success=True,
            output={"message": f"Opened browser: {url}"}
        )
    except Exception as e:
        return ToolResult(
            success=False,
            output=None,
            error=str(e)
        )


def handle_process_list(params: Dict[str, Any]) -> ToolResult:
    """Handle process list tool."""
    import psutil

    try:
        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info']):
            try:
                processes.append({
                    'pid': proc.info['pid'],
                    'name': proc.info['name'],
                    'cpu': proc.info['cpu_percent'] or 0,
                    'memory_mb': proc.info['memory_info'].rss / 1024 / 1024 if proc.info['memory_info'] else 0
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        processes.sort(key=lambda x: x['memory_mb'], reverse=True)

        return ToolResult(
            success=True,
            output={"processes": processes[:50], "count": len(processes)}
        )
    except Exception as e:
        return ToolResult(
            success=False,
            output=None,
            error=str(e)
        )


def handle_system_info(params: Dict[str, Any]) -> ToolResult:
    """Handle system info tool."""
    import psutil

    try:
        info = {
            "os": platform.system(),
            "os_version": platform.version(),
            "python_version": platform.python_version(),
            "cpu_count": psutil.cpu_count(),
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "memory_total_gb": psutil.virtual_memory().total / 1024 / 1024 / 1024,
            "memory_available_gb": psutil.virtual_memory().available / 1024 / 1024 / 1024,
            "memory_percent": psutil.virtual_memory().percent,
            "disk_total_gb": psutil.disk_usage('C:\\' if platform.system() == 'Windows' else '/').total / 1024 / 1024 / 1024,
            "disk_percent": psutil.disk_usage('C:\\' if platform.system() == 'Windows' else '/').percent
        }

        return ToolResult(
            success=True,
            output=info
        )
    except Exception as e:
        return ToolResult(
            success=False,
            output=None,
            error=str(e)
        )


def handle_window_control(params: Dict[str, Any]) -> ToolResult:
    """Handle window management tool (list, focus, minimize, maximize)."""
    import platform
    if platform.system() != "Windows":
        return ToolResult(
            success=False,
            output=None,
            error="Window control is only supported on Windows operating systems"
        )

    action = params.get("action", "list").lower()
    target_title = params.get("title", "").strip().lower()
    target_hwnd = params.get("hwnd")

    valid_actions = ("list", "focus", "minimize", "maximize")
    if action not in valid_actions:
        return ToolResult(
            success=False,
            output=None,
            error=f"Unsupported action: '{action}'. Choose from 'list', 'focus', 'minimize', 'maximize'."
        )

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        if action == "list":
            windows = []
            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def enum_proc(hwnd, lParam):
                if user32.IsWindowVisible(hwnd):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buff = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buff, length + 1)
                        title = buff.value.strip()
                        if title and title != "Program Manager":
                            windows.append({"hwnd": hwnd, "title": title})
                return True

            user32.EnumWindows(WNDENUMPROC(enum_proc), 0)

            if target_title:
                windows = [w for w in windows if target_title in w["title"].lower()]

            return ToolResult(
                success=True,
                output={"windows": windows, "count": len(windows)}
            )

        # Find target hwnd
        hwnd = None
        if target_hwnd is not None:
            try:
                hwnd = int(target_hwnd)
            except ValueError:
                return ToolResult(success=False, output=None, error=f"Invalid hwnd: {target_hwnd}")
        elif target_title:
            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            candidates = []

            def find_proc(h, lParam):
                if user32.IsWindowVisible(h):
                    length = user32.GetWindowTextLengthW(h)
                    if length > 0:
                        buff = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(h, buff, length + 1)
                        title = buff.value.strip()
                        if target_title in title.lower():
                            candidates.append((h, title))
                return True

            user32.EnumWindows(WNDENUMPROC(find_proc), 0)
            if candidates:
                hwnd = candidates[0][0]

        if not hwnd or not user32.IsWindow(hwnd):
            return ToolResult(
                success=False,
                output=None,
                error=f"Target window not found matching '{target_title or target_hwnd}'"
            )

        SW_MINIMIZE = 6
        SW_MAXIMIZE = 3
        SW_RESTORE = 9

        if action == "focus":
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
            return ToolResult(
                success=True,
                output={"action": "focus", "hwnd": hwnd, "message": "Window brought to foreground"}
            )
        elif action == "minimize":
            user32.ShowWindow(hwnd, SW_MINIMIZE)
            return ToolResult(
                success=True,
                output={"action": "minimize", "hwnd": hwnd, "message": "Window minimized"}
            )
        elif action == "maximize":
            user32.ShowWindow(hwnd, SW_MAXIMIZE)
            return ToolResult(
                success=True,
                output={"action": "maximize", "hwnd": hwnd, "message": "Window maximized"}
            )
        else:
            return ToolResult(
                success=False,
                output=None,
                error=f"Unsupported action: '{action}'. Choose from 'list', 'focus', 'minimize', 'maximize'."
            )
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def find_matching_window(query: str) -> Optional[tuple]:
    """Find visible top-level window HWND and title matching query."""
    import win32gui
    import win32process
    import difflib

    query_raw = (query or "").strip().lower()
    if not query_raw or query_raw in ("active", "current", "foreground", "النشطة", "الحالية", "المفتوحة حاليا"):
        fg = win32gui.GetForegroundWindow()
        if fg and win32gui.IsWindowVisible(fg):
            return fg, win32gui.GetWindowText(fg)
        return None

    candidates = []

    def enum_cb(hwnd, lparam):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd).strip()
            if title and title not in ("Default IME", "MSCTFIME UI", "Program Manager", "Windows Shell Experience Host"):
                proc_name = ""
                try:
                    import psutil
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    if pid:
                        proc_name = psutil.Process(pid).name().lower()
                except Exception:
                    pass
                candidates.append((hwnd, title, proc_name))
        return True

    win32gui.EnumWindows(enum_cb, None)

    # 1. Exact or substring match in window title
    for hwnd, title, proc_name in candidates:
        if query_raw in title.lower():
            return hwnd, title

    # 2. Match against process name (e.g. "brave", "chrome", "edge", "code", "vscode")
    for hwnd, title, proc_name in candidates:
        clean_proc = proc_name.replace(".exe", "")
        if query_raw in clean_proc or clean_proc in query_raw:
            return hwnd, title

    # 3. Keyword extraction (ignoring Arabic/English stop words)
    stop_words = {
        "نافذة", "نافذه", "المتصفح", "متصفح", "المفتوح", "مفتوح", "المفتوحة", "مفتوحة",
        "على", "علي", "موقع", "صفحة", "برنامج", "تطبيق", "في", "من", "الشاشة", "شاشة",
        "window", "the", "browser", "open", "opened", "app", "application", "on", "at", "to", "site", "webpage", "screen"
    }
    tokens = [w for w in re.split(r'[\s\-_]+', query_raw) if w and w not in stop_words and len(w) > 1]
    for token in tokens:
        for hwnd, title, proc_name in candidates:
            if token in title.lower() or token in proc_name:
                return hwnd, title

    # 4. If query mentions browser ("متصفح" / "browser"), match any open browser window
    if any(b in query_raw for b in ("متصفح", "المتصفح", "browser", "chrome", "brave", "edge", "firefox")):
        browser_names = ("brave", "chrome", "msedge", "edge", "firefox")
        for hwnd, title, proc_name in candidates:
            if any(b in proc_name for b in browser_names) or any(b in title.lower() for b in browser_names):
                return hwnd, title

    # 5. Fuzzy match on title
    for hwnd, title, proc_name in candidates:
        if difflib.SequenceMatcher(None, query_raw, title.lower()).ratio() > 0.45:
            return hwnd, title

    return None


def handle_move_application_window(params: Dict[str, Any]) -> ToolResult:
    """Move, snap, maximize, or minimize an open application window on the screen."""
    window_query = str(params.get("window_query", "")).strip()
    position = str(params.get("position", "right")).strip().lower()

    if not window_query:
        return ToolResult(
            success=False,
            output=None,
            error="Window query is required (e.g. 'omniroute', 'brave', 'chrome', 'vscode', or 'المتصفح')"
        )

    try:
        import win32gui
        import win32con
        import ctypes

        found = find_matching_window(window_query)
        if not found:
            return ToolResult(
                success=False,
                output={"window_query": window_query},
                error=f"Could not find any open visible window matching '{window_query}'"
            )

        hwnd, title = found

        # Screen dimensions
        user32 = ctypes.windll.user32
        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)
        # Usable height excluding taskbar
        work_h = screen_h - 40

        pos_norm = position.replace(" ", "_").replace("-", "_")

        # Minimize
        if pos_norm in ("minimize", "تصغير", "اخفاء", "صغر"):
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            return ToolResult(
                success=True,
                output={
                    "window_title": title,
                    "position": "minimize",
                    "message": f"✅ تم تصغير نافذة '{title}' إلى شريط المهام."
                }
            )

        # Maximize
        if pos_norm in ("maximize", "تكبير", "كامل_الشاشة", "ملء_الشاشة", "كبر"):
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            return ToolResult(
                success=True,
                output={
                    "window_title": title,
                    "position": "maximize",
                    "message": f"✅ تم تكبير نافذة '{title}' على كامل الشاشة."
                }
            )

        # Restore window if minimized/maximized before positioning
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

        # Compute position coordinates
        if pos_norm in ("right", "يمين", "اليمين", "لليمين", "في_يمين_الشاشة", "يمين_الشاشة"):
            x = screen_w // 2
            y = 0
            w = screen_w // 2
            h = work_h
            pos_label = "right side of the screen"
            pos_ar = "يمين الشاشة"
        elif pos_norm in ("left", "يسار", "اليسار", "لليسار", "شمال", "الشمال", "في_يسار_الشاشة", "يسار_الشاشة"):
            x = 0
            y = 0
            w = screen_w // 2
            h = work_h
            pos_label = "left side of the screen"
            pos_ar = "يسار الشاشة"
        elif pos_norm in ("top", "اعلى", "أعلى", "فوق"):
            x = 0
            y = 0
            w = screen_w
            h = work_h // 2
            pos_label = "top half of the screen"
            pos_ar = "أعلى الشاشة"
        elif pos_norm in ("bottom", "اسفل", "أسفل", "تحت"):
            x = 0
            y = work_h // 2
            w = screen_w
            h = work_h // 2
            pos_label = "bottom half of the screen"
            pos_ar = "أسفل الشاشة"
        elif pos_norm in ("top_right", "أعلى_اليمين", "اعلى_اليمين"):
            x = screen_w // 2
            y = 0
            w = screen_w // 2
            h = work_h // 2
            pos_label = "top right corner"
            pos_ar = "أعلى يمين الشاشة"
        elif pos_norm in ("top_left", "أعلى_اليسار", "اعلى_اليسار"):
            x = 0
            y = 0
            w = screen_w // 2
            h = work_h // 2
            pos_label = "top left corner"
            pos_ar = "أعلى يسار الشاشة"
        elif pos_norm in ("bottom_right", "أسفل_اليمين", "اسفل_اليمين"):
            x = screen_w // 2
            y = work_h // 2
            w = screen_w // 2
            h = work_h // 2
            pos_label = "bottom right corner"
            pos_ar = "أسفل يمين الشاشة"
        elif pos_norm in ("bottom_left", "أسفل_اليسار", "اسفل_اليسار"):
            x = 0
            y = work_h // 2
            w = screen_w // 2
            h = work_h // 2
            pos_label = "bottom left corner"
            pos_ar = "أسفل يسار الشاشة"
        else:  # center / middle / default
            w = int(screen_w * 0.7)
            h = int(screen_h * 0.7)
            x = (screen_w - w) // 2
            y = (work_h - h) // 2
            pos_label = "center of the screen"
            pos_ar = "وسط الشاشة"

        win32gui.MoveWindow(hwnd, x, y, w, h, True)

        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass

        return ToolResult(
            success=True,
            output={
                "window_title": title,
                "position": pos_norm,
                "coordinates": {"x": x, "y": y, "width": w, "height": h},
                "message": f"✅ تم تحريك نافذة '{title}' إلى {pos_ar} بنجاح!"
            }
        )
    except Exception as e:
        return ToolResult(
            success=False,
            output=None,
            error=f"Failed to move application window: {e}"
        )


def handle_desktop_icon_position(params: Dict[str, Any]) -> ToolResult:
    """Handle visual positioning of desktop icons on the Windows screen."""
    target = params.get("target", "").strip()
    position = params.get("position", "center")
    x = params.get("x")
    y = params.get("y")

    # Strict safety net: If target mentions a window, browser, or app, redirect to handle_move_application_window
    if any(w in target.lower() for w in ("نافذة", "نافذه", "متصفح", "المتصفح", "برنامج", "window", "browser", "omniroute", "chrome", "brave")):
        return handle_move_application_window({"window_query": target, "position": position})

    if not target:
        return ToolResult(
            success=False,
            output=None,
            error="Target icon/file name is required (e.g. 'her', 'main', 'PlayerMusic', or 'all')"
        )

    try:
        from src.services.desktop_icon_service import position_desktop_icon
        res = position_desktop_icon(target=target, position=position, x=x, y=y)
        if res.get("success"):
            return ToolResult(success=True, output=res)
        else:
            return ToolResult(success=False, output=None, error=res.get("error", "Failed to move desktop icon"))
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def handle_code_edit(params: Dict[str, Any]) -> ToolResult:
    """Handle targeted code search and replace within a file."""
    path_str = params.get("path", "")
    old_string = params.get("old_string", "")
    new_string = params.get("new_string", "")
    replace_all = params.get("replace_all", False)

    if not path_str:
        return ToolResult(success=False, output=None, error="File path is required")
    if not old_string:
        return ToolResult(success=False, output=None, error="Target old_string is required")

    path = Path(path_str).expanduser().resolve()
    if not path.exists():
        return ToolResult(success=False, output=None, error=f"File not found: {path}")
    if not path.is_file():
        return ToolResult(success=False, output=None, error=f"Path is not a regular file: {path}")

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        return ToolResult(success=False, output=None, error=f"Could not read file: {e}")

    occurrences = content.count(old_string)
    if occurrences == 0:
        return ToolResult(
            success=False,
            output=None,
            error=f"Target old_string not found in {path.name}"
        )

    if not replace_all and occurrences > 1:
        return ToolResult(
            success=False,
            output=None,
            error=f"Target old_string occurs {occurrences} times in {path.name}. Specify more unique context or set replace_all=True."
        )

    # Permission check for dangerous modification
    if not params.get("_approved", False):
        return ToolResult(
            success=False,
            output=None,
            requires_permission=True,
            is_dangerous=True,
            permission_message=f"Edit {path.name}: replace {occurrences} occurrence(s) ({len(old_string)} chars -> {len(new_string)} chars)"
        )

    try:
        if replace_all:
            updated_content = content.replace(old_string, new_string)
            count = occurrences
        else:
            updated_content = content.replace(old_string, new_string, 1)
            count = 1

        path.write_text(updated_content, encoding="utf-8")

        return ToolResult(
            success=True,
            output={
                "path": str(path),
                "occurrences_replaced": count,
                "message": f"Successfully replaced {count} occurrence(s) in {path.name}"
            }
        )
    except Exception as e:
        return ToolResult(success=False, output=None, error=f"Failed to write changes: {e}")


def handle_web_fetch(params: Dict[str, Any]) -> ToolResult:
    """Fetch and extract clean readable text from a URL."""
    import urllib.request
    import re
    import html

    url = params.get("url", "").strip()
    max_chars = params.get("max_chars", 4000)

    if not url:
        return ToolResult(success=False, output=None, error="URL is required")

    # If it doesn't have scheme, default to https
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.7",
        "Accept-Language": "en-US,en;q=0.9"
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as response:
            content_type = response.headers.get("Content-Type", "")
            raw_bytes = response.read()

            charset = "utf-8"
            if "charset=" in content_type:
                charset = content_type.split("charset=")[-1].split(";")[0].strip()

            try:
                raw_text = raw_bytes.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                raw_text = raw_bytes.decode("utf-8", errors="replace")

        # Strip scripts, styles, comments, noscript
        text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", raw_text)
        text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
        text = re.sub(r"(?is)<!--.*?-->", " ", text)
        text = re.sub(r"(?is)<noscript[^>]*>.*?</noscript>", " ", text)

        # Replace tags with space or newline for block elements
        text = re.sub(r"(?i)<(p|div|br|h[1-6]|li|tr)[^>]*>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)

        # Unescape HTML entities
        text = html.unescape(text)

        # Collapse whitespace
        lines = []
        for line in text.splitlines():
            cleaned_line = re.sub(r"[ \t]+", " ", line).strip()
            if cleaned_line:
                lines.append(cleaned_line)
        cleaned_text = "\n".join(lines)

        truncated = len(cleaned_text) > max_chars
        output_text = cleaned_text[:max_chars]

        return ToolResult(
            success=True,
            output={
                "url": url,
                "text": output_text,
                "length": len(output_text),
                "truncated": truncated
            }
        )
    except Exception as e:
        return ToolResult(
            success=False,
            output=None,
            error=f"Web fetch failed for '{url}': {str(e)}"
        )


def handle_system_control(params: Dict[str, Any]) -> ToolResult:
    """Instant Windows system controls for volume, brightness, and settings."""
    action = params.get("action", "").strip().lower()
    value = params.get("value")

    valid_actions = {
        "volume_up", "volume_down", "volume_mute", "set_volume",
        "brightness_up", "brightness_down", "set_brightness",
        "open_settings"
    }
    if action not in valid_actions:
        return ToolResult(
            success=False,
            output=None,
            error=f"Unsupported action '{action}'. Valid actions: {', '.join(sorted(valid_actions))}"
        )

    if platform.system() != "Windows":
        return ToolResult(
            success=False,
            output=None,
            error="system_control is only supported on Windows"
        )

    try:
        import ctypes
        user32 = ctypes.windll.user32

        # 1. Volume actions (<15ms via keybd_event)
        if action in ("volume_up", "volume_down", "volume_mute", "set_volume"):
            VK_VOLUME_MUTE = 0xAD
            VK_VOLUME_DOWN = 0xAE
            VK_VOLUME_UP = 0xAF
            KEYEVENTF_EXTENDEDKEY = 0x0001
            KEYEVENTF_KEYUP = 0x0002

            def _press(vk: int):
                user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
                user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)

            if action == "volume_mute":
                _press(VK_VOLUME_MUTE)
                return ToolResult(success=True, output={"action": action, "message": "Volume muted/unmuted"})
            elif action == "set_volume":
                target = max(0, min(100, int(round(float(value if value is not None else 50)))))
                # Zero out volume first (50 down presses = -100%)
                for _ in range(50):
                    _press(VK_VOLUME_DOWN)
                # Step up to target (each step is 2%)
                up_steps = round(target / 2)
                for _ in range(up_steps):
                    _press(VK_VOLUME_UP)
                return ToolResult(
                    success=True,
                    output={"action": action, "volume": target, "message": f"Volume set to {target}%"}
                )
            elif action == "volume_up":
                steps = max(1, int(value)) if value is not None else 3
                for _ in range(steps):
                    _press(VK_VOLUME_UP)
                return ToolResult(success=True, output={"action": action, "steps": steps, "message": f"Volume increased by {steps * 2}%"})
            elif action == "volume_down":
                steps = max(1, int(value)) if value is not None else 3
                for _ in range(steps):
                    _press(VK_VOLUME_DOWN)
                return ToolResult(success=True, output={"action": action, "steps": steps, "message": f"Volume decreased by {steps * 2}%"})

        # 2. Brightness actions (Fast WMI)
        elif action in ("brightness_up", "brightness_down", "set_brightness"):
            def _get_brightness() -> int:
                try:
                    import win32com.client
                    wmi = win32com.client.GetObject('winmgmts:\\\\.\\root\\wmi')
                    b_list = list(wmi.InstancesOf('WmiMonitorBrightness'))
                    if b_list:
                        return int(b_list[0].CurrentBrightness)
                except Exception:
                    pass
                try:
                    res = subprocess.run(
                        ['powershell', '-NoProfile', '-NonInteractive', '-Command', '(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightness).CurrentBrightness'],
                        capture_output=True, text=True, timeout=3,
                        stdin=subprocess.DEVNULL
                    )
                    if res.returncode == 0 and res.stdout.strip().isdigit():
                        return int(res.stdout.strip())
                except Exception:
                    pass
                return 50

            def _set_brightness(level: int):
                level = max(0, min(100, int(level)))
                # Try COM first (<20ms)
                try:
                    import win32com.client
                    wmi = win32com.client.GetObject('winmgmts:\\\\.\\root\\wmi')
                    methods = list(wmi.InstancesOf('WmiMonitorBrightnessMethods'))
                    if methods:
                        m = methods[0]
                        in_param = m.Methods_('WmiSetBrightness').inParameters.SpawnInstance_()
                        in_param.Properties_('Timeout').Value = 1
                        in_param.Properties_('Brightness').Value = level
                        m.ExecMethod_('WmiSetBrightness', in_param)
                        return True, level
                except Exception:
                    pass
                # Fallback to powershell WmiSetBrightness
                try:
                    subprocess.run(
                        ['powershell', '-NoProfile', '-NonInteractive', '-Command', f'(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1, {level})'],
                        capture_output=True, timeout=5,
                        stdin=subprocess.DEVNULL
                    )
                    return True, level
                except Exception as e:
                    return False, str(e)

            current = _get_brightness()
            if action == "brightness_up":
                step = int(value) if value is not None else 10
                new_level = min(100, current + step)
            elif action == "brightness_down":
                step = int(value) if value is not None else 10
                new_level = max(0, current - step)
            else:  # set_brightness
                new_level = max(0, min(100, int(value if value is not None else 50)))

            ok, res_level = _set_brightness(new_level)
            if ok:
                return ToolResult(success=True, output={"action": action, "brightness": res_level, "message": f"Brightness set to {res_level}%"})
            else:
                return ToolResult(success=False, output=None, error=f"Could not adjust brightness: {res_level}")

        # 3. Open settings (Instant non-blocking)
        elif action == "open_settings":
            os.startfile("ms-settings:")
            return ToolResult(success=True, output={"action": action, "message": "Windows Settings opened"})

    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def handle_youtube_play(params: Dict[str, Any]) -> ToolResult:
    """Search YouTube for a query and immediately play the first matching video directly."""
    query = params.get("query", "").strip()
    if not query:
        return ToolResult(success=False, output=None, error="Query parameter is required")

    search_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(query)}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    video_url = None
    video_id = None
    try:
        req = urllib.request.Request(search_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode("utf-8", errors="ignore")
            matches = re.findall(r'/watch\?v=([a-zA-Z0-9_-]{11})', html)
            if matches:
                video_id = matches[0]
                video_url = f"https://www.youtube.com/watch?v={video_id}"
    except Exception:
        pass

    target_url = video_url or search_url
    try:
        if platform.system() == "Windows":
            os.startfile(target_url)
        elif platform.system() == "Darwin":
            subprocess.Popen(
                ["open", target_url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True
            )
        else:
            subprocess.Popen(
                ["xdg-open", target_url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True
            )

        return ToolResult(
            success=True,
            output={
                "query": query,
                "video_id": video_id,
                "url": target_url,
                "message": f"Playing '{query}' directly on YouTube" if video_id else f"Opened YouTube search for '{query}'"
            }
        )
    except Exception as e:
        return ToolResult(
            success=False,
            output=None,
            error=f"Failed to play YouTube video: {e}"
        )


def extract_youtube_video_id(url_or_query: str) -> Optional[str]:
    """Extract 11-character YouTube video ID if URL or ID is passed."""
    patterns = [
        r'(?:v=|\/v\/|youtu\.be\/|\/embed\/|\/shorts\/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$'
    ]
    for pattern in patterns:
        m = re.search(pattern, url_or_query.strip())
        if m:
            return m.group(1)
    return None


def fetch_youtube_transcript_text(video_id: str, languages: tuple = ("ar", "en", "fr")) -> str:
    """Fetch complete spoken transcript text from YouTube using youtube_transcript_api."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return ""

    # 1. Try classmethod get_transcript (older versions)
    if hasattr(YouTubeTranscriptApi, "get_transcript"):
        try:
            items = YouTubeTranscriptApi.get_transcript(video_id, languages=list(languages))
            if items:
                return " ".join(item.get("text", "") for item in items if item.get("text"))
        except Exception:
            pass

    # 2. Try modern instance/class fetch
    try:
        api = YouTubeTranscriptApi() if callable(YouTubeTranscriptApi) else YouTubeTranscriptApi
        fetched = None
        for lang in languages:
            try:
                fetched = api.fetch(video_id, languages=(lang,))
                if fetched:
                    break
            except Exception:
                continue

        if not fetched:
            try:
                fetched = api.fetch(video_id, languages=languages)
            except Exception:
                pass

        if fetched:
            if hasattr(fetched, "snippets"):
                return " ".join(s.text for s in fetched.snippets if getattr(s, "text", ""))
            elif hasattr(fetched, "to_raw_data"):
                return " ".join(item.get("text", "") for item in fetched.to_raw_data() if item.get("text"))
            elif isinstance(fetched, list):
                return " ".join(item.get("text", "") for item in fetched if item.get("text"))
    except Exception:
        pass

    # 3. Try listing and finding generated or manual transcripts
    try:
        api = YouTubeTranscriptApi() if callable(YouTubeTranscriptApi) else YouTubeTranscriptApi
        list_fn = getattr(api, "list", None) or getattr(YouTubeTranscriptApi, "list_transcripts", None)
        if list_fn:
            t_list = list_fn(video_id)
            chosen = None
            try:
                chosen = t_list.find_transcript(list(languages))
            except Exception:
                try:
                    chosen = t_list.find_generated_transcript(list(languages))
                except Exception:
                    for tr in t_list:
                        chosen = tr
                        break

            if chosen:
                data = chosen.fetch()
                if hasattr(data, "snippets"):
                    return " ".join(s.text for s in data.snippets if getattr(s, "text", ""))
                elif hasattr(data, "to_raw_data"):
                    return " ".join(item.get("text", "") for item in data.to_raw_data() if item.get("text"))
                elif isinstance(data, list):
                    return " ".join(item.get("text", "") for item in data if item.get("text"))
    except Exception:
        pass

    return ""


def handle_youtube_play_and_summarize(params: Dict[str, Any]) -> ToolResult:
    """Search for or open a YouTube video, optionally play it in browser, and fetch transcript to summarize."""
    query_or_url = params.get("query_or_url", "").strip()
    if not query_or_url:
        return ToolResult(success=False, output=None, error="Parameter 'query_or_url' is required")

    play = params.get("play", False)
    if isinstance(play, str):
        play = play.lower() in ("true", "1", "yes")

    summarize = params.get("summarize", True)
    if isinstance(summarize, str):
        summarize = summarize.lower() in ("true", "1", "yes")

    video_id = extract_youtube_video_id(query_or_url)
    video_title = ""
    video_url = ""
    video_description = ""

    # If it's a direct URL or ID
    if video_id:
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        # Extract title using yt-dlp quickly
        try:
            import yt_dlp
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=False)
                if info:
                    video_title = info.get("title", "")
                    video_description = (info.get("description") or "")[:2000]
        except Exception:
            video_title = f"YouTube Video ({video_id})"
    else:
        # Search query: use yt-dlp ytsearch1
        try:
            import yt_dlp
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
                'extract_flat': 'in_playlist',
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{query_or_url}", download=False)
                if info and "entries" in info and info["entries"]:
                    entry = info["entries"][0]
                    video_id = entry.get("id")
                    video_title = entry.get("title", query_or_url)
                    video_url = entry.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}"
                elif info:
                    video_id = info.get("id")
                    video_title = info.get("title", query_or_url)
                    video_url = info.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}"
        except Exception:
            pass

        # Fallback to web search regex if yt-dlp was unavailable
        if not video_id:
            try:
                search_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(query_or_url)}"
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36"}
                req = urllib.request.Request(search_url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as response:
                    html_content = response.read().decode("utf-8", errors="ignore")
                    matches = re.findall(r'/watch\?v=([a-zA-Z0-9_-]{11})', html_content)
                    if matches:
                        video_id = matches[0]
                        video_url = f"https://www.youtube.com/watch?v={video_id}"
                        video_title = query_or_url
            except Exception:
                pass

    if not video_url:
        return ToolResult(
            success=False,
            output=None,
            error=f"Could not find any YouTube video for '{query_or_url}'"
        )

    # 1. Play in browser if requested
    if play:
        try:
            import webbrowser
            webbrowser.open(video_url)
        except Exception:
            try:
                if platform.system() == "Windows":
                    os.startfile(video_url)
            except Exception:
                pass

    # 2. Extract transcript if summarize requested
    transcript_text = ""
    if summarize and video_id:
        transcript_text = fetch_youtube_transcript_text(video_id)

        # Fallback to yt-dlp description/captions if transcript API returned empty
        if not transcript_text:
            try:
                import yt_dlp
                ydl_opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'skip_download': True,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(video_url, download=False)
                    if info:
                        if not video_title:
                            video_title = info.get("title", "")
                        desc = info.get("description", "")
                        if desc:
                            transcript_text = f"[Video Description & Summary Context]:\n{desc[:3000]}"
            except Exception:
                pass

    # Truncate overly long transcripts to prevent exceeding token limits (keep up to 14,000 chars)
    if transcript_text and len(transcript_text) > 14000:
        transcript_text = transcript_text[:14000] + "... [transcript truncated for brevity]"

    output_data = {
        "title": video_title or query_or_url,
        "url": video_url,
        "video_id": video_id,
        "played_in_browser": play,
        "transcript_available": bool(transcript_text),
        "transcript": transcript_text if transcript_text else "Spoken transcript could not be automatically retrieved for this video.",
        "description": video_description,
        "instruction": (
            "Based on the video title and spoken transcript above, provide a comprehensive, clear, and well-structured summary "
            "in the user's language (Arabic or English) highlighting the main ideas, key takeaways, and conclusion."
        )
    }

    return ToolResult(
        success=True,
        output=output_data
    )


def handle_create_and_send_pdf(params: Dict[str, Any]) -> ToolResult:
    """Generate a clean styled PDF supporting Arabic/English and deliver to Desktop + Telegram."""
    title = params.get("title", "Report").strip() or "Report"
    content = params.get("content", "").strip()
    filename = params.get("filename", "report.pdf").strip() or "report.pdf"
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    send_to_phone = bool(params.get("send_to_phone", True))

    try:
        from PySide6.QtGui import QPdfWriter, QTextDocument, QPageSize
        from PySide6.QtCore import QMarginsF

        desktop_dir = Path.home() / "Desktop"
        desktop_dir.mkdir(parents=True, exist_ok=True)
        out_path = desktop_dir / filename

        # Determine if Arabic script is dominant or present
        is_arabic = any('؀' <= ch <= 'ۿ' or 'ݐ' <= ch <= 'ݿ' for ch in (title + content))
        direction = "rtl" if is_arabic else "ltr"
        align = "right" if is_arabic else "left"

        # Format content paragraphs into clean HTML
        paragraphs = []
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                paragraphs.append("<br>")
            elif line.startswith("#"):
                clean_h = line.lstrip("#").strip()
                paragraphs.append(f"<h3 style='color:#1e293b; margin-top:12px; margin-bottom:4px;'>{html.escape(clean_h)}</h3>")
            elif line.startswith(("-", "*")):
                clean_li = line.lstrip("-*").strip()
                paragraphs.append(f"<p style='margin:3px 0;'>&bull; {html.escape(clean_li)}</p>")
            else:
                paragraphs.append(f"<p style='margin:6px 0; line-height:1.6;'>{html.escape(line)}</p>")
        body_html = "\n".join(paragraphs)

        html_doc = f"""<!DOCTYPE html>
<html dir="{direction}">
<head>
<meta charset="utf-8">
<style>
  body {{
    font-family: 'Segoe UI', Arial, sans-serif;
    color: #0f172a;
    margin: 20px;
    direction: {direction};
    text-align: {align};
  }}
  .header {{
    border-bottom: 2px solid #6366f1;
    padding-bottom: 10px;
    margin-bottom: 20px;
  }}
  h1 {{
    color: #4f46e5;
    font-size: 24px;
    margin: 0 0 6px 0;
  }}
  .content {{
    font-size: 13px;
    color: #1e293b;
  }}
  .footer {{
    margin-top: 30px;
    border-top: 1px solid #e2e8f0;
    padding-top: 8px;
    font-size: 10px;
    color: #94a3b8;
    text-align: center;
  }}
</style>
</head>
<body>
  <div class="header">
    <h1>{html.escape(title)}</h1>
  </div>
  <div class="content">
    {body_html}
  </div>
  <div class="footer">
    Generated by AI Desktop Assistant
  </div>
</body>
</html>"""

        writer = QPdfWriter(str(out_path))
        writer.setPageSize(QPageSize(QPageSize.A4))
        writer.setPageMargins(QMarginsF(15, 15, 15, 15))

        document = QTextDocument()
        document.setHtml(html_doc)
        document.print_(writer)

        sent_telegram = False
        telegram_status = ""
        if send_to_phone:
            bot_token = TELEGRAM_BOT_TOKEN or os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat_id = TELEGRAM_CHAT_ID or os.environ.get("TELEGRAM_CHAT_ID", "")
            if bot_token and chat_id:
                sent_telegram = send_telegram_document(
                    file_path=str(out_path),
                    caption=f"📄 {title}",
                    token=bot_token,
                    chat_id=chat_id
                )
                telegram_status = " and sent it to your phone via Telegram" if sent_telegram else " (Telegram upload skipped or pending)"
            else:
                telegram_status = " (Telegram not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env)"

        confirmation = f"✅ Created PDF '{filename}' on Desktop{telegram_status}."
        return ToolResult(
            success=True,
            output={
                "title": title,
                "file_path": str(out_path),
                "filename": filename,
                "sent_to_telegram": sent_telegram,
                "message": confirmation
            }
        )
    except Exception as e:
        return ToolResult(
            success=False,
            output=None,
            error=f"PDF generation failed: {e}"
        )


def handle_send_to_telegram(params: Dict[str, Any]) -> ToolResult:
    """Send text messages or documents directly to the authorized Telegram chat."""
    bot_token = TELEGRAM_BOT_TOKEN or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = TELEGRAM_CHAT_ID or os.environ.get("TELEGRAM_CHAT_ID", "")

    if not bot_token or not chat_id:
        return ToolResult(
            success=False,
            output=None,
            error="Telegram bot token or chat ID is not configured. Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your .env file."
        )

    message = params.get("message", "").strip()
    file_path = params.get("file_path", "").strip()

    if not message and not file_path:
        return ToolResult(
            success=False,
            output=None,
            error="Either 'message' or 'file_path' must be provided"
        )

    doc_sent = False
    msg_sent = False

    if file_path:
        doc_sent = send_telegram_document(file_path=file_path, caption=message, token=bot_token, chat_id=chat_id)
        if not doc_sent:
            return ToolResult(
                success=False,
                output=None,
                error=f"Failed to send file '{file_path}' to Telegram"
            )
        return ToolResult(
            success=True,
            output={"file_path": file_path, "message": f"✅ File '{file_path}' sent to your Telegram."}
        )

    if message:
        msg_sent = send_telegram_message(text=message, token=bot_token, chat_id=chat_id)
        if not msg_sent:
            return ToolResult(
                success=False,
                output=None,
                error="Failed to send message to Telegram"
            )
        return ToolResult(
            success=True,
            output={"message": "✅ Message sent to your Telegram."}
        )

    return ToolResult(success=False, output=None, error="No action taken")


def handle_capture_and_inspect_screen(params: Dict[str, Any]) -> ToolResult:
    """Capture a screenshot of the user's desktop, inspect open/active windows, and deliver to Telegram."""
    send_to_telegram = params.get("send_to_telegram", True)
    if isinstance(send_to_telegram, str):
        send_to_telegram = send_to_telegram.lower() in ("true", "1", "yes")

    caption = params.get("caption", "").strip()

    # 1. Inspect active foreground window and visible windows
    active_window = "Desktop"
    visible_windows: List[str] = []

    try:
        import win32gui
        hwnd = win32gui.GetForegroundWindow()
        if hwnd:
            active_window = win32gui.GetWindowText(hwnd).strip() or "Desktop"

        def _enum_win_cb(h, window_list):
            if win32gui.IsWindowVisible(h):
                txt = win32gui.GetWindowText(h).strip()
                if txt and txt not in ("Default IME", "MSCTFIME UI", "Program Manager") and txt not in window_list:
                    window_list.append(txt)

        win32gui.EnumWindows(_enum_win_cb, visible_windows)
    except Exception:
        # Fallback via ctypes
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buff = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buff, length + 1)
                    active_window = buff.value.strip() or "Desktop"
        except Exception:
            active_window = "Desktop"

    # 2. Capture screenshot of the Windows Desktop
    import time
    screenshot_dir = Path.home() / ".ai-desktop-assistant" / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time())
    screenshot_path = screenshot_dir / f"screenshot_{timestamp}.png"

    screenshot_captured = False
    error_msg = ""

    # Primary method: PIL.ImageGrab
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(all_screens=True)
        img.save(str(screenshot_path), format="PNG")
        screenshot_captured = True
    except Exception as e:
        # Fallback 1: pyautogui
        try:
            import pyautogui
            screenshot = pyautogui.screenshot()
            screenshot.save(str(screenshot_path))
            screenshot_captured = True
        except Exception as e2:
            # Fallback 2: PySide6 QGuiApplication
            try:
                from PySide6.QtGui import QGuiApplication
                screen = QGuiApplication.primaryScreen()
                if screen:
                    pix = screen.grabWindow(0)
                    pix.save(str(screenshot_path), "PNG")
                    screenshot_captured = True
            except Exception as e3:
                error_msg = f"Screen grab failed: {e}; {e2}; {e3}"

    if not screenshot_captured or not screenshot_path.exists():
        return ToolResult(
            success=False,
            output={
                "active_window": active_window,
                "visible_windows": visible_windows[:15],
            },
            error=f"Could not capture desktop screen: {error_msg}"
        )

    # 3. Deliver to Telegram if requested
    sent_telegram = False
    telegram_status = ""
    if send_to_telegram:
        bot_token = TELEGRAM_BOT_TOKEN or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = TELEGRAM_CHAT_ID or os.environ.get("TELEGRAM_CHAT_ID", "")
        photo_caption = caption or f"🖥️ Live Screen: {active_window}"
        if bot_token and chat_id:
            send_telegram_photo_async(
                image_path=str(screenshot_path),
                caption=photo_caption,
                token=bot_token,
                chat_id=chat_id
            )
            sent_telegram = True
            telegram_status = " Screenshot sent to your Telegram."
        else:
            telegram_status = " (Telegram not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env)"

    top_apps = ", ".join(visible_windows[:8]) if visible_windows else "None detected"
    summary = (
        f"Active window: '{active_window}'. "
        f"Open applications: {top_apps}. "
        f"Screenshot captured at '{screenshot_path}'.{telegram_status}"
    )

    return ToolResult(
        success=True,
        output={
            "active_window": active_window,
            "visible_windows": visible_windows[:15],
            "screenshot_path": str(screenshot_path),
            "sent_to_telegram": sent_telegram,
            "summary": summary,
            "message": f"✅ Captured screen. Active window is '{active_window}'.{telegram_status}"
        }
    )


# === Windows Clipboard Manager ===

_CLIPBOARD_HISTORY: List[str] = []


def _record_clipboard_history(text: str) -> None:
    """Record text entry in clipboard history, keeping up to 20 unique items."""
    global _CLIPBOARD_HISTORY
    if not text or not str(text).strip():
        return
    clean = str(text).strip()
    if clean in _CLIPBOARD_HISTORY:
        _CLIPBOARD_HISTORY.remove(clean)
    _CLIPBOARD_HISTORY.insert(0, clean)
    if len(_CLIPBOARD_HISTORY) > 20:
        _CLIPBOARD_HISTORY.pop()


def _get_clipboard_unicode() -> str:
    """Retrieve text from Windows clipboard safely."""
    try:
        import win32clipboard
        import win32con
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) or ""
            elif win32clipboard.IsClipboardFormatAvailable(win32con.CF_TEXT):
                data = win32clipboard.GetClipboardData(win32con.CF_TEXT)
                return data.decode("utf-8", errors="ignore") if isinstance(data, bytes) else str(data)
            return ""
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return ""


def _set_clipboard_unicode(text: str) -> bool:
    """Set Windows clipboard to unicode text."""
    try:
        import win32clipboard
        import win32con
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, str(text))
            return True
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return False


def _simulate_keybd(vk_key: int, ctrl: bool = False) -> None:
    """Simulate key press with optional Ctrl modifier using ctypes."""
    import ctypes
    user32 = ctypes.windll.user32
    VK_CONTROL = 0x11
    KEYEVENTF_KEYUP = 0x0002

    if ctrl:
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(vk_key, 0, 0, 0)
    import time
    time.sleep(0.04)
    user32.keybd_event(vk_key, 0, KEYEVENTF_KEYUP, 0)
    if ctrl:
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def handle_clipboard_manager(params: Dict[str, Any]) -> ToolResult:
    """Manage Windows clipboard: read, write, paste, copy selected, history, or clear."""
    action = str(params.get("action", "get")).strip().lower()
    text = str(params.get("text", ""))
    index = int(params.get("index", 0))

    import time
    global _CLIPBOARD_HISTORY

    # 1. Clear clipboard
    if action in ("clear", "empty", "مسح", "تفريغ"):
        try:
            import win32clipboard
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
            finally:
                win32clipboard.CloseClipboard()
            return ToolResult(
                success=True,
                output={"action": "clear", "message": "✅ تم تفريغ الحافظة بنجاح."}
            )
        except Exception as e:
            return ToolResult(success=False, output=None, error=f"Failed to clear clipboard: {e}")

    # 2. Get / Read clipboard
    if action in ("get", "read", "قراءة", "عرض", "الحافظة"):
        clip_text = _get_clipboard_unicode()
        if clip_text:
            _record_clipboard_history(clip_text)
            preview = clip_text[:200] + "..." if len(clip_text) > 200 else clip_text
            return ToolResult(
                success=True,
                output={
                    "action": "get",
                    "text": clip_text,
                    "length": len(clip_text),
                    "message": f"📋 محتوى الحافظة: \"{preview}\""
                }
            )
        else:
            return ToolResult(
                success=True,
                output={"action": "get", "text": "", "message": "📋 الحافظة فارغة حالياً."}
            )

    # 3. Set / Copy text into clipboard
    if action in ("set", "copy", "نسخ", "وضع"):
        if not text:
            return ToolResult(success=False, output=None, error="Parameter 'text' is required for action 'set'")
        ok = _set_clipboard_unicode(text)
        if ok:
            _record_clipboard_history(text)
            preview = text[:150] + "..." if len(text) > 150 else text
            return ToolResult(
                success=True,
                output={
                    "action": "set",
                    "text": text,
                    "length": len(text),
                    "message": f"✅ تم نسخ النص إلى الحافظة: \"{preview}\""
                }
            )
        else:
            return ToolResult(success=False, output=None, error="Failed to write text to Windows clipboard")

    # 4. Paste into active window
    if action in ("paste", "لصق", "الصق"):
        if text:
            _set_clipboard_unicode(text)
            _record_clipboard_history(text)

        time.sleep(0.15)
        # VK_V = 0x56
        _simulate_keybd(0x56, ctrl=True)
        return ToolResult(
            success=True,
            output={"action": "paste", "message": "✅ تم لصق النص في النافذة النشطة بنجاح."}
        )

    # 5. Copy selected text from foreground window
    if action in ("copy_selected", "نسخ_المحدد", "نسخ_المحدد_حاليا"):
        # VK_C = 0x43
        _simulate_keybd(0x43, ctrl=True)
        time.sleep(0.15)
        new_text = _get_clipboard_unicode()
        if new_text:
            _record_clipboard_history(new_text)
            preview = new_text[:150] + "..." if len(new_text) > 150 else new_text
            return ToolResult(
                success=True,
                output={
                    "action": "copy_selected",
                    "text": new_text,
                    "length": len(new_text),
                    "message": f"✅ تم نسخ النص المحدد: \"{preview}\""
                }
            )
        else:
            return ToolResult(
                success=True,
                output={"action": "copy_selected", "text": "", "message": "⚠️ لم يتم العثور على نص محدد للنسخ."}
            )

    # 6. Clipboard History
    if action in ("history", "سجل", "سجل_الحافظة", "تاريخ"):
        if index > 0 and index <= len(_CLIPBOARD_HISTORY):
            item_to_restore = _CLIPBOARD_HISTORY[index - 1]
            _set_clipboard_unicode(item_to_restore)
            _record_clipboard_history(item_to_restore)
            preview = item_to_restore[:150] + "..." if len(item_to_restore) > 150 else item_to_restore
            return ToolResult(
                success=True,
                output={
                    "action": "history_restore",
                    "restored_index": index,
                    "text": item_to_restore,
                    "message": f"✅ تم استعادة العنصر رقم {index} إلى الحافظة: \"{preview}\""
                }
            )
        else:
            if not _CLIPBOARD_HISTORY:
                return ToolResult(
                    success=True,
                    output={"action": "history", "items": [], "message": "📋 سجل الحافظة فارغ حالياً."}
                )

            formatted_history = [
                f"{i + 1}. {item[:80]}..." if len(item) > 80 else f"{i + 1}. {item}"
                for i, item in enumerate(_CLIPBOARD_HISTORY)
            ]
            return ToolResult(
                success=True,
                output={
                    "action": "history",
                    "count": len(_CLIPBOARD_HISTORY),
                    "items": _CLIPBOARD_HISTORY,
                    "message": "📋 سجل الحافظة:\n" + "\n".join(formatted_history)
                }
            )

    return ToolResult(success=False, output=None, error=f"Unknown clipboard action: {action}")


# === WhatsApp Desktop Sender ===

def _is_phone_number(val: str) -> bool:
    """Check if recipient string looks like a numeric phone number."""
    clean = re.sub(r'[\s\-\(\)\+]', '', val.strip())
    return clean.isdigit() and len(clean) >= 8


def _normalize_moroccan_phone(val: str) -> str:
    """Normalize phone numbers, converting Moroccan 06/07 numbers to international 212 format."""
    clean = re.sub(r'[\s\-\(\)\+]', '', val.strip())
    if len(clean) == 10 and (clean.startswith("06") or clean.startswith("07")):
        return "212" + clean[1:]
    if clean.startswith("00212"):
        return clean[2:]
    return clean


def _mouse_click(x: int, y: int) -> None:
    """Move cursor to (x, y) and perform a left mouse click using ctypes."""
    import ctypes
    import time
    try:
        ctypes.windll.user32.SetCursorPos(int(x), int(y))
        time.sleep(0.05)
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
        time.sleep(0.05)
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
        time.sleep(0.05)
    except Exception:
        pass


def _focus_whatsapp_window() -> Optional[tuple[int, tuple[int, int, int, int]]]:
    """Find WhatsApp window, bring it to the foreground, and return (hwnd, (left, top, right, bottom))."""
    import win32gui
    import win32con
    import ctypes
    import time

    found = find_matching_window("whatsapp") or find_matching_window("واتساب")
    if not found:
        try:
            os.startfile("whatsapp:")
        except Exception:
            pass
        for _ in range(8):
            time.sleep(0.5)
            found = find_matching_window("whatsapp") or find_matching_window("واتساب")
            if found:
                break

    if not found:
        return None

    hwnd, title = found
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        time.sleep(0.1)

        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)  # Alt down
            time.sleep(0.02)
            ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)  # Alt up
            win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass

    time.sleep(0.4)
    try:
        rect = win32gui.GetWindowRect(hwnd)
        return hwnd, rect
    except Exception:
        return hwnd, (0, 0, 1000, 700)


def _find_contact_element_uia(hwnd: int, win_left: int, win_width: int, recipient: str) -> Optional[tuple[int, int]]:
    """Query UIA for a sidebar item matching recipient in left pane and return its center (cx, cy)."""
    import subprocess
    import json
    from pathlib import Path

    ps_file = Path("inspect_wa_contact.ps1")
    out_file = Path("inspect_wa_contact_out.json")
    out_resolved = str(out_file.resolve()).replace("\\", "\\\\").replace("'", "''")
    safe_rec = recipient.replace("'", "''")

    ps_script = f"""[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;
$OutputEncoding = [System.Text.Encoding]::UTF8;
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$hwnd = [IntPtr]{hwnd}
$targetWin = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
$foundItem = $null

if ($targetWin) {{
    $condTrue = [System.Windows.Automation.Condition]::TrueCondition
    $elements = $targetWin.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condTrue)
    $maxLeft = {win_left} + ({win_width} * 0.38)

    foreach ($el in $elements) {{
        try {{
            $name = $el.Current.Name
            $rect = $el.Current.BoundingRectangle
            $ct = $el.Current.ControlType.ProgrammaticName
            if ($name -and $rect -and $rect.Width -gt 0 -and $rect.Height -gt 0) {{
                if ($rect.Left -lt $maxLeft) {{
                    if ($ct -match '(?i)ListItem|DataItem|Button|Text') {{
                        if ($name -match [regex]::Escape('{safe_rec}')) {{
                            $foundItem = @{{
                                Left = $rect.Left
                                Top = $rect.Top
                                Width = $rect.Width
                                Height = $rect.Height
                            }}
                            break
                        }}
                    }}
                }}
            }}
        }} catch {{}}
    }}
}}

if ($foundItem) {{
    $foundItem | ConvertTo-Json -Compress | Set-Content -Path '{out_resolved}' -Encoding utf8
}} else {{
    @{{ Found = $false }} | ConvertTo-Json -Compress | Set-Content -Path '{out_resolved}' -Encoding utf8
}}
"""
    try:
        ps_file.write_text(ps_script, encoding="utf-8")
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ps_file)],
            capture_output=True,
            timeout=8,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        if out_file.exists():
            data = json.loads(out_file.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict) and data.get("Width") and data.get("Width") > 0:
                cx = int(data["Left"] + data["Width"] // 2)
                cy = int(data["Top"] + data["Height"] // 2)
                return cx, cy
    except Exception:
        pass
    finally:
        for f in (ps_file, out_file):
            try:
                if f.exists():
                    f.unlink()
            except Exception:
                pass
    return None


def _open_whatsapp_chat(recipient: str) -> bool:
    """Reliably open a WhatsApp chat using direct UIA match or search box fallback."""
    hwnd_rect = _focus_whatsapp_window()
    if not hwnd_rect:
        return False
    hwnd, (win_left, win_top, win_right, win_bottom) = hwnd_rect
    win_width = win_right - win_left

    recipient = str(recipient).strip()
    if not recipient:
        return False

    import subprocess
    import json
    import time
    from pathlib import Path

    # 1. Direct UIA Match First
    contact_center = _find_contact_element_uia(hwnd, win_left, win_width, recipient)
    if contact_center:
        _mouse_click(contact_center[0], contact_center[1])
        time.sleep(0.8)
        return True

    # 2. Search Box Fallback (if not currently visible in sidebar)
    _simulate_keybd(0x1B, ctrl=False)  # VK_ESCAPE
    time.sleep(0.1)
    _simulate_keybd(0x1B, ctrl=False)
    time.sleep(0.1)

    search_ps = Path("inspect_wa_search.ps1")
    search_out = Path("inspect_wa_search_out.json")
    search_out_resolved = str(search_out.resolve()).replace("\\", "\\\\").replace("'", "''")

    search_script = f"""[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;
$OutputEncoding = [System.Text.Encoding]::UTF8;
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$hwnd = [IntPtr]{hwnd}
$targetWin = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
$box = $null

if ($targetWin) {{
    $condTrue = [System.Windows.Automation.Condition]::TrueCondition
    $elements = $targetWin.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condTrue)
    $maxLeft = {win_left} + ({win_width} * 0.38)

    foreach ($el in $elements) {{
        try {{
            $name = $el.Current.Name
            $aid = $el.Current.AutomationId
            $rect = $el.Current.BoundingRectangle
            if ($rect -and $rect.Width -gt 0 -and $rect.Height -gt 0) {{
                if ($rect.Left -lt $maxLeft) {{
                    if (($name -match '(?i)search|بحث') -or ($aid -match '(?i)search')) {{
                        $box = @{{
                            Left = $rect.Left
                            Top = $rect.Top
                            Width = $rect.Width
                            Height = $rect.Height
                        }}
                        break
                    }}
                }}
            }}
        }} catch {{}}
    }}
}}

if ($box) {{
    $box | ConvertTo-Json -Compress | Set-Content -Path '{search_out_resolved}' -Encoding utf8
}} else {{
    @{{ Found = $false }} | ConvertTo-Json -Compress | Set-Content -Path '{search_out_resolved}' -Encoding utf8
}}
"""
    box_clicked = False
    try:
        search_ps.write_text(search_script, encoding="utf-8")
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(search_ps)],
            capture_output=True,
            timeout=8,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        if search_out.exists():
            sdata = json.loads(search_out.read_text(encoding="utf-8", errors="replace"))
            if isinstance(sdata, dict) and sdata.get("Width") and sdata.get("Width") > 0:
                bx = int(sdata["Left"] + sdata["Width"] // 2)
                by = int(sdata["Top"] + sdata["Height"] // 2)
                _mouse_click(bx, by)
                box_clicked = True
    except Exception:
        pass
    finally:
        for f in (search_ps, search_out):
            try:
                if f.exists():
                    f.unlink()
            except Exception:
                pass

    if not box_clicked:
        _mouse_click(win_left + 180, win_top + 115)

    time.sleep(0.2)

    old_clip = _get_clipboard_unicode()
    try:
        _simulate_keybd(0x41, ctrl=True)  # Ctrl+A
        _simulate_keybd(0x08, ctrl=False)  # Backspace
        time.sleep(0.1)

        _set_clipboard_unicode(recipient)
        _simulate_keybd(0x56, ctrl=True)  # Ctrl+V
        time.sleep(1.8)  # Wait 1.8 seconds for search results to filter
    finally:
        if old_clip:
            _set_clipboard_unicode(old_clip)

    res_center = _find_contact_element_uia(hwnd, win_left, win_width, recipient)
    if res_center:
        _mouse_click(res_center[0], res_center[1])
    else:
        _simulate_keybd(0x28, ctrl=False)
        _simulate_keybd(0x0D, ctrl=False)

    time.sleep(0.8)
    return True


def handle_whatsapp_send(params: Dict[str, Any]) -> ToolResult:
    """Automate sending a message to a phone number or contact on Windows WhatsApp Desktop."""
    recipient = str(params.get("recipient", "")).strip()
    message = str(params.get("message", "")).strip()
    auto_send = params.get("auto_send", True)
    if isinstance(auto_send, str):
        auto_send = auto_send.lower() in ("true", "1", "yes")

    if not recipient:
        return ToolResult(success=False, output=None, error="Parameter 'recipient' (phone number or contact name) is required")
    if not message:
        return ToolResult(success=False, output=None, error="Parameter 'message' is required")

    import time
    import urllib.parse

    # Mode 1: Phone Number Mode
    if _is_phone_number(recipient):
        clean_phone = _normalize_moroccan_phone(recipient)
        encoded_msg = urllib.parse.quote(message)
        whatsapp_url = f"whatsapp://send?phone={clean_phone}&text={encoded_msg}"

        try:
            os.startfile(whatsapp_url)
        except Exception as e:
            try:
                web_url = f"https://wa.me/{clean_phone}?text={encoded_msg}"
                os.startfile(web_url)
            except Exception as e2:
                return ToolResult(success=False, output=None, error=f"Could not launch WhatsApp: {e}; {e2}")

        time.sleep(2.0)
        if auto_send:
            # VK_RETURN = 0x0D
            _simulate_keybd(0x0D, ctrl=False)

        return ToolResult(
            success=True,
            output={
                "mode": "phone",
                "recipient": clean_phone,
                "text": message,
                "auto_sent": auto_send,
                "message": f"✅ تم إرسال رسالة واتساب إلى الرقم {clean_phone} بنجاح!"
            }
        )

    # Mode 2: Contact / Group Name Mode
    else:
        hwnd_rect = _focus_whatsapp_window()
        if not hwnd_rect:
            return ToolResult(success=False, output=None, error="Could not locate or bring WhatsApp window to foreground")
        hwnd, (win_left, win_top, win_right, win_bottom) = hwnd_rect
        win_width = win_right - win_left

        opened = _open_whatsapp_chat(recipient)
        if not opened:
            return ToolResult(success=False, output=None, error=f"Could not open WhatsApp chat for contact '{recipient}'")

        # Explicitly click bottom-right message input box before pasting message
        input_x = win_left + int(win_width * 0.65)
        input_y = win_bottom - 45
        _mouse_click(input_x, input_y)
        time.sleep(0.3)

        old_clip = _get_clipboard_unicode()
        try:
            _set_clipboard_unicode(message)
            _simulate_keybd(0x56, ctrl=True)  # VK_V
            time.sleep(0.3)
            if auto_send:
                _simulate_keybd(0x0D, ctrl=False)  # VK_RETURN
                time.sleep(0.2)
        finally:
            if old_clip:
                _set_clipboard_unicode(old_clip)

        return ToolResult(
            success=True,
            output={
                "mode": "contact",
                "recipient": recipient,
                "text": message,
                "auto_sent": auto_send,
                "message": f"✅ تم إرسال رسالة واتساب إلى '{recipient}' بنجاح!"
            }
        )


def handle_whatsapp_read(
    params: Optional[Dict[str, Any]] = None,
    contact_or_chat: str = "",
    count: int = 10,
    send_to_telegram: bool = True,
    send_screenshot: bool = False,
    **kwargs
) -> ToolResult:
    """Read latest messages from WhatsApp Desktop using deep UIA, Windows OCR fallback, and clean text Telegram delivery."""
    if isinstance(params, dict):
        contact_or_chat = params.get("contact_or_chat", contact_or_chat)
        count = params.get("count", count)
        send_to_telegram = params.get("send_to_telegram", send_to_telegram)
        send_screenshot = params.get("send_screenshot", send_screenshot)
    elif params is not None and not contact_or_chat:
        contact_or_chat = str(params)

    contact_or_chat = str(contact_or_chat or "").strip()
    if contact_or_chat.lower() in ("meta ai", "metaai", "ميتا ai", "ميتا اي", "ميتا"):
        contact_or_chat = "Meta AI"

    import time
    import os
    import subprocess
    import json
    import re
    from pathlib import Path
    import win32gui
    import win32con

    # Clean up any leftover temporary inspection files
    for tmp_file in (
        Path("inspect_wa.ps1"), Path("inspect_wa_out.json"),
        Path("inspect_ocr.ps1"), Path("inspect_ocr_out.json"),
        Path("inspect_wa_contact.ps1"), Path("inspect_wa_contact_out.json"),
        Path("inspect_wa_search.ps1"), Path("inspect_wa_search_out.json"),
        Path("inspect_wa_res.ps1"), Path("inspect_wa_res_out.json"),
        Path("inspect_wa_messages.ps1"), Path("inspect_wa_messages_out.json")
    ):
        try:
            if tmp_file.exists():
                tmp_file.unlink()
        except Exception:
            pass

    # Step 1: Open chat or bring WhatsApp to foreground
    if contact_or_chat:
        _open_whatsapp_chat(contact_or_chat)
    else:
        _focus_whatsapp_window()
        time.sleep(0.5)

    # Capture cropped screenshot of right-side WhatsApp conversation pane for fallback
    hwnd_rect = _focus_whatsapp_window()
    chat_rect = None
    hwnd = None
    if hwnd_rect:
        hwnd, (win_left, win_top, win_right, win_bottom) = hwnd_rect
        w = win_right - win_left
        h = win_bottom - win_top
        if w > 400 and h > 200:
            chat_rect = (win_left + int(w * 0.32), win_top + 60, win_right, win_bottom - 60)
        elif w > 0 and h > 0:
            chat_rect = (win_left, win_top, win_right, win_bottom)

    screenshot_path = Path.home() / ".altron" / "whatsapp_chat.png"
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    has_screenshot = False

    try:
        from PIL import ImageGrab
        if chat_rect:
            img = ImageGrab.grab(bbox=chat_rect)
        else:
            img = ImageGrab.grab()
        img.save(str(screenshot_path), "PNG")
        has_screenshot = True
    except Exception:
        try:
            import pyautogui
            if chat_rect:
                cw = chat_rect[2] - chat_rect[0]
                ch = chat_rect[3] - chat_rect[1]
                img = pyautogui.screenshot(region=(chat_rect[0], chat_rect[1], cw, ch))
            else:
                img = pyautogui.screenshot()
            img.save(str(screenshot_path))
            has_screenshot = True
        except Exception:
            pass

    # Step 2: Deep UIA Extraction ignoring elements outside active message pane, filtering chrome/outgoing, sorting by Top
    latest_reply = ""
    if hwnd_rect:
        hwnd, (win_left, win_top, win_right, win_bottom) = hwnd_rect
        win_width = win_right - win_left

        ps_read_file = Path("inspect_wa_messages.ps1")
        ps_read_out = Path("inspect_wa_messages_out.json")
        ps_read_out_resolved = str(ps_read_out.resolve()).replace("\\", "\\\\").replace("'", "''")

        read_script = f"""[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;
$OutputEncoding = [System.Text.Encoding]::UTF8;
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$hwnd = [IntPtr]{hwnd}
$targetWin = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
$collected = @()

if ($targetWin) {{
    $condTrue = [System.Windows.Automation.Condition]::TrueCondition
    $elements = $targetWin.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condTrue)
    $minLeft = {win_left} + ({win_width} * 0.35)
    $minTop = {win_top} + 80
    $maxBottom = {win_bottom} - 80

    foreach ($el in $elements) {{
        try {{
            $rect = $el.Current.BoundingRectangle
            if (-not $rect -or $rect.Width -le 0 -or $rect.Height -le 0) {{ continue }}

            # Ignore elements outside active message history pane
            if ($rect.Left -lt $minLeft -or $rect.Top -lt $minTop -or ($rect.Top + $rect.Height) -gt $maxBottom) {{
                continue
            }}

            $text = $el.Current.Name
            if (-not $text -or $text.Trim().Length -eq 0) {{
                try {{
                    $valObj = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
                    if ($valObj) {{
                        $text = $valObj.Current.Value
                    }}
                }} catch {{}}
            }}

            if ($text -and $text.Trim().Length -gt 0) {{
                $collected += @{{
                    Text = $text.Trim()
                    Top = $rect.Top
                    Left = $rect.Left
                }}
            }}
        }} catch {{}}
    }}
}}

$collected | ConvertTo-Json -Compress | Set-Content -Path '{ps_read_out_resolved}' -Encoding utf8
"""
        try:
            ps_read_file.write_text(read_script, encoding="utf-8")
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ps_read_file)],
                capture_output=True,
                timeout=10,
                text=True,
                encoding="utf-8",
                errors="replace"
            )
            if ps_read_out.exists():
                rdata = json.loads(ps_read_out.read_text(encoding="utf-8", errors="replace"))
                if isinstance(rdata, dict):
                    rdata = [rdata]
                elif not isinstance(rdata, list):
                    rdata = []

                noise_exact = {
                    "type a message", "اكتب رسالة", "voice message", "رسالة صوتية",
                    "attach", "ارفاق", "emojis, gifs, stickers", "ملصقات", "emoji", "إيموجي", "stickers",
                    "delivered", "read", "sent", "تم التسليم", "تمت القراءة", "تم الإرسال",
                    "wds-ic-read", "you:", "أنت:", "you", "أنت",
                    "minimize", "maximize", "close", "restore", "whatsapp", "واتساب", "whatsapp web",
                    "search", "search or start new chat", "بحث", "بحث أو بدء دردشة جديدة",
                    "settings", "الإعدادات", "اعدادات", "profile", "الملف الشخصي",
                    "starred messages", "الرسائل المميزة بنجمة", "new chat", "دردشة جديدة",
                    "chats", "الدردشات", "status", "الحالة", "channels", "القنوات",
                    "communities", "المجتمعات", "calls", "المكالمات", "archived", "المؤرشفة",
                    "logout", "تسجيل الخروج", "online", "متصل الآن", "typing...", "يكتب الآن...",
                    "meta ai", "metaai"
                }

                valid_messages = []
                for item in rdata:
                    if not isinstance(item, dict):
                        continue
                    txt = str(item.get("Text", "")).strip()
                    top_pos = item.get("Top", 0)
                    if not txt:
                        continue
                    t_low = txt.lower()
                    if t_low in noise_exact or t_low == contact_or_chat.lower():
                        continue
                    # Exclude outgoing messages sent by the user (containing "You:" / "You " or "Delivered" or "أنت:" / "أنت ")
                    if (
                        t_low.startswith("you:")
                        or t_low.startswith("you ")
                        or t_low == "you"
                        or t_low.startswith("أنت:")
                        or t_low.startswith("أنت ")
                        or t_low == "أنت"
                        or "delivered" in t_low
                        or "wds-ic-read" in t_low
                    ):
                        continue
                    # Exclude timestamps
                    if re.match(r'^(?:\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM|am|pm|ص|م)?|أمس|اليوم|yesterday|today|\d{1,2}/\d{1,2}(?:/\d{2,4})?)$', txt, re.IGNORECASE):
                        continue
                    if len(txt) <= 1 and not txt.isalnum():
                        continue
                    valid_messages.append((top_pos, txt))

                # Sort remaining incoming messages by vertical position (BoundingRectangle.Top)
                valid_messages.sort(key=lambda x: x[0])
                if valid_messages:
                    # Return ONLY the single latest (bottom-most) reply text
                    latest_reply = valid_messages[-1][1]
        except Exception:
            pass
        finally:
            for f in (ps_read_file, ps_read_out):
                try:
                    if f.exists():
                        f.unlink()
                except Exception:
                    pass

    # Step 3: Windows OCR Fallback on cropped right-side chat screenshot if UIA found no message
    if not latest_reply and has_screenshot and screenshot_path.exists():
        ocr_ps_file = Path("inspect_ocr.ps1")
        ocr_out_file = Path("inspect_ocr_out.json")
        img_resolved = str(screenshot_path.resolve()).replace("\\", "\\\\").replace("'", "''")
        ocr_out_resolved = str(ocr_out_file.resolve()).replace("\\", "\\\\").replace("'", "''")

        ocr_script = """[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;
$OutputEncoding = [System.Text.Encoding]::UTF8;
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime] | Out-Null

function AwaitOperation($op) {
    $task = [System.WindowsRuntimeSystemExtensions]::AsTask($op)
    $task.Wait()
    return $task.Result
}

try {
    $file = AwaitOperation([Windows.Storage.StorageFile]::GetFileFromPathAsync('__IMG_RESOLVED__'))
    $stream = AwaitOperation($file.OpenAsync([Windows.Storage.FileAccessMode]::Read))
    $decoder = AwaitOperation([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream))
    $bitmap = AwaitOperation($decoder.GetSoftwareBitmapAsync())

    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if (-not $engine) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new("en-US"))
    }
    $res = AwaitOperation($engine.RecognizeAsync($bitmap))
    $lines = @()
    foreach ($l in $res.Lines) {
        if ($l.Text and $l.Text.Trim().Length -gt 0) {
            $lines += $l.Text.Trim()
        }
    }
    $jsonOut = $lines | ConvertTo-Json -Compress
    [System.IO.File]::WriteAllText('__OCR_OUT_RESOLVED__', $jsonOut, [System.Text.Encoding]::UTF8)
} catch {
    [System.IO.File]::WriteAllText('__OCR_OUT_RESOLVED__', '[]', [System.Text.Encoding]::UTF8)
}
""".replace("__IMG_RESOLVED__", img_resolved).replace("__OCR_OUT_RESOLVED__", ocr_out_resolved)
        try:
            ocr_ps_file.write_text(ocr_script, encoding="utf-8")
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ocr_ps_file)],
                capture_output=True,
                timeout=12,
                text=True,
                encoding="utf-8",
                errors="replace"
            )
            ocr_out = ""
            if ocr_out_file.exists():
                ocr_out = ocr_out_file.read_text(encoding="utf-8", errors="replace").strip()
            if ocr_out:
                parsed_ocr = json.loads(ocr_out)
                if isinstance(parsed_ocr, list):
                    ocr_lines = [str(x).strip() for x in parsed_ocr if str(x).strip()]
                    noise_ocr = {
                        "type a message", "اكتب رسالة", "voice message", "رسالة صوتية",
                        "attach", "ارفاق", "emojis, gifs, stickers", "ملصقات", "delivered", "read", "sent",
                        "wds-ic-read", "you:", "أنت:", "you", "أنت"
                    }
                    valid_ocr = []
                    for line in ocr_lines:
                        l_clean = line.strip()
                        l_low = l_clean.lower()
                        if not l_clean or l_low in noise_ocr or l_low == contact_or_chat.lower():
                            continue
                        if l_low.startswith("you:") or l_low.startswith("you ") or l_low.startswith("أنت:") or l_low.startswith("أنت ") or "delivered" in l_low:
                            continue
                        if re.match(r'^(?:\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM|am|pm|ص|م)?|أمس|اليوم|yesterday|today|\d{1,2}/\d{1,2}(?:/\d{2,4})?)$', l_clean, re.IGNORECASE):
                            continue
                        if len(l_clean) <= 1 and not l_clean.isalnum():
                            continue
                        valid_ocr.append(l_clean)
                    if valid_ocr:
                        latest_reply = valid_ocr[-1]
        except Exception:
            pass
        finally:
            for f in (ocr_ps_file, ocr_out_file):
                try:
                    if f.exists():
                        f.unlink()
                except Exception:
                    pass

    # Telegram Delivery: Send clean text reply to Telegram
    sent_telegram = False
    bot_token = TELEGRAM_BOT_TOKEN or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = TELEGRAM_CHAT_ID or os.environ.get("TELEGRAM_CHAT_ID", "")
    chat_name = contact_or_chat or "Active Chat"

    if latest_reply:
        telegram_text = f"💬 *رد {chat_name}:*\n\n{latest_reply}" if any('؀' <= c <= 'ۿ' for c in chat_name) else f"💬 *Reply from {chat_name}:*\n\n{latest_reply}"
        if send_to_telegram and bot_token and chat_id:
            send_telegram_message_async(
                text=telegram_text,
                token=bot_token,
                chat_id=chat_id
            )
            sent_telegram = True

        t_status = " (تم إرسال الرد إلى تيليجرام 📱)" if sent_telegram else ""
        msg_summary = f"✅ أحدث رد من {chat_name}{t_status}:\n\n{latest_reply}"
    else:
        if has_screenshot and screenshot_path.exists() and send_to_telegram and bot_token and chat_id:
            caption = f"💬 لقطة محادثة واتساب ({chat_name}) مرفقة أدناه."
            send_telegram_photo_async(
                image_path=str(screenshot_path),
                caption=caption,
                token=bot_token,
                chat_id=chat_id
            )
            sent_telegram = True

        t_status = " (تم إرسال لقطة المحادثة إلى تيليجرام 📱)" if sent_telegram else ""
        msg_summary = f"✅ تم فحص محادثة {chat_name} بنجاح{t_status}."

    return ToolResult(
        success=True,
        output={
            "chat": chat_name,
            "latest_reply": latest_reply,
            "reply": latest_reply,
            "messages": [latest_reply] if latest_reply else [],
            "count": 1 if latest_reply else 0,
            "sent_to_telegram": sent_telegram,
            "screenshot_path": str(screenshot_path) if has_screenshot else "",
            "message": msg_summary
        }
    )


whatsapp_read = handle_whatsapp_read


# === Create Default Registry ===

def create_default_registry() -> ToolRegistry:
    """Create and populate the default tool registry."""
    registry = ToolRegistry()

    # File System Tools
    registry.register(Tool(
        name="file_search",
        description="Search for files by name in a directory",
        category=ToolCategory.FILE_SYSTEM,
        parameters=[
            ToolParameter("query", "string", "Search query for file names"),
            ToolParameter("directory", "string", "Directory to search in", required=False, default="~")
        ],
        handler=handle_file_search,
        requires_permission=False
    ))

    registry.register(Tool(
        name="file_read",
        description="Read the contents of a file",
        category=ToolCategory.FILE_SYSTEM,
        parameters=[
            ToolParameter("path", "string", "Path to the file to read"),
            ToolParameter("max_lines", "number", "Maximum lines to read", required=False, default=100)
        ],
        handler=handle_file_read,
        requires_permission=False
    ))

    registry.register(Tool(
        name="file_write",
        description="Write content to a file (creates or overwrites)",
        category=ToolCategory.FILE_SYSTEM,
        parameters=[
            ToolParameter("path", "string", "Path to the file to write"),
            ToolParameter("content", "string", "Content to write to the file")
        ],
        handler=handle_file_write,
        requires_permission=True,
        is_dangerous=True
    ))

    registry.register(Tool(
        name="file_delete",
        description="Delete a file or directory",
        category=ToolCategory.FILE_SYSTEM,
        parameters=[
            ToolParameter("path", "string", "Path to the file or directory to delete")
        ],
        handler=handle_file_delete,
        requires_permission=True,
        is_dangerous=True
    ))

    registry.register(Tool(
        name="file_copy",
        description="Copy a file or directory to a new location",
        category=ToolCategory.FILE_SYSTEM,
        parameters=[
            ToolParameter("source", "string", "Source file or directory path"),
            ToolParameter("destination", "string", "Destination file or directory path")
        ],
        handler=handle_file_copy,
        requires_permission=False
    ))

    registry.register(Tool(
        name="file_move",
        description="Move or rename a file or directory",
        category=ToolCategory.FILE_SYSTEM,
        parameters=[
            ToolParameter("source", "string", "Source file or directory path"),
            ToolParameter("destination", "string", "Destination file or directory path")
        ],
        handler=handle_file_move,
        requires_permission=True,
        is_dangerous=True
    ))

    registry.register(Tool(
        name="file_open",
        description="Open a file with its default Windows application",
        category=ToolCategory.FILE_SYSTEM,
        parameters=[
            ToolParameter("path", "string", "Path to the file to open")
        ],
        handler=handle_file_open,
        requires_permission=False
    ))

    # Terminal Tools
    registry.register(Tool(
        name="terminal_execute",
        description="Execute a terminal command (PowerShell, CMD, or Bash)",
        category=ToolCategory.TERMINAL,
        parameters=[
            ToolParameter("command", "string", "Command to execute"),
            ToolParameter("shell", "string", "Shell to use", required=False, default="powershell", enum=["powershell", "cmd", "bash"])
        ],
        handler=handle_terminal_command,
        requires_permission=True,
        is_dangerous=True
    ))

    # System Tools
    registry.register(Tool(
        name="app_open",
        description="Open an application by name or path",
        category=ToolCategory.SYSTEM,
        parameters=[
            ToolParameter("app_name", "string", "Name of the application", required=False),
            ToolParameter("path", "string", "Path to the application executable", required=False)
        ],
        handler=handle_app_open,
        requires_permission=False
    ))

    registry.register(Tool(
        name="app_close",
        description="Close an application by name or process ID",
        category=ToolCategory.SYSTEM,
        parameters=[
            ToolParameter("app_name", "string", "Name of the application", required=False),
            ToolParameter("pid", "number", "Process ID", required=False)
        ],
        handler=handle_app_close,
        requires_permission=True,
        is_dangerous=True
    ))

    registry.register(Tool(
        name="process_list",
        description="List running processes with CPU and memory usage",
        category=ToolCategory.SYSTEM,
        parameters=[],
        handler=handle_process_list,
        requires_permission=False
    ))

    registry.register(Tool(
        name="system_info",
        description="Get system information (CPU, RAM, OS, Disk)",
        category=ToolCategory.SYSTEM,
        parameters=[],
        handler=handle_system_info,
        requires_permission=False
    ))

    registry.register(Tool(
        name="window_control",
        description="Manage and control open application windows (list, focus, minimize, maximize)",
        category=ToolCategory.SYSTEM,
        parameters=[
            ToolParameter("action", "string", "Action to perform", required=False, default="list", enum=["list", "focus", "minimize", "maximize"]),
            ToolParameter("title", "string", "Target window title or partial title", required=False, default=""),
            ToolParameter("hwnd", "number", "Target window handle (HWND)", required=False, default=None)
        ],
        handler=handle_window_control,
        requires_permission=False
    ))

    registry.register(Tool(
        name="system_control",
        description="Instant Windows system controls for volume (volume_up, volume_down, volume_mute, set_volume), brightness (brightness_up, brightness_down, set_brightness), and opening settings (open_settings)",
        category=ToolCategory.SYSTEM,
        parameters=[
            ToolParameter("action", "string", "Action to perform", enum=["volume_up", "volume_down", "volume_mute", "set_volume", "brightness_up", "brightness_down", "set_brightness", "open_settings"]),
            ToolParameter("value", "number", "Optional value for brightness level, volume percentage (0-100), or volume steps", required=False, default=None)
        ],
        handler=handle_system_control,
        requires_permission=False
    ))

    registry.register(Tool(
        name="desktop_icon_position",
        description="Visually move DESKTOP ICONS, shortcut files, or folders located on the Windows Desktop wallpaper background. NEVER use this tool for open application windows, browsers, or running programs.",
        category=ToolCategory.SYSTEM,
        parameters=[
            ToolParameter("target", "string", "Name of the desktop shortcut/file to move on desktop background (e.g. 'her', 'main', 'PlayerMusic', or 'all')"),
            ToolParameter("position", "string", "Target screen position preset: 'center', 'top', 'bottom', 'left', 'right', 'top_left', 'top_right', 'bottom_left', 'bottom_right'", required=False, default="center", enum=["center", "top", "bottom", "left", "right", "top_left", "top_right", "bottom_left", "bottom_right"]),
            ToolParameter("x", "number", "Optional exact X screen coordinate", required=False, default=None),
            ToolParameter("y", "number", "Optional exact Y screen coordinate", required=False, default=None)
        ],
        handler=handle_desktop_icon_position,
        requires_permission=False
    ))

    registry.register(Tool(
        name="move_application_window",
        description="Move, snap, position, maximize, or minimize an open APPLICATION WINDOW (e.g. browser, Chrome, Brave, Edge, VS Code, OmniRoute, or any running software window). ALWAYS use this tool whenever the user mentions a window (نافذة), browser (متصفح), or running program.",
        category=ToolCategory.SYSTEM,
        parameters=[
            ToolParameter("window_query", "string", "Name, title, or type of the open window/app to move (e.g. 'omniroute', 'brave', 'chrome', 'vscode', or 'المتصفح')"),
            ToolParameter("position", "string", "Target position preset: 'right', 'left', 'center', 'top', 'bottom', 'top_left', 'top_right', 'bottom_left', 'bottom_right', 'maximize', 'minimize'", required=False, default="right", enum=["right", "left", "center", "top", "bottom", "top_left", "top_right", "bottom_left", "bottom_right", "maximize", "minimize"])
        ],
        handler=handle_move_application_window,
        requires_permission=False
    ))

    # Coding Tools
    registry.register(Tool(
        name="code_edit",
        description="Search and replace a specific block of code or text inside a file",
        category=ToolCategory.CODING,
        parameters=[
            ToolParameter("path", "string", "Path to the file to edit"),
            ToolParameter("old_string", "string", "Exact text block to find and replace"),
            ToolParameter("new_string", "string", "Replacement text"),
            ToolParameter("replace_all", "boolean", "Replace all occurrences (default False)", required=False, default=False)
        ],
        handler=handle_code_edit,
        requires_permission=True,
        is_dangerous=True
    ))

    # Browser Tools
    registry.register(Tool(
        name="browser_open",
        description="Open a URL in the default browser",
        category=ToolCategory.BROWSER,
        parameters=[
            ToolParameter("url", "string", "URL to open")
        ],
        handler=handle_browser_open,
        requires_permission=False
    ))

    registry.register(Tool(
        name="youtube_play",
        description="Search YouTube and immediately play the first matching video directly in the browser",
        category=ToolCategory.BROWSER,
        parameters=[
            ToolParameter("query", "string", "Song, video title, or search query to play on YouTube")
        ],
        handler=handle_youtube_play,
        requires_permission=False
    ))

    registry.register(Tool(
        name="youtube_play_and_summarize",
        description="Search for or open a YouTube video (by query or direct URL) to extract its spoken transcript and summarize its content. Set play=False when the user only asks to summarize or explain. Set play=True ONLY when the user explicitly asks to play/open/watch the video.",
        category=ToolCategory.BROWSER,
        parameters=[
            ToolParameter("query_or_url", "string", "Search query or direct YouTube URL to find and summarize"),
            ToolParameter("play", "boolean", "Whether to launch and play the video in the default browser. Set to False when user only wants to summarize/explain. Set to True only when user explicitly asks to play/open.", required=False, default=False),
            ToolParameter("summarize", "boolean", "Whether to extract transcript text for summarization (default True)", required=False, default=True)
        ],
        handler=handle_youtube_play_and_summarize,
        requires_permission=False
    ))

    registry.register(Tool(
        name="web_fetch",
        description="Fetch and extract readable text content from a web URL",
        category=ToolCategory.BROWSER,
        parameters=[
            ToolParameter("url", "string", "URL to fetch content from"),
            ToolParameter("max_chars", "number", "Maximum characters to return", required=False, default=4000)
        ],
        handler=handle_web_fetch,
        requires_permission=False
    ))

    # PDF & Telegram Tools
    registry.register(Tool(
        name="create_and_send_pdf",
        description="Generate a formatted PDF document supporting Arabic (RTL) and English, save to Desktop, and optionally deliver to the user's phone via Telegram",
        category=ToolCategory.UTILITY,
        parameters=[
            ToolParameter("title", "string", "Document title"),
            ToolParameter("content", "string", "Document content body or report text"),
            ToolParameter("filename", "string", "PDF filename (e.g. report.pdf)", required=False, default="report.pdf"),
            ToolParameter("send_to_phone", "boolean", "Whether to deliver the PDF to Telegram (default True)", required=False, default=True)
        ],
        handler=handle_create_and_send_pdf,
        requires_permission=False
    ))

    registry.register(Tool(
        name="send_to_telegram",
        description="Send a text message or local file directly to the user's phone via Telegram",
        category=ToolCategory.UTILITY,
        parameters=[
            ToolParameter("message", "string", "Text message to send", required=False, default=""),
            ToolParameter("file_path", "string", "Path to a local file to send as a document", required=False, default="")
        ],
        handler=handle_send_to_telegram,
        requires_permission=False
    ))

    # Screen Capture & Vision Tools
    registry.register(Tool(
        name="capture_and_inspect_screen",
        description="Capture a live screenshot of the Windows desktop, inspect the active foreground window and open visible applications, and optionally deliver the screenshot to the user's Telegram phone chat",
        category=ToolCategory.SYSTEM,
        parameters=[
            ToolParameter("send_to_telegram", "boolean", "Whether to deliver the screenshot photo to Telegram (default True)", required=False, default=True),
            ToolParameter("caption", "string", "Optional caption for the Telegram photo", required=False, default="")
        ],
        handler=handle_capture_and_inspect_screen,
        requires_permission=False
    ))

    # Windows Clipboard Manager Tool
    registry.register(Tool(
        name="clipboard_manager",
        description="Manage the Windows clipboard: read current text ('get'), copy text to clipboard ('set'), paste into foreground window ('paste'), copy selected text ('copy_selected'), view or restore history ('history'), or clear clipboard ('clear')",
        category=ToolCategory.UTILITY,
        parameters=[
            ToolParameter("action", "string", "Clipboard action: 'get', 'set', 'paste', 'copy_selected', 'history', 'clear'", enum=["get", "set", "paste", "copy_selected", "history", "clear"]),
            ToolParameter("text", "string", "Text to copy or paste (used with 'set' or 'paste')", required=False, default=""),
            ToolParameter("index", "number", "1-based history index to restore (used with 'history')", required=False, default=0)
        ],
        handler=handle_clipboard_manager,
        requires_permission=False
    ))

    # WhatsApp Desktop Sender Tool
    registry.register(Tool(
        name="whatsapp_send",
        description="Send a message to a contact name or phone number (e.g. Moroccan 06/07 numbers or international numbers) via Windows WhatsApp Desktop",
        category=ToolCategory.UTILITY,
        parameters=[
            ToolParameter("recipient", "string", "Recipient phone number (e.g. '0612345678', '+2126...') or contact/group name"),
            ToolParameter("message", "string", "Text message to send"),
            ToolParameter("auto_send", "boolean", "Whether to automatically press Enter to send the message (default True)", required=False, default=True)
        ],
        handler=handle_whatsapp_send,
        requires_permission=False
    ))

    # WhatsApp Desktop Reader Tool
    registry.register(Tool(
        name="whatsapp_read",
        description="Read the latest messages from any WhatsApp Desktop chat (e.g. contact name or Meta AI) and deliver clean text or screenshot to Telegram",
        category=ToolCategory.UTILITY,
        parameters=[
            ToolParameter("contact_or_chat", "string", "Contact name or chat title to open and read from (optional)", required=False, default=""),
            ToolParameter("count", "number", "Number of latest messages to return (default 10)", required=False, default=10),
            ToolParameter("send_to_telegram", "boolean", "Whether to deliver the response to Telegram (default True)", required=False, default=True),
            ToolParameter("send_screenshot", "boolean", "Whether to explicitly send the chat screenshot photo instead of text (default False)", required=False, default=False)
        ],
        handler=handle_whatsapp_read,
        requires_permission=False
    ))

    return registry
