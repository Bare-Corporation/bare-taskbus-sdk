"""TaskbusWorker - a gateway-only polling loop with no fleet dependencies.

Runs from environment variables alone:

    TASKBUS_GATEWAY_BASE   default https://api.bare-ai.net
    TASKBUS_API_KEY        the machine key
    TASKBUS_POLL_INTERVAL  seconds between cycles, default 60
    TASKBUS_REFRESH        "1" to hold an ephemeral child key and rotate it (default 1 when the
                           parent key is not ephemeral)

The loop order matters and each step exists because its absence caused a real failure:
  1. preflight: if a list call fails, do not claim. A worker that claims and then cannot write is
     worse than an idle one, because the row is then held for the whole lease.
  2. credential: exchange a child key at about 80 percent of the server-reported TTL. If the
     exchange fails, fail CLOSED - release held rows as Blocked and stop claiming.
  3. list, filter by status, claim at most max_concurrent rows.
  4. dispatch to the handler, then write the result back with the claim nonce.
  5. any handler exception becomes status=Blocked with a short trace tail. Never leave a claimed row
     to rot until its lease lapses: the lease is a backstop, not a workflow.
"""
import logging
import os
import time
import traceback
from dataclasses import dataclass
from typing import Callable, List, Optional

from .client import TaskbusClient, Row
from .errors import TaskbusError

log = logging.getLogger("bare_taskbus.worker")

REFRESH_RATIO = 0.8
MIN_REFRESH_SECONDS = 60


@dataclass
class HandlerResult:
    status: str = "HumanReviewNeeded"
    summary: str = ""
    reason: Optional[str] = None


def _env_flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


class TaskbusWorker:
    def __init__(self, client: TaskbusClient, handler: Callable[[Row, TaskbusClient], HandlerResult],
                 interval=None, max_concurrent=1, refresh=None, identity=None):
        self.client = client
        self.handler = handler
        self.interval = int(interval or os.environ.get("TASKBUS_POLL_INTERVAL") or 60)
        self.max_concurrent = max(1, int(max_concurrent))
        self.refresh_enabled = _env_flag("TASKBUS_REFRESH", True) if refresh is None else bool(refresh)
        self.identity = identity or os.environ.get("TASKBUS_IDENTITY") or ""
        self._child = None
        self._child_expires_at = 0.0
        self._stopped = False

    # ---- credential ------------------------------------------------------
    def ensure_credential(self):
        if not self.refresh_enabled:
            return self.client.api_key
        now = time.time()
        if self._child and now < self._child_expires_at:
            return self._child
        data = self.client.exchange_key()
        self._child = data.get("api_key") or self._child
        ttl_minutes = int(data.get("ttl_minutes") or 15)
        ttl_seconds = ttl_minutes * 60
        self._child_expires_at = now + max(MIN_REFRESH_SECONDS, int(ttl_seconds * REFRESH_RATIO))
        log.info("credential: ephemeral key id=%s ttl=%ss next_refresh_in=%ss",
                 data.get("key_id"), ttl_seconds, int(self._child_expires_at - now))
        return self._child

    def _with_child(self):
        """Temporarily present the child key, if one is held."""
        return TaskbusClient(api_key=self.ensure_credential(), base_url=self.client.base_url,
                             user_agent=self.client.user_agent)

    # ---- one cycle -------------------------------------------------------
    def run_once(self) -> dict:
        summary = {"claimed": 0, "completed": 0, "blocked": 0, "skipped": 0, "error": None}
        client = self._with_child()
        try:
            rows = client.list_tasks(status="new", page_size=100)
        except TaskbusError as exc:
            summary["error"] = "preflight: %s" % exc
            log.warning("preflight failed, not claiming: %s", exc)
            return summary

        claimable = [r for r in rows if self._mine(r)]
        summary["skipped"] = len(rows) - len(claimable)
        for row in claimable[: self.max_concurrent]:
            if self._stopped:
                break
            claim = None
            try:
                claim = client.claim_task(row.id)
                summary["claimed"] += 1
                log.info("claimed row %s (attempt data in claim)", row.id)
            except TaskbusError as exc:
                summary["error"] = "claim: %s" % exc
                log.warning("could not claim row %s: %s", row.id, exc)
                continue
            try:
                result = self.handler(row, client) or HandlerResult()
            except Exception:
                tail = traceback.format_exc().strip().splitlines()[-4:]
                result = HandlerResult(status="Blocked", summary="handler raised",
                                       reason=" | ".join(tail)[:500])
            try:
                client.update_task(claim, status=result.status,
                                   machines_response=result.summary, reason=result.reason)
                if result.status == "Blocked":
                    summary["blocked"] += 1
                else:
                    summary["completed"] += 1
            except TaskbusError as exc:
                summary["error"] = "write-back: %s" % exc
                log.warning("write-back failed for row %s: %s", row.id, exc)
        return summary

    def _mine(self, row: Row) -> bool:
        """Open mode claims anything unowned; a set identity claims only its own rows."""
        if not self.identity:
            return True
        assigned = row.assigned
        if isinstance(assigned, list):
            return self.identity in assigned
        return assigned == self.identity

    # ---- loop ------------------------------------------------------------
    def run(self):
        log.info("worker starting: base=%s interval=%ss refresh=%s",
                 self.client.base_url, self.interval, self.refresh_enabled)
        try:
            while not self._stopped:
                summary = self.run_once()
                log.info("cycle end: %s", summary)
                if self._stopped:
                    break
                time.sleep(self.interval)
        except KeyboardInterrupt:
            log.info("interrupted, stopping")

    def stop(self):
        self._stopped = True


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not _env_flag("POLLER_GATEWAY_ONLY", True):
        log.warning("POLLER_GATEWAY_ONLY is not set: this worker only ever talks to the gateway, "
                    "so set it to true to make that explicit")
    client = TaskbusClient()
    worker = TaskbusWorker(client, handler=_example_handler)
    worker.run()


def _example_handler(row, client):
    """Replace with your own logic. The default refuses, so nothing is ever silently claimed."""
    log.warning("no handler configured; releasing row %s", row.id)
    return HandlerResult(status="Blocked", summary="no handler configured",
                         reason="bare-taskbus-sdk worker has no handler installed")
