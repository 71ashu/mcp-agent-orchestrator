# Example goals

These run against the two servers enabled by default (`filesystem`, `deployment`).
Enable `github` / `postgres` in `config/servers.yaml` for the cross-system variants.

## Single-system (sanity checks)
- What services does the deployment control plane know about, and which are unhealthy?
- Read every file in the sandbox and give me a one-line summary of each.

## Cross-system (the point of the project)
- Find the latest production deployment for the payments service, inspect its logs,
  and tell me whether that deployment introduced a problem.
- The payments service is degraded. Figure out which deployment caused it, summarize
  the failing log lines, and recommend whether to roll back.
- Compare the error rate before and after the two most recent payments deployments
  and write the finding to sandbox/payments-incident.md.

## Write / human-in-the-loop
- Roll back the payments deployment that caused the current incident, citing the
  evidence from the logs.

## Batch eval file (one goal per line) — see examples/eval_tasks.txt
