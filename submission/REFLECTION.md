# Reflection

## A design decision

The memory hook injects retrieved memories into the user's message before the
model sees it, which creates a problem on the write path: if the turn is saved
as-is, that context is written back into memory, and every subsequent turn
inherits a larger copy of its own history. Parsing the prefix off again is
unreliable, because the injected block contains blank lines of its own.

I chose to capture the customer's message on the read path, before injection, and
prefer that value when writing the event. A fresh `MemoryHook` is constructed per
request, so the stored value cannot leak between customers. The cost is one
instance attribute; the benefit is that stored memories stay the size of what the
customer actually said.

## A challenge

Test 6 failed with the agent unable to read a page title. CloudWatch showed the
model had called the browser tool correctly — navigate, then read — so the
reasoning was right. The real error was

```
PermissionError: [Errno 13] Permission denied: '/var/task/playwright/driver/node'
```

The binary was present and the correct architecture; it simply was not
executable. Zip archives record POSIX permission bits, but the package was built
on Windows, which has no executable bit to record, so CodeZip shipped playwright's
Node driver without one.

I resolved it by marking the driver executable at first browser use, falling back
to copying it into `/tmp` and pointing playwright at the copy through
`PLAYWRIGHT_NODEJS_PATH` when the deployment directory is read-only. The lesson
was that the log identified the cause in one line, after I had already spent time
optimising import performance on a hunch.

## Extending this for production

That workaround is the first thing I would replace: building the package on
Linux, or through a container build, preserves executable bits and removes the
need for it.

Three others matter more. The Gateway bearer token is read from an environment
variable; it belongs in AgentCore Identity via `@requires_access_token`, so no
secret sits in the runtime and refresh is handled for me. The API Gateway methods
fronting the order Lambda use `authorization-type NONE`, leaving customer data
publicly readable — they need an authorizer. And the execution role grants
browser and code-interpreter actions on `Resource: "*"`, which should be scoped
to the specific tool resources.

Finally, `create_event` runs inside the response path, so every customer waits on
a memory write they do not benefit from. Moving it to a background task would cut
latency on every turn.
