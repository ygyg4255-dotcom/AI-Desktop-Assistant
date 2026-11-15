"""Desktop icon positioning service for Windows Desktop.

Provides Win32-level control over Windows Explorer's desktop SysListView32
to retrieve, target, and visually position desktop files/icons on the screen.
"""
import ctypes
from ctypes import wintypes
import difflib
import platform
import re
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

# Win32 Constants for List-View Control
LVM_FIRST = 0x1000
LVM_GETITEMCOUNT = LVM_FIRST + 4         # 0x1004
LVM_SETITEMPOSITION = LVM_FIRST + 15     # 0x100F
LVM_GETITEMPOSITION = LVM_FIRST + 16     # 0x1010
LVM_GETITEMTEXTW = LVM_FIRST + 115       # 0x1073

# Process Memory Access Rights
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
LVIF_TEXT = 0x0001


class LVITEMW(ctypes.Structure):
    """Win32 LVITEMW structure aligned for 64-bit and 32-bit Windows."""
    _fields_ = [
        ("mask", wintypes.UINT),
        ("iItem", ctypes.c_int),
        ("iSubItem", ctypes.c_int),
        ("state", wintypes.UINT),
        ("stateMask", wintypes.UINT),
        ("pszText", ctypes.c_void_p),
        ("cchTextMax", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("lParam", wintypes.LPARAM),
        ("iIndent", ctypes.c_int),
        ("iGroupId", ctypes.c_int),
        ("cColumns", wintypes.UINT),
        ("puColumns", ctypes.c_void_p),
        ("piColFmt", ctypes.c_void_p),
        ("iGroup", ctypes.c_int),
    ]


def get_desktop_listview_hwnd() -> Optional[int]:
    """Find the HWND of the SysListView32 desktop icons window.

    Searches inside Progman or through WorkerW instances spawned by desktop wallpaper.
    """
    if platform.system() != "Windows":
        return None

    user32 = ctypes.windll.user32

    # 1. Check Progman -> SHELLDLL_DefView -> SysListView32
    progman = user32.FindWindowW("Progman", None)
    shell_view = user32.FindWindowExW(progman, 0, "SHELLDLL_DefView", None)

    # 2. If wallpaper or WorkerW is active, search WorkerW windows
    if not shell_view:
        res = (wintypes.HWND * 1)()

        def enum_cb(hwnd, lparam):
            sv = user32.FindWindowExW(hwnd, 0, "SHELLDLL_DefView", None)
            if sv:
                lparam[0] = sv
                return False
            return True

        CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, ctypes.POINTER(wintypes.HWND))
        user32.EnumWindows(CB(enum_cb), res)
        shell_view = res[0]

    if shell_view:
        hwnd = user32.FindWindowExW(shell_view, 0, "SysListView32", None)
        if hwnd:
            return hwnd

    return None


def get_screen_dimensions() -> Tuple[int, int]:
    """Return primary display resolution (width, height) in pixels."""
    if platform.system() != "Windows":
        return 1920, 1080
    user32 = ctypes.windll.user32
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def get_desktop_icons() -> List[Dict[str, Any]]:
    """Enumerate all desktop icons with their names, listview indices, and current positions."""
    if platform.system() != "Windows":
        return []

    hwnd = get_desktop_listview_hwnd()
    if not hwnd:
        return []

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    count = user32.SendMessageW(hwnd, LVM_GETITEMCOUNT, 0, 0)
    if count <= 0:
        return []

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h_proc = kernel32.OpenProcess(
        PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE,
        False,
        pid.value
    )
    if not h_proc:
        return []

    buf_size = 512
    lvitem_size = ctypes.sizeof(LVITEMW)
    point_size = ctypes.sizeof(wintypes.POINT)
    total_size = max(lvitem_size + buf_size, point_size)

    remote_mem = kernel32.VirtualAllocEx(h_proc, None, total_size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
    if not remote_mem:
        kernel32.CloseHandle(h_proc)
        return []

    remote_text = remote_mem + lvitem_size
    icons = []

    try:
        for i in range(count):
            # Read name
            lv = LVITEMW()
            lv.mask = LVIF_TEXT
            lv.iSubItem = 0
            lv.pszText = remote_text
            lv.cchTextMax = 256

            kernel32.WriteProcessMemory(h_proc, remote_mem, ctypes.byref(lv), lvitem_size, None)
            user32.SendMessageW(hwnd, LVM_GETITEMTEXTW, i, remote_mem)

            read_buf = ctypes.create_unicode_buffer(256)
            kernel32.ReadProcessMemory(h_proc, remote_text, read_buf, buf_size, None)
            item_name = read_buf.value.strip()

            # Read position
            user32.SendMessageW(hwnd, LVM_GETITEMPOSITION, i, remote_mem)
            pt = wintypes.POINT()
            kernel32.ReadProcessMemory(h_proc, remote_mem, ctypes.byref(pt), point_size, None)

            icons.append({
                "index": i,
                "name": item_name,
                "x": pt.x,
                "y": pt.y
            })
    finally:
        kernel32.VirtualFreeEx(h_proc, remote_mem, 0, MEM_RELEASE)
        kernel32.CloseHandle(h_proc)

    return icons


def calculate_screen_coordinates(
    position: str,
    screen_width: int,
    screen_height: int,
    offset_index: int = 0
) -> Tuple[int, int]:
    """Calculate pixel coordinates on the primary screen for preset positions.

    Supports offsets so multiple icons moved to the same location do not completely overlap.
    """
    pos = (position or "center").strip().lower().replace("-", "_").replace(" ", "_")

    # Margins and icon dimensions estimate (typical Windows icon grid item: ~80x90px)
    margin_x = 60
    margin_y = 50
    icon_w = 85
    icon_h = 90

    # Grid offset calculations for multi-item placement
    col = offset_index % 4
    row = offset_index // 4
    grid_dx = col * icon_w
    grid_dy = row * icon_h

    # Normalize Arabic/English position synonyms
    if pos in ("center", "middle", "centre", "وسط", "الوسط", "منتصف", "المنتصف", "مركز", "المركز"):
        base_x = (screen_width // 2) - 45
        base_y = (screen_height // 2) - 45
    elif pos in ("top", "أعلى", "اعلى", "فوق", "الأعلى", "الاعلى"):
        base_x = (screen_width // 2) - 45
        base_y = margin_y
    elif pos in ("bottom", "أسفل", "اسفل", "تحت", "الأسفل", "الاسفل"):
        base_x = (screen_width // 2) - 45
        base_y = screen_height - 140
    elif pos in ("left", "يسار", "اليسار", "شمال", "الشمال"):
        base_x = margin_x
        base_y = (screen_height // 2) - 45
    elif pos in ("right", "يمين", "اليمين"):
        base_x = screen_width - 140
        base_y = (screen_height // 2) - 45
    elif pos in ("top_left", "أعلى_اليسار", "اعلى_اليسار", "فوق_يسار"):
        base_x = margin_x
        base_y = margin_y
    elif pos in ("top_right", "أعلى_اليمين", "اعلى_اليمين", "فوق_يمين"):
        base_x = screen_width - 140
        base_y = margin_y
    elif pos in ("bottom_left", "أسفل_اليسار", "اسفل_اليسار", "تحت_يسار"):
        base_x = margin_x
        base_y = screen_height - 140
    elif pos in ("bottom_right", "أسفل_اليمين", "اسفل_اليمين", "تحت_يمين"):
        base_x = screen_width - 140
        base_y = screen_height - 140
    else:
        # Default to center
        base_x = (screen_width // 2) - 45
        base_y = (screen_height // 2) - 45

    target_x = max(10, min(screen_width - 90, base_x + grid_dx))
    target_y = max(10, min(screen_height - 90, base_y + grid_dy))
    return target_x, target_y


def position_desktop_icon(
    target: str,
    position: Optional[str] = None,
    x: Optional[int] = None,
    y: Optional[int] = None
) -> Dict[str, Any]:
    """Visually move one or more desktop icons to specific screen coordinates or presets.

    Args:
        target: Name of the desktop icon/file (e.g. 'her', 'main', 'PlayerMusic') or 'all'.
        position: Preset name: 'center', 'top', 'bottom', 'left', 'right',
                  'top_left', 'top_right', 'bottom_left', 'bottom_right'.
        x: Optional explicit X screen coordinate.
        y: Optional explicit Y screen coordinate.

    Returns:
        Dict with status, moved items list, coordinates, and friendly messages.
    """
    if platform.system() != "Windows":
        return {
            "success": False,
            "error": "Desktop icon positioning is supported only on Windows OS."
        }

    hwnd = get_desktop_listview_hwnd()
    if not hwnd:
        return {
            "success": False,
            "error": "Could not locate Windows Desktop SysListView32 window handle."
        }

    all_icons = get_desktop_icons()
    if not all_icons:
        return {
            "success": False,
            "error": "No icons detected on the Windows Desktop."
        }

    clean_target = (target or "").strip()
    is_all = clean_target.lower() in ("all", "all icons", "الكل", "كل الايقونات", "جميع الايقونات", "كل الملفات")

    # Match target icon(s)
    matching_icons = []
    if is_all:
        matching_icons = all_icons
    else:
        target_lower = clean_target.lower()
        # 1. Exact match
        matching_icons = [ic for ic in all_icons if ic["name"].lower() == target_lower]

        # 2. Match without file extension (e.g. 'her' matches 'her.txt' or 'her.pdf')
        if not matching_icons:
            matching_icons = [
                ic for ic in all_icons
                if Path(ic["name"]).stem.lower() == target_lower
            ]

        # 3. Substring match
        if not matching_icons:
            matching_icons = [
                ic for ic in all_icons
                if target_lower in ic["name"].lower()
            ]

        # 4. Fuzzy sequence match
        if not matching_icons:
            best_match = None
            best_ratio = 0.60
            for ic in all_icons:
                ratio = difflib.SequenceMatcher(None, target_lower, ic["name"].lower()).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_match = ic
            if best_match:
                matching_icons = [best_match]

    if not matching_icons:
        available = [ic["name"] for ic in all_icons[:8]]
        avail_str = ", ".join(f"'{name}'" for name in available)
        return {
            "success": False,
            "error": f"Icon '{clean_target}' not found on desktop. Available sample: {avail_str}"
        }

    sw, sh = get_screen_dimensions()
    user32 = ctypes.windll.user32

    moved_records = []
    effective_pos = position or "center"

    for idx, icon in enumerate(matching_icons):
        if x is not None and y is not None:
            tx = int(x) + (idx * 85)
            ty = int(y)
        else:
            tx, ty = calculate_screen_coordinates(effective_pos, sw, sh, offset_index=idx)

        # MAKELPARAM: loword = x, hiword = y
        lparam = (int(tx) & 0xFFFF) | ((int(ty) & 0xFFFF) << 16)
        user32.SendMessageW(hwnd, LVM_SETITEMPOSITION, icon["index"], lparam)

        moved_records.append({
            "name": icon["name"],
            "index": icon["index"],
            "old_x": icon["x"],
            "old_y": icon["y"],
            "new_x": tx,
            "new_y": ty
        })

    # Force desktop window to repaint immediately
    user32.InvalidateRect(hwnd, None, True)
    user32.UpdateWindow(hwnd)

    first_item = moved_records[0]["name"]
    pos_desc = effective_pos.replace("_", " ").title() if (x is None or y is None) else f"({x}, {y})"

    return {
        "success": True,
        "target": clean_target,
        "position": effective_pos,
        "moved_count": len(moved_records),
        "items": [r["name"] for r in moved_records],
        "coordinates": (moved_records[0]["new_x"], moved_records[0]["new_y"]),
        "message": f"Successfully moved '{first_item}' to {pos_desc}." if len(moved_records) == 1 else f"Successfully moved {len(moved_records)} icons to {pos_desc}."
    }
