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
from src.services.telegram_service import send_telegram_message, send_telegram_document
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


def handle_desktop_icon_position(params: Dict[str, Any]) -> ToolResult:
    """Handle visual positioning of desktop icons on the Windows screen."""
    target = params.get("target", "").strip()
    position = params.get("position", "center")
    x = params.get("x")
    y = params.get("y")

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
        description="Visually move desktop icons or files on the Windows Desktop screen to specific positions (center, top, bottom, left, right, top_left, top_right, bottom_left, bottom_right) or exact (x, y) coordinates",
        category=ToolCategory.SYSTEM,
        parameters=[
            ToolParameter("target", "string", "Name of the desktop icon/file to move (e.g. 'her', 'main', 'PlayerMusic', or 'all')"),
            ToolParameter("position", "string", "Target screen position preset: 'center', 'top', 'bottom', 'left', 'right', 'top_left', 'top_right', 'bottom_left', 'bottom_right'", required=False, default="center", enum=["center", "top", "bottom", "left", "right", "top_left", "top_right", "bottom_left", "bottom_right"]),
            ToolParameter("x", "number", "Optional exact X screen coordinate", required=False, default=None),
            ToolParameter("y", "number", "Optional exact Y screen coordinate", required=False, default=None)
        ],
        handler=handle_desktop_icon_position,
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

    return registry
