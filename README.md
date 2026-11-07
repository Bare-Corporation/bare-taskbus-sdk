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
    for row in client.list_tasks(status="new"):
        print(row.id, row.priority, row.short_description)

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

## Lease window and tenant isolation

Two server contracts that shape how a worker should behave, both verified against the live
gateway.

**The lease is 900 seconds and cannot be renewed.** `LEASE_SECONDS = 900` is a module constant on
the server - not something a client can ask to change. There is no renew route, and a PATCH does
not extend `lease_expires_at`. The only way to get more time is to re-claim after expiry, which
mints a new nonce (invalidating the old one) and increments `attempts_taken`.

**45 minutes is cumulative leased time, not a wall-clock ceiling.** With `max_attempts` defaulting
to 3, a task can be leased for at most 3 x 15 minutes of execution. Reclamation is lazy: no
sweeper walks the table. A row becomes claimable the instant `lease_expires_at` passes, but it is
only actually reclaimed when some caller next posts a claim. So the wall-clock interval from first
claim to a terminal write can exceed 45 minutes by an arbitrary margin - an operator watching for a
45-minute deadline will not see a state change at that mark.

Because the lease cannot be extended, `TaskbusWorker` runs your handler under a watchdog
(`JOB_WATCHDOG_SECONDS = 840`, i.e. 14 minutes) and releases the row as Blocked before the lease
lapses. Override with `watchdog=` or `TASKBUS_JOB_WATCHDOG`.

**Review is tenant-gated, and a foreign row answers 404.** `POST /v1/taskbus/rows/{id}/review`
first applies Gate 1, tenant isolation: the caller stakeholder must appear on the row
`stakeholder_account_master`. A caller from another tenant is refused **404 `row_not_found`**,
deliberately not 403, because a 403 would confirm that the row exists and belongs to somebody
else. The gate runs before the idempotent branch, so a foreign row recorded verdict is never
echoed back. Only then does Gate 2 apply - reviewer authority, `403 forbidden_not_reviewer` for a
key that is neither a dispatcher holding `taskbus:write` nor the compliance reviewer seat. Two
gates, two codes, not interchangeable.


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

0.1.0 - initial scaffold. Client and worker are usable; the MCP server requires the optional extra.
