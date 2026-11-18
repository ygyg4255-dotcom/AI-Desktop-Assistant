---
name: permission-gate-review
description: Reference for the S3/S4 permission gate rules in src/agent. Background knowledge to apply when adding or changing tools.
user-invocable: false
---

# Permission gate rules (audit S3, S4, S5a)

Source of truth: `src/agent/permissions.py`, `src/agent/tools.py` (`Tool.run`),
`src/agent/executor.py`, `src/agent/approvals.py`. Tests: `tests/test_permission_bypass.py`,
`tests/test_safe_tools.py`, `tests/test_approvals.py`.

## S3: model input can never grant approval
- Every key starting with `_` is stripped from model-supplied tool input at the single
  dispatch point, `Tool.run`. A model-sent `"_approved": true` must have no effect.
- Approval is passed only as the `approved` keyword argument, and only by the executor
  after a real user decision.
- A new tool that requires permission must not be runnable unprompted by omission:
  default to *asking*, never to allowing.

## S4: tiers
- `SAFE_TOOLS`: auto-run. Keep this list minimal. Anything here receives `approved=True`
  unconditionally, so a tool whose safety depends on its input does NOT belong here.
- `CONDITIONAL_TOOLS` (`file_read`, `file_open`, `app_open`, `send_to_telegram`): the
  handler decides per call via `needs_approval_*` (sensitive paths such as `.ssh`, `.aws`,
  browser profiles, key files, `memory.json`/`status.json`; launchable or scriptable file
  types; non-plain app names). A blanket "allow" (session or remembered) is refused for these.
- `DANGEROUS_TOOLS` (`file_write`, `file_delete`, `file_move`, `file_copy`,
  `terminal_execute`, `app_close`, `code_edit`, ...): always require approval.
- `web_fetch` is safe to auto-run only because SSRF rules are enforced unconditionally in
  its handler: http(s) only, public addresses only, re-validated on every redirect, 2 MB cap.

## S5a: approval delivery
- Approvals are non-blocking and out-of-band through `ApprovalManager`.
- Voice origin can never approve. Telegram approvals are owner-only. HUD approvals need the
  per-launch HUD token.
- Approvals are bound to a hash of the tool arguments; a changed argument set must not
  reuse an earlier approval.

## Checklist for a new or changed tool
1. Which tier? Default to DANGEROUS unless it is provably read-only and low-risk.
2. Does any input choose a path, URL, command, or app? Add a per-call check, not a tier bump.
3. Does the executor have a direct `registry.execute` path that skips the gate? There must be none.
4. Add a regression test that a model-supplied `_approved` / approval-looking key is ignored.
