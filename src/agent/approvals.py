"""Out-of-band approval flow for dangerous tool calls (audit S5a).

A tool call that needs approval becomes an ``ApprovalRequest``:

- its id is an unguessable nonce (``secrets.token_urlsafe``);
- it is bound to a SHA-256 hash of the tool name + the exact (deep-copied) args;
- it expires after ``ttl`` seconds (default 120) and is DENIED on expiry;
- it is single-use: it leaves the ``pending`` state exactly once;
- only the channels in ``approver_channels`` may resolve it; ``voice`` never can.

Nothing blocks while a request is pending. When a request is approved the
registered ``runner`` executes the stored args (never caller-supplied args)
exactly once, on a ``dispatch`` worker (a daemon thread by default).

PIN mode: when ``APPROVAL_PIN_HASH`` is configured, approving requires the PIN via
``verify_pin_and_approve`` (a correct PIN executes immediately, 3 wrong ones deny the
request); plain ``resolve(approve=True)`` is refused. Denying never needs a PIN.
The raw PIN is never stored, logged or returned.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.security import pin as pin_mod

# Origin channels
CHANNEL_LOCAL = "local"
CHANNEL_HUD = "hud"
CHANNEL_TELEGRAM = "telegram"
CHANNEL_VOICE = "voice"

# Who may approve a request, by origin. Voice is never an approver: a spoken
# "yes" could come from the TV, a recording, or the request itself.
APPROVERS_BY_ORIGIN: Dict[str, frozenset] = {
    CHANNEL_TELEGRAM: frozenset({CHANNEL_TELEGRAM, CHANNEL_HUD}),
    CHANNEL_HUD: frozenset({CHANNEL_HUD}),
    CHANNEL_VOICE: frozenset({CHANNEL_HUD, CHANNEL_TELEGRAM}),
    CHANNEL_LOCAL: frozenset({CHANNEL_HUD}),
}

DEFAULT_TTL = 120.0
MIN_HASH_PREFIX = 16  # hex chars a caller must echo back (Telegram callback_data is <= 64 bytes)

# Status values
PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
EXPIRED = "expired"

# Resolve outcomes (in addition to APPROVED / DENIED / EXPIRED)
UNKNOWN = "unknown"
ALREADY_USED = "already_used"
HASH_MISMATCH = "hash_mismatch"
FORBIDDEN_CHANNEL = "forbidden_channel"
PIN_REQUIRED = "pin_required"  # resolve(approve=True) while a PIN is configured
WRONG_PIN = "wrong_pin"
LOCKED = "locked"  # too many wrong PINs: request denied
MAX_PIN_ATTEMPTS = 3


# Session trust: after a PIN-verified approval of one of these, the same kind of interactive,
# medium-risk calls skip the card for ALTRON_SESSION_TRUST_SECONDS. Allow-list only: everything
# else (file_delete/move/write, code_edit, terminal_execute, gmail_send_email, power_control,
# app_close, window_close, browser_close_tab, ...) always asks.
TRUSTED_TOOLS = frozenset({"gui_hotkey", "gui_type", "app_open", "browser_open_url", "browser_switch_tab"})
DEFAULT_TRUST_SECONDS = 180


def _trust_seconds() -> float:
    from src.config.settings import get_config_value
    try:
        return max(0.0, float(get_config_value("ALTRON_SESSION_TRUST_SECONDS", default=str(DEFAULT_TRUST_SECONDS))))
    except ValueError:
        return float(DEFAULT_TRUST_SECONDS)


def args_hash(tool_name: str, args: Dict[str, Any]) -> str:
    """Canonical SHA-256 of tool name + args (key order does not matter)."""
    canonical = json.dumps(
        {"tool": tool_name, "args": args},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=repr,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def summarize_args(args: Dict[str, Any], limit: int = 300) -> str:
    """Short, single-string preview of the args for display (truncated)."""
    try:
        text = json.dumps(args, ensure_ascii=False, sort_keys=True, default=repr)
    except Exception:
        text = repr(args)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _where(path: Any) -> tuple:
    """(file name, friendly parent folder) for a user path."""
    p = str(path or "").strip().strip('"').replace("\\", "/").rstrip("/")
    name = p.rsplit("/", 1)[-1] or p or "?"
    parent = p.rsplit("/", 1)[0].rsplit("/", 1)[-1] if "/" in p else ""
    return name, ("" if parent in ("", "~", ".") else parent)


def describe_action(tool_name: str, args: Dict[str, Any], fallback: str = "") -> str:
    """Plain-language one-line description of a gated call (no raw args or code)."""
    a = args or {}
    name, parent = _where(a.get("path") or a.get("source"))
    loc = f" from {parent}" if parent else ""
    dest_name, dest_parent = _where(a.get("destination"))
    if tool_name == "file_delete":
        return f"Move `{name}`{loc} to Recycle Bin"
    if tool_name == "file_write":
        return f"Write to `{name}`{loc}"
    if tool_name == "file_move":
        return f"Move `{name}`{loc} to {dest_parent or dest_name}"
    if tool_name == "file_copy":
        return f"Copy `{name}`{loc} to {dest_parent or dest_name}"
    if tool_name == "power_control":
        return {"shutdown": "Shut down the PC", "restart": "Restart the PC", "sleep": "Put the PC to sleep",
                "lock": "Lock the PC", "cancel_shutdown": "Cancel the scheduled shutdown"
                }.get(str(a.get("action")), "Change the PC power state")
    if tool_name == "gmail_send_email":
        body = str(a.get("body") or "")
        return (f"Send email\nTo: {a.get('to', '?')}\nSubject: {a.get('subject', '')}\n"
                f"Body: {body[:500]}{'…' if len(body) > 500 else ''}")
    if tool_name == "gmail_read_message":
        return f"Read Gmail message {str(a.get('message_id', '?'))[:40]}"
    if tool_name == "gmail_list_messages":
        return f"List Gmail messages (query: {str(a.get('query') or 'inbox')[:80]})"
    if tool_name == "schedule_task":
        inner = describe_action(str(a.get("action_name") or ""), a.get("action_args") or {}, "Run a tool")
        return f"Schedule for {a.get('run_at_or_delay', 'later')}: {inner}"
    if tool_name in ("terminal_execute", "claude_code_run"):
        said = " ".join(str(a.get("summary") or "").split())
        if said:
            return said[:200]
        text = " ".join(str(a.get("command") or a.get("prompt") or "").split())
        what = "Run a terminal command" if tool_name == "terminal_execute" else "Run a coding task"
        return f"{what}: {text[:80]}{'…' if len(text) > 80 else ''}" if text else what
    return fallback or f"Run {tool_name}"


@dataclass
class ApprovalRequest:
    id: str
    tool_name: str
    args: Dict[str, Any]
    args_hash: str
    channel: str
    summary: str
    created_at: float
    expires_at: float
    approver_channels: frozenset
    status: str = PENDING
    resolved_via: Optional[str] = None
    result: Any = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def public_view(self, now: float) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool_name,
            "summary": self.summary,
            "args_preview": summarize_args(self.args),
            "args_hash": self.args_hash,
            "channel": self.channel,
            "status": self.status,
            "expires_in": max(0, int(round(self.expires_at - now))),
        }


@dataclass
class ResolveResult:
    outcome: str
    request: Optional[ApprovalRequest] = None
    attempts_left: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.outcome in (APPROVED, DENIED)


def _thread_dispatch(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="AltronApprovedTool", daemon=True).start()


Listener = Callable[[str, ApprovalRequest], None]


class ApprovalManager:
    """Thread-safe registry of pending approval requests."""

    def __init__(
        self,
        runner: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        ttl: float = DEFAULT_TTL,
        clock: Callable[[], float] = time.monotonic,
        dispatch: Callable[[Callable[[], None]], None] = _thread_dispatch,
        pin_hash: Optional[Callable[[], str]] = None,
    ):
        self._runner = runner
        self._ttl = float(ttl)
        self._clock = clock
        self._dispatch = dispatch
        # Read live on every call so setting/disabling the PIN needs no restart.
        self._pin_hash = pin_hash or (lambda: pin_mod.configured_hash())
        self._lock = threading.RLock()
        self._trust_until = 0.0
        self._requests: Dict[str, ApprovalRequest] = {}
        self._listeners: List[Listener] = []
        self._sweeper: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # -- wiring ---------------------------------------------------------
    def set_runner(self, runner: Callable[[str, Dict[str, Any]], Any]) -> None:
        self._runner = runner

    def add_listener(self, listener: Listener) -> None:
        """listener(event, request); event is "created", "resolved" (approved,
        denied or expired) or "executed" (approved tool finished; see request.result)."""
        self._listeners.append(listener)

    def grant_trust(self) -> None:
        secs = _trust_seconds()
        self._trust_until = self._clock() + secs if secs > 0 else 0.0

    def revoke_trust(self) -> None:
        self._trust_until = 0.0

    def trust_active(self, tool_name: str) -> bool:
        """True while a PIN-granted trust window is open AND the tool is in the medium-risk allow-list."""
        return tool_name in TRUSTED_TOOLS and self.pin_required and self._clock() < self._trust_until

    def _notify(self, event: str, req: ApprovalRequest) -> None:
        if event == "resolved" and req.status in (DENIED, EXPIRED):
            self.revoke_trust()  # any deny / cancel / expiry / lockout closes the window
        for listener in list(self._listeners):
            try:
                listener(event, req)
            except Exception as err:  # a broken listener must not break approvals
                print(f"[Approvals] listener error on {event}: {err}")

    @property
    def pin_required(self) -> bool:
        return bool(self._pin_hash())

    # -- lifecycle ------------------------------------------------------
    def create_request(self, tool_name: str, args: Dict[str, Any], channel: str, summary: str = "") -> str:
        frozen_args = copy.deepcopy(dict(args or {}))
        now = self._clock()
        req = ApprovalRequest(
            id=secrets.token_urlsafe(16),
            tool_name=tool_name,
            args=frozen_args,
            args_hash=args_hash(tool_name, frozen_args),
            channel=channel,
            summary=describe_action(tool_name, frozen_args, summary),
            created_at=now,
            expires_at=now + self._ttl,
            approver_channels=APPROVERS_BY_ORIGIN.get(channel, frozenset({CHANNEL_HUD})),
        )
        with self._lock:
            self._requests[req.id] = req
        self._notify("created", req)
        return req.id

    def get(self, request_id: str) -> Optional[ApprovalRequest]:
        with self._lock:
            return self._requests.get(request_id)

    def list_pending(self) -> List[Dict[str, Any]]:
        self.sweep()
        now = self._clock()
        with self._lock:
            pin = self.pin_required
            return [
                {**r.public_view(now), "pin_required": pin,
                 "attempts_left": MAX_PIN_ATTEMPTS - r.meta.get("pin_attempts", 0)}
                for r in self._requests.values() if r.status == PENDING
            ]

    def sweep(self) -> List[ApprovalRequest]:
        """Expire (deny) every pending request past its deadline."""
        now = self._clock()
        expired: List[ApprovalRequest] = []
        with self._lock:
            for req in self._requests.values():
                if req.status == PENDING and now >= req.expires_at:
                    req.status = EXPIRED
                    expired.append(req)
            # forget long-finished requests so the dict does not grow forever
            cutoff = now - 10 * self._ttl
            for rid in [r.id for r in self._requests.values() if r.status != PENDING and r.expires_at < cutoff]:
                del self._requests[rid]
        for req in expired:
            self._notify("resolved", req)
        return expired

    def resolve(self, request_id: str, approve: bool, via: str, expected_hash: str) -> ResolveResult:
        """Approve or deny a request. Only the first valid call has any effect."""
        if not isinstance(request_id, str) or not isinstance(expected_hash, str):
            return ResolveResult(UNKNOWN)
        self.sweep()
        with self._lock:
            req = self._requests.get(request_id)
            if req is None:
                return ResolveResult(UNKNOWN)
            if req.status == EXPIRED:
                return ResolveResult(EXPIRED, req)
            if req.status != PENDING:
                return ResolveResult(ALREADY_USED, req)
            if via not in req.approver_channels:
                return ResolveResult(FORBIDDEN_CHANNEL, req)
            if approve and self.pin_required:
                return ResolveResult(PIN_REQUIRED, req)
            if len(expected_hash) < MIN_HASH_PREFIX or not hmac.compare_digest(
                req.args_hash[: len(expected_hash)].encode(), expected_hash.encode()
            ):
                # The approver saw different args than we would run: treat as tampering.
                req.status = DENIED
                req.resolved_via = via
                outcome = HASH_MISMATCH
            else:
                req.status = APPROVED if approve else DENIED
                req.resolved_via = via
                outcome = req.status

        self._notify("resolved", req)
        if outcome == APPROVED:
            self._dispatch(lambda: self._run(req))
        return ResolveResult(outcome, req)

    def cancel(self, request_id: str, via: str) -> ResolveResult:
        """Deny a pending request (the PIN-mode replacement for the Deny button)."""
        req = self.get(request_id)
        return self.resolve(request_id, False, via, req.args_hash if req else "")

    def verify_pin_and_approve(self, request_id: str, pin: Any, via: str) -> ResolveResult:
        """Approve and run immediately if ``pin`` is right; 3 wrong PINs deny the request."""
        stored = self._pin_hash()
        if not stored:
            return ResolveResult("no_pin")
        if not isinstance(request_id, str):
            return ResolveResult(UNKNOWN)
        self.sweep()
        with self._lock:
            req = self._requests.get(request_id)
            if req is None:
                return ResolveResult(UNKNOWN)
            if req.status == EXPIRED:
                return ResolveResult(EXPIRED, req)
            if req.status != PENDING:
                return ResolveResult(ALREADY_USED, req)
            if via not in req.approver_channels:
                return ResolveResult(FORBIDDEN_CHANNEL, req)
            if pin_mod.verify_pin(pin, stored):
                req.status, req.resolved_via = APPROVED, via
                outcome, left = APPROVED, None
            else:
                used = req.meta["pin_attempts"] = req.meta.get("pin_attempts", 0) + 1
                left = MAX_PIN_ATTEMPTS - used
                if left > 0:
                    self.revoke_trust()
                    return ResolveResult(WRONG_PIN, req, left)
                req.status, req.resolved_via = DENIED, via
                outcome = LOCKED
        if outcome == APPROVED and req.tool_name in TRUSTED_TOOLS:
            self.grant_trust()
        self._notify("resolved", req)
        if outcome == APPROVED:
            self._dispatch(lambda: self._run(req))
        return ResolveResult(outcome, req, left)

    def _run(self, req: ApprovalRequest) -> None:
        # Defence in depth: the stored args must still match the approved hash.
        if args_hash(req.tool_name, req.args) != req.args_hash or self._runner is None:
            req.result = None
            req.meta["run_error"] = "integrity check failed" if self._runner else "no runner"
        else:
            try:
                req.result = self._runner(req.tool_name, copy.deepcopy(req.args))
            except Exception as err:
                req.meta["run_error"] = str(err)
        self._notify("executed", req)

    # -- background expiry ------------------------------------------------
    def start_sweeper(self, interval: float = 5.0) -> None:
        if self._sweeper is not None:
            return

        def _loop():
            while not self._stop.wait(interval):
                try:
                    self.sweep()
                except Exception as err:
                    print(f"[Approvals] sweep error: {err}")

        self._sweeper = threading.Thread(target=_loop, name="AltronApprovalSweeper", daemon=True)
        self._sweeper.start()

    def stop(self) -> None:
        self._stop.set()
