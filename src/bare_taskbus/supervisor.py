"""SUPERVISOR_RESULT - the contract a supervised runner prints when it finishes.

A supervisor runs a task in passes and ends with an outcome, not a status. It
prints ONE line to stdout:

    SUPERVISOR_RESULT {"outcome": "...", "reason": "...", "passes": 2,
                       "state_path": "...", "status_hint": "HumanReviewNeeded"}

The JSON is single-line and may be buried among other output, so the parser scans
for the LAST line carrying the prefix rather than assuming position.

Two rules are load-bearing and are enforced here rather than left to a caller:

  * an executor never decides Completed. A verified outcome still goes to a human,
    so the close body is always HumanReviewNeeded or Blocked.
  * a Blocked close carries a non-empty re_opened_or_retry_reason, because the
    gateway refuses that transition without one.
"""
import json
from typing import Optional

SUPERVISOR_RESULT_PREFIX = "SUPERVISOR_RESULT "
SUPERVISOR_TERMINAL_STATUSES = ("HumanReviewNeeded", "Blocked")


def parse_supervisor_result(text: str) -> Optional[dict]:
    """Return the last SUPERVISOR_RESULT envelope in text, or None.

    Total by design: a malformed envelope returns None instead of raising, because
    this runs on a failure path where a crash would hide the real fault.
    """
    found = None
    for line in (text or "").splitlines():
        if not line.startswith(SUPERVISOR_RESULT_PREFIX):
            continue
        try:
            payload = json.loads(line[len(SUPERVISOR_RESULT_PREFIX):])
        except ValueError:
            continue
        if isinstance(payload, dict):
            found = payload
    return found


def supervisor_close_body(parsed: Optional[dict], log_excerpt: str = "") -> Optional[dict]:
    """Map an envelope onto a PATCH body, or None when there is nothing to map.

    Never returns Completed: verified work still goes to a human. A non-verified
    outcome goes to HumanReviewNeeded too, because it needs a decision rather than
    another silent attempt.
    """
    if not isinstance(parsed, dict):
        return None
    hint = str(parsed.get("status_hint") or "HumanReviewNeeded")
    if hint not in SUPERVISOR_TERMINAL_STATUSES:
        hint = "HumanReviewNeeded"
    lines = [
        "supervisor outcome=%s passes=%s" % (parsed.get("outcome"), parsed.get("passes")),
        "reason: %s" % str(parsed.get("reason") or "")[:400],
        "state: %s" % str(parsed.get("state_path") or ""),
    ]
    if log_excerpt:
        lines.append("log tail: " + str(log_excerpt)[-800:])
    body = {"status": hint, "machines_response": chr(10).join(lines)}
    if hint == "Blocked":
        body["re_opened_or_retry_reason"] = str(parsed.get("reason") or "supervisor blocked")[:400]
    return body
