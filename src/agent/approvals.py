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
    ):
        self._runner = runner
        self._ttl = float(ttl)
        self._clock = clock
        self._dispatch = dispatch
        self._lock = threading.RLock()
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

    def _notify(self, event: str, req: ApprovalRequest) -> None:
        for listener in list(self._listeners):
            try:
                listener(event, req)
            except Exception as err:  # a broken listener must not break approvals
                print(f"[Approvals] listener error on {event}: {err}")

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
            summary=summary or f"Run {tool_name}",
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
            return [r.public_view(now) for r in self._requests.values() if r.status == PENDING]

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
