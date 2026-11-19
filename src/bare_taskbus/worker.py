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

Lease semantics, measured on the gateway and not negotiable from this side:

  * LEASE_SECONDS = 900. A claim holds the row for 15 minutes. The value is a module
    constant on the server: it is not configurable per claim, per key or per tenant.
  * The lease IS renewable, by heartbeat. While the handler runs this worker POSTs
    /v1/taskbus/rows/{id}/heartbeat with the claim nonce every heartbeat_interval
    seconds (default LEASE_SECONDS / 3 = 300), and the gateway pushes
    lease_expires_at forward. A PATCH still does not extend the lease; only the
    heartbeat route does.
  * Renewal is BOUNDED. Heartbeats extend a claim only up to
    HEARTBEAT_HORIZON_SECONDS (7200 s = 2 h) counted from the claim itself. Past that
    horizon the claim lapses and the row must be re-claimed, which mints a NEW nonce
    and spends another attempt. The route does not report a per-tenant horizon, so the
    bound is a published constant.
  * A 409 from the heartbeat does NOT revive the claim. It means the claim is no
    longer live - the row left InProgress, or the lease already lapsed and the nonce
    is stale - and every later write with that nonce will be refused too. This worker
    stops heartbeating, abandons the handler result and does NOT write back: a write
    would only earn a second 409 and a misleading log line.
  * With heartbeats enabled the handler watchdog is no longer lease-bound. The
    default becomes HEARTBEAT_HORIZON_SECONDS - HEARTBEAT_MARGIN_SECONDS, so a run
    still stops before the renewal horizon; TASKBUS_JOB_WATCHDOG overrides it. With
    heartbeats disabled (TASKBUS_HEARTBEAT=0) the watchdog is 840 s, one minute short
    of the lease, which is the pre-0.1.2 behaviour.
  * Recursion is bounded by the ROW CHAIN, not by a counter. Before dispatching, the
    worker walks parent_task; a row at or beyond MAX_SPAWN_DEPTH (3) is claimed and
    immediately released as Blocked, so it leaves the queue with a reason instead of
    being picked up every cycle. That spends one attempt - the same trade the fleet
    poller makes, for the same reason: the gateway authorises a status write with the
    claim nonce, and an unclaimed row has none.
  * A handler may return a SUPERVISOR_RESULT envelope in its summary. When it does,
    the envelope decides the close, and never as Completed (see supervisor.py).
"""
import logging
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Callable, List, Optional

from .client import (HEARTBEAT_HORIZON_SECONDS, LEASE_SECONDS, MAX_SPAWN_DEPTH,
                     TaskbusClient, Row)
from .errors import TaskbusConflict, TaskbusError
from .supervisor import parse_supervisor_result, supervisor_close_body

log = logging.getLogger("bare_taskbus.worker")

REFRESH_RATIO = 0.8
MIN_REFRESH_SECONDS = 60
# Watchdog used when heartbeats are DISABLED: 14 minutes, a minute short of the lease.
JOB_WATCHDOG_SECONDS = 840
# Stop a little before the renewal horizon so the closing write still lands inside it.
HEARTBEAT_MARGIN_SECONDS = 300


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
                 interval=None, max_concurrent=1, refresh=None, identity=None,
                 watchdog=None):
        self.client = client
        self.handler = handler
        self.interval = int(interval or os.environ.get("TASKBUS_POLL_INTERVAL") or 60)
        self.max_concurrent = max(1, int(max_concurrent))
        self.refresh_enabled = _env_flag("TASKBUS_REFRESH", True) if refresh is None else bool(refresh)
        self.identity = identity or os.environ.get("TASKBUS_IDENTITY") or ""
        # Run the handler under a watchdog that expires before the server lease does.
        self.heartbeat_enabled = _env_flag("TASKBUS_HEARTBEAT", True)
        # A third of the lease, so two beats can be missed before the claim is at risk.
        self.heartbeat_interval = int(os.environ.get("TASKBUS_HEARTBEAT_INTERVAL")
                                      or max(30, LEASE_SECONDS // 3))
        default_watchdog = (max(60, HEARTBEAT_HORIZON_SECONDS - HEARTBEAT_MARGIN_SECONDS)
                            if self.heartbeat_enabled else JOB_WATCHDOG_SECONDS)
        self.watchdog = int(watchdog or os.environ.get("TASKBUS_JOB_WATCHDOG")
                            or default_watchdog)
        self._child = None
        self._child_expires_at = 0.0
        self._stopped = False
        self._claim_lost = False

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
        summary = {"claimed": 0, "completed": 0, "blocked": 0, "skipped": 0,
                   "lost": 0, "error": None}
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
            # Recursion guard BEFORE dispatch: a row at or beyond the depth limit
            # never reaches the handler, not even once.
            depth, depth_problem = client.spawn_depth(row.id)
            if depth_problem:
                log.warning("spawn depth NOT computed for row %s (%s)", row.id, depth_problem)
            refuse_depth = depth >= MAX_SPAWN_DEPTH
            claim = None
            try:
                claim = client.claim_task(row.id)
                summary["claimed"] += 1
                log.info("claimed row %s (attempt data in claim)", row.id)
            except TaskbusError as exc:
                summary["error"] = "claim: %s" % exc
                log.warning("could not claim row %s: %s", row.id, exc)
                continue
            if refuse_depth:
                reason = ("spawn depth %d has reached the limit of %d"
                          % (depth, MAX_SPAWN_DEPTH))
                log.error("row %s: REFUSED at claim time - %s (no handler will run)",
                          row.id, reason)
                try:
                    client.release_task(claim, reason)
                    summary["blocked"] += 1
                except TaskbusError as exc:
                    summary["error"] = "depth refusal write-back: %s" % exc
                    log.warning("row %s: depth refusal could not be written: %s", row.id, exc)
                continue
            try:
                self._claim_lost = False
                result = self._run_handler(row, client, claim)
            except Exception:
                tail = traceback.format_exc().strip().splitlines()[-4:]
                result = HandlerResult(status="Blocked", summary="handler raised",
                                       reason=" | ".join(tail)[:500])
            if self._claim_lost:
                # The heartbeat was refused with 409: the claim is gone and the
                # nonce no longer authorises anything, so writing would only earn a
                # second 409. Leave the row to the gateway's own reclamation.
                log.warning("row %s: claim lost mid-handler; result discarded, not written",
                            row.id)
                summary["lost"] += 1
                continue
            body = supervisor_close_body(parse_supervisor_result(result.summary or ""),
                                         log_excerpt=result.summary or "")
            if body:
                # A supervisor envelope outranks the handler's own status hint, and
                # never says Completed.
                try:
                    client.update_task(claim, status=body["status"],
                                       machines_response=body["machines_response"],
                                       reason=body.get("re_opened_or_retry_reason"))
                except TaskbusError as exc:
                    summary["error"] = "write-back (supervisor): %s" % exc
                    log.warning("supervisor write-back failed for row %s: %s", row.id, exc)
                    continue
                summary["blocked" if body["status"] == "Blocked" else "completed"] += 1
                continue
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

    def _heartbeat_loop(self, claim, stop_event):
        """Renew the lease until the handler finishes or the claim is lost.

        A 409 is terminal: it means the row is no longer InProgress or the nonce is
        stale, and the gateway will refuse the closing write with that nonce too. The
        loop stops and raises the lost flag so the caller discards the result instead
        of attempting a write that cannot land.
        """
        while not stop_event.wait(self.heartbeat_interval):
            try:
                payload = self.client.heartbeat(claim)
            except TaskbusConflict as exc:
                log.warning("row %s: heartbeat refused (409) - claim is no longer live, "
                            "not reviving it: %s", claim.row_id, exc)
                self._claim_lost = True
                return
            except TaskbusError as exc:
                # A transport blip is not a lost claim: keep trying until the lease
                # horizon, so a single failed beat does not abandon the work.
                log.warning("row %s: heartbeat failed (will retry): %s", claim.row_id, exc)
                continue
            log.debug("row %s: lease renewed %s", claim.row_id, payload.get("lease_expires_at"))

    def _run_handler(self, row, client, claim=None):
        """Run the handler under a watchdog, renewing the lease while it works.

        Renewal is bounded by HEARTBEAT_HORIZON_SECONDS, so the watchdog defaults to
        just inside that horizon rather than just inside one lease. A thread cannot be
        killed in Python, so an overrunning handler is abandoned - the row is released
        as Blocked and the process exits once that thread finishes.
        """
        stop_event = threading.Event()
        beat = None
        if claim is not None and self.heartbeat_enabled:
            beat = threading.Thread(target=self._heartbeat_loop, args=(claim, stop_event),
                                    name="taskbus-heartbeat", daemon=True)
            beat.start()
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(self.handler, row, client)
        try:
            return future.result(timeout=self.watchdog) or HandlerResult()
        except FuturesTimeout:
            log.warning("handler for row %s exceeded watchdog of %ss", row.id, self.watchdog)
            return HandlerResult(
                status="Blocked",
                summary="handler exceeded the %ss watchdog" % self.watchdog,
                reason=("no result within %ss; released before the %ss lease could lapse"
                        % (self.watchdog, LEASE_SECONDS)))
        finally:
            stop_event.set()
            if beat is not None:
                beat.join(timeout=5)
            pool.shutdown(wait=False)


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
