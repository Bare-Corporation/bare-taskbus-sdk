"""Error types mapped from the gateway's own error strings.

The gateway answers with a JSON body whose error key is "error" (not "detail").
Callers branch on the exception type, never on message text.
"""


class TaskbusError(Exception):
    """Base error. Carries the HTTP status and the gateway's raw error code."""

    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class TaskbusAuthError(TaskbusError):
    """401 missing/invalid key, 403 key bound to another address, revoked or suspended."""


class TaskbusForbidden(TaskbusError):
    """403 insufficient scope, not a reviewer, self certification, tenant scope, creator only."""


class TaskbusConflict(TaskbusError):
    """409 row not claimable, token consumed, not in review, live lease, already reviewed."""


class TaskbusValidation(TaskbusError):
    """400/422 unknown verdict, verdict/status conflict, ttl out of range, reason required."""


class TaskbusUnavailable(TaskbusError):
    """503 auth unavailable or database unreachable."""


_AUTH = ("missing api key", "invalid api key", "key_bound_to_other_address",
         "key_revoked", "key_suspended")
_FORBIDDEN = ("insufficient_scope", "forbidden_not_reviewer", "self_certification_forbidden",
              "non_exchangeable", "tenant_scope_violation", "creator_only", "machine_paused")
_CONFLICT = ("row_not_claimable", "token_consumed", "row_not_in_review", "row_carries_live_lease",
             "row_already_reviewed", "row_not_blocked", "row_is_claimed", "row_not_in_token")
_VALIDATION = ("unknown_verdict", "verdict_status_conflict", "ttl_out_of_range",
               "comments_too_long", "reason_required", "malformed_body")


def error_for(status, code, message):
    """Map a gateway response to the narrowest exception type."""
    text = str(message or "")
    low = text.lower()
    if status in (401, 403):
        if code in _FORBIDDEN or any(t in low for t in _FORBIDDEN):
            return TaskbusForbidden(text, status, code)
        return TaskbusAuthError(text, status, code)
    if status == 409:
        return TaskbusConflict(text, status, code)
    if status in (400, 422):
        return TaskbusValidation(text, status, code)
    if status >= 500 or status == 0:
        return TaskbusUnavailable(text, status, code)
    return TaskbusError(text, status, code)
