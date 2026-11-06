# bare-taskbus

A standalone client for the Bare-AI Task Bus: claim work, do it, hand it to review.

One dependency (httpx). No Vault, no database client, no SSH, no fleet configuration. It talks to one
gateway over HTTPS with one API key, which is all an external agent needs.

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
    client.update_task(claim, status="HumanWorkNeeded" if False else "HumanReviewNeeded",
                       machines_response="what I read and what I ran")

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
