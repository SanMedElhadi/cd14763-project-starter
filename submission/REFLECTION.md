# Reflection

## A design decision: the AgentCore Memory integration

`MemoryHook` reads from AgentCore Memory on `MessageAddedEvent` and writes on
`AfterInvocationEvent`. Injecting retrieved memories into the user's message
creates a problem on the write path: if the turn is saved as sent to the model,
the injected context is stored back into memory, and every later turn inherits a
larger copy of its own history. Stripping the prefix afterwards is unreliable,
because the injected block contains blank lines of its own.

I chose to capture the customer's message on the read path, before injection, and
prefer that value when calling `create_event`. A fresh `MemoryHook` is built per
request, so the captured value cannot leak between customers. The cost is one
instance attribute; the benefit is that stored memories stay the size of what the
customer actually said.

## A challenge, and how I resolved it

Test 6 returned "I could not retrieve the page title."

**What I inspected first.** I tailed the runtime's CloudWatch log group. It showed
`Tool #1: browser` and `Tool #2: browser`, so the model had correctly chosen the
tool and sequenced navigate-then-read — the failure was below the agent layer. The
traceback ended in
`PermissionError: [Errno 13] Permission denied: '/var/task/playwright/driver/node'`.
I confirmed the file existed and was the right architecture, which narrowed it to
file permissions rather than a missing or mismatched binary.

**What I changed.** Zip archives record POSIX permission bits, but the package was
built on Windows, which has no executable bit to record, so CodeZip shipped the
driver without one. I added `_ensure_playwright_driver()`, called before the
browser tool is constructed: it chmods the driver in place, and if the deployment
directory is read-only, copies it to `/tmp` and points playwright at the copy via
`PLAYWRIGHT_NODEJS_PATH`, which `compute_driver_executable()` honours. A module
flag makes it run once per container.

**How I verified it.** I redeployed and re-ran the same invocation. The agent
returned the real title, "Learn the Latest Tech Skills; Advance Your Career |
Udacity", and the log showed the driver being marked executable with no further
`PermissionError`. I also re-ran Tests 1–5 to confirm the change had not affected
the other tools.

## A production consideration: security

The API Gateway methods fronting the order Lambda are created with
`authorization-type NONE`, so `/customers/{customer_id}` is readable by anyone
with the URL. With real customer records that is a data breach. In production it
needs a Cognito or Lambda authorizer, and the Gateway bearer token — currently an
environment variable — belongs in AgentCore Identity via `@requires_access_token`
so no secret sits in the runtime.