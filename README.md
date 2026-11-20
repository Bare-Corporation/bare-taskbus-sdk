# bare-taskbus

Standalone Python client, worker and MCP server for the Bare-AI Task Bus: claim work, do it, hand it to
review.

One dependency (httpx). No Vault, no database client, no SSH, no fleet configuration. It talks to one
gateway over HTTPS with one API key, which is all an external agent needs.

Who this is for: an external agent (Claude Desktop or Code over MCP, an OpenAI Swarm or LangChain
worker, or a plain cron script) that needs to claim tickets, report progress and submit work for review
without touching the cluster's internals.

## Install

    pip install bare-taskbus
    pip install bare-taskbus[mcp]     # adds the Model Context Protocol server

## Configure

Environment only. There is no config file and no on-disk key.

    TASKBUS_GATEWAY_BASE   default https://api.bare-ai.net
    TASKBUS_API_KEY        the machine key issued at onboarding (bck- prefix)
    TASKBUS_USER_AGENT     optional override

Every request sends a User-Agent. The public edge answers 403 to a request without one, which looks
like a permissions problem and is not.

## Quickstart: a script that claims one task and finishes it

    from bare_taskbus import TaskbusClient, HandlerResult, TaskbusWorker

    def handler(row, client):
        # do the work here; row.success_criterion tells you what must exist afterwards
        return HandlerResult(status="HumanReviewNeeded", summary="ran the probe; artefact at /tmp/out.txt")

    with TaskbusClient() as client:
        TaskbusWorker(client, handler, interval=60).run()

A worker claims only rows that are New, writes its result back with the claim nonce the client keeps for
you, and blocks a row rather than losing it when the handler raises.

## Quickstart: direct calls

    from bare_taskbus import TaskbusClient

    client = TaskbusClient()
    for row in client.list_tasks():                      # New + InProgress
        print(row.id, row.status, row.priority, row.short_description)

    # status is a REPEATABLE filter, so a list means "either of these".
    # A single label, a comma-joined string, a list, or None (no filter) all work,
    # and status=["all"] disables the filter entirely.
    for row in client.list_tasks(status=["new", "InProgress"], page_size=50):
        print(row.id, row.status)

    claim = client.claim_task(514)
    client.update_task(claim, status="HumanReviewNeeded",
                       machines_response="what I read and what I ran")

    # review needs a dispatcher key or the compliance seat, and the row must be in HumanReviewNeeded
    client.review_task(514, verdict="Passed", status="Completed", comments="21/21 tests OK")

## Tools for Claude Desktop and Claude Code

Add to the MCP host configuration:

    {
      "mcpServers": {
        "bare-taskbus": {
          "command": "bare-taskbus-mcp",
          "env": {
            "TASKBUS_GATEWAY_BASE": "https://api.bare-ai.net",
            "TASKBUS_API_KEY": "bck-your-machine-key",
            "TASKBUS_IS_DISPATCHER": "false"
          }
        }
      }
    }

Tools exposed: list_available_tasks, claim_task, create_subtask, and submit_for_review. The last one is
only registered when TASKBUS_IS_DISPATCHER is true, because a tool that always answers 403 teaches the
model to distrust the tool set. No tool accepts an API key argument: the server holds the key, so a prompt
cannot exfiltrate it by calling a tool.

## Rules this client encodes for you

These are not style choices. Each one is a failure that costs real attempts to discover:

1. The claim nonce is a HEADER (X-Claim-Nonce), never a JSON field. A nonce in the body is ignored and
   every write answers 409 "Stale or missing claim nonce", which reads like an expired lease.
2. Pagination is page and page_size. Do not send size: some gateway builds accept it and ignore it, so
   you silently page a smaller dataset and conclude the queue is empty.
3. An executor may set InProgress, HumanReviewNeeded or Blocked. Completed belongs to the review route
   and is refused to an executor, so the client refuses it locally rather than spending a call.
4. Passed pairs with Completed and Failed pairs with Blocked. Any other pairing is 422
   verdict_status_conflict, so review_task validates before sending.
5. An ephemeral child key has a 15 minute floor. exchange_key(ttl_minutes=5) is a 422, not a clamp.
6. Short-lived keys should be rotated at about 80 percent of the advertised TTL, and a failed rotation
   must fail closed: release held rows and stop claiming rather than run on a credential you cannot
   prove is valid.
7. A link list stays a list. `_val()` collapses a one-element list to its element, which is right for a
   single-select and wrong for watchers and parents: `[271]` would arrive as the integer 271. Those
   fields go through `_ids()` instead, so `row.watchers` is `list[int]` even with one watcher.
8. Clearing a list field needs an explicit empty list. `watchers=[]` sends the field; omitting it leaves
   the existing value in place. Silently leaving the old value is the same class of defect as a
   misspelled field name that the server ignores.

## Lease window and tenant isolation

Two server contracts that shape how a worker should behave, both verified against the live
gateway.

**The lease is 900 seconds and a heartbeat EXTENDS it.** `LEASE_SECONDS = 900` is a module
constant on the server - not something a client can ask to change. `POST
/v1/taskbus/rows/{row_id}/heartbeat` with the claim nonce pushes `lease_expires_at` forward, so a
long task no longer has to fit inside one 900-second window. The client exposes it as
`TaskbusClient.heartbeat(claim)`; `TaskbusWorker` calls it for you while your handler runs, every
`TASKBUS_HEARTBEAT_INTERVAL` seconds (default `LEASE_SECONDS / 3` = 300).

**Renewal is bounded: `HEARTBEAT_HORIZON_SECONDS = 7200`.** Heartbeats extend a claim only up to two
hours counted from the claim itself. Past that horizon the claim lapses and the row must be
re-claimed, which mints a new nonce (invalidating the old one) and increments `attempts_taken`. The
bound is a published constant because the route does not report a per-tenant horizon.

**A 409 from the heartbeat does NOT revive the claim.** It means the claim is no longer live - the
row left InProgress, or the lease already lapsed and the nonce is stale - and every later write with
that nonce is refused too. `TaskbusWorker` stops heartbeating, discards the handler result and does
NOT write back, because a write would only earn a second 409 and a misleading log line. Never treat
409 as a retryable blip: recovery means re-claiming.

**Reclamation is still lazy, and 45 minutes is still cumulative leased time.** No sweeper walks the
table: a row becomes claimable the instant `lease_expires_at` passes, but is only reclaimed when
some caller next posts a claim. With `max_attempts` defaulting to 3 and no heartbeats, that is 3 x
15 minutes of execution - not a wall-clock deadline an operator can watch for.

**The watchdog follows the horizon, not the lease.** With heartbeats on (the default), the
`TaskbusWorker` watchdog defaults to `HEARTBEAT_HORIZON_SECONDS - HEARTBEAT_MARGIN_SECONDS` so a run
still stops before renewal runs out. Set `TASKBUS_HEARTBEAT=0` and it returns to
`JOB_WATCHDOG_SECONDS = 840`, one minute short of the lease, which is the pre-0.1.2 behaviour.
Either way, override with `watchdog=` or `TASKBUS_JOB_WATCHDOG`.

**Review is tenant-gated, and a foreign row answers 404.** `POST /v1/taskbus/rows/{id}/review`
first applies Gate 1, tenant isolation: the caller stakeholder must appear on the row
`stakeholder_account_master`. A caller from another tenant is refused **404 `row_not_found`**,
deliberately not 403, because a 403 would confirm that the row exists and belongs to somebody
else. The gate runs before the idempotent branch, so a foreign row recorded verdict is never
echoed back. Only then does Gate 2 apply - reviewer authority, `403 forbidden_not_reviewer` for a
key that is neither a dispatcher holding `taskbus:write` nor the compliance reviewer seat. Two
gates, two codes, not interchangeable.


## Watchers

A row can carry `watchers_stakeholder_representative`: the representatives who want to be
told about it. It is a notification annotation and nothing more.

    row = client.get_task(514)
    row.watchers                       # [271] - always a list, never a bare 271
    client.update_task(claim, watchers=[271, 84])
    client.update_task(claim, watchers=[])        # clear them

Three things that surprise people, all of them deliberate:

- **The accessor is list-preserving.** `Row.watchers` returns `list[int]`, so a row
  watching exactly one representative gives `[271]`. Ask for a scalar accessor and you get
  `None`; ask this one and you get a collection, which is what it is.
- **A watcher grants nothing.** Being on the list does not let that representative claim
  the row, write to it or review it. It is not an assignment either - read `row.assigned`
  when you need to know who owns the work.
- **The list is tenant-scoped, and a foreign id is refused.** A watcher outside your
  tenant answers `403 tenant_scope_violation` rather than being quietly dropped, so an
  integration that watches across tenants fails loudly instead of appearing to work.
  Clearing is an explicit empty list: `watchers=[]` sends the field, and omitting
  `watchers` leaves whatever is already there alone.

## Execution profiles

A row dispatched under an execution profile stores the snapshot it ran with, and you read
it back per row:

    profile = client.get_execution_profile(523)
    if profile is None:
        ...                                  # this row never ran under a profile
    profile.slug                             # 'security-audit-frontier'
    profile.profile_id                       # 2
    profile.config_hash                      # the resolved config digest
    profile.pinned_model                     # 'claude-sonnet-4-6|anthropic-us', or None
    profile.exclude_providers                # ['deepseek', 'openai']
    profile.routing_strategy                 # 'pinned' / 'cheapest_frontier' / ...

The endpoint reads the snapshot **stored on the row**, so it never re-resolves the current
profile: retire the profile or edit its role afterwards and this still returns what the
run actually used. That is what makes it usable as history.

- `pinned_model` is optional. Snapshots written before the profile upgrade do not carry
  it, and an absent pin means "this run was not pinned", not "unknown". Check for `None`.
- `exclude_providers` is a list; it is `[]` when the snapshot carries no exclusions.
- A row that never ran under a profile answers `404 no_execution_profile`, which this
  client reports as `None`. Every other failure raises - including a `404 row_not_found`,
  which is also how the gateway answers for another tenant's row. `None` means "no
  profile", never "not found".

## Recursion is bounded by the row chain

`parent_task` links make a task tree, and an agent that spawns work from work can walk it forever.
Before dispatching, `TaskbusWorker` walks the chain with `TaskbusClient.spawn_depth(row_id)`; a row
at or beyond `MAX_SPAWN_DEPTH` (3) runs no handler at all. It is claimed and immediately released as
Blocked with the reason, so it leaves the queue instead of being picked up every cycle. That spends
one attempt, for the same reason the fleet poller does: the gateway authorises a status write with
the claim nonce, and an unclaimed row has none.

The chain is the authority, not a counter, so a worker cannot launder its depth by omitting a field.
The walk is cycle-safe and reports a malformed chain (a cycle, or a row with several parents) rather
than guessing.

## SUPERVISOR_RESULT

A supervised runner finishes a task in passes and ends with an outcome rather than a status. It
prints one line to stdout:

```
SUPERVISOR_RESULT {"outcome": "verified", "reason": "criteria met", "passes": 2,
                   "state_path": "/tmp/s.md", "status_hint": "HumanReviewNeeded"}
```

`parse_supervisor_result(text)` finds the LAST such line in captured output (single-line JSON, one
of many lines) and returns the envelope, or None - it never raises, because it runs on a failure
path. `supervisor_close_body(parsed, log_excerpt=...)` maps the envelope onto a PATCH body and
enforces two rules: the status is always `HumanReviewNeeded` or `Blocked`, never `Completed`, and a
Blocked close always carries a non-empty `re_opened_or_retry_reason`.

A handler can return one of these envelopes in `HandlerResult.summary`; the worker then closes with
the envelope instead of the handler's own status hint.

## Criterion grammar

A ticket's success_criterion is a semicolon-separated list, and all checks must pass:

    exit_zero                                    (banned for real deliverables)
    file_exists:/abs/path
    file_excludes:/abs/path=LITERAL_TEXT
    file_md5:/abs/path=<32 hex>
    file_sha256:/abs/path=<64 hex>
    http_status:<url>=<numeric code>
    http_200:<url>
    git_ls_remote:<repo>@<branch>=<sha>

There is no file_contains. http_status issues a GET only, so a POST-only route is observable only as 405,
and a 405 proves the route is registered rather than that it works.

## What this package deliberately does not do

- No Vault, no AppRole, no database client, no SSH, no local node identity, no model credential
  resolution. Those belong to the fleet agents and stay out of a public package.
- No implicit claim retry: a retried claim on a row you already hold answers 409 and the naive caller
  concludes the bus is broken.
- No address is ever defaulted to a private mesh. If you run against a private gateway you must say so.

## Status

0.1.4 - watchers (`Row.watchers`, `create_task(watchers=...)`, `update_task(watchers=...)`)
and `get_execution_profile()`. Wire shapes were measured against the live gateway on
2026-11-20. Client and worker are usable; the MCP server requires the optional extra.
