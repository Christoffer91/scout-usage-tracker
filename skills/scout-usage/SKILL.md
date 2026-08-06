---
name: scout-usage
description: Install, update, inspect, or troubleshoot the private local Scout Usage Tracker and its static dashboard. Use for Scout usage imports, credit verification, dashboard refreshes, local installation, opt-in scheduling, privacy checks, and uninstall requests.
---

# Scout Usage Tracker

1. Inspect the package README and current local configuration before acting.
2. Keep Scout's database read-only. Never upload it, copy it into a bundle, or display prompts, summaries, raw session IDs, source paths, or source row IDs.
3. Run focused local verification before changing installation state.
4. Use `./install.sh install`, `update`, `status`, `open`, or `uninstall` as requested. Require explicit approval before `--enable-auto-update`, `--install-skill`, or `--purge-data`.
5. Treat exact Scout credits, estimated gross Scout value, included plan allowances, and aggregate GitHub billing usage as separate concepts. Never call user-scope usage organization-wide or allocate a Business/Enterprise pool to one user.
6. Plan and currency values are dated estimates, not invoices. Do not guess a Free or unknown allowance. Use promotional allowances only after explicit eligibility or a configured override.
7. Normal `update`, `render`, `status`, and `open` are offline. Run `github-sync` only when the user explicitly requests that read, and use the existing `gh` login without displaying or storing its token or owner.
8. For `github-sync`, confirm the billing entity scope (`user`, `organization`, or `enterprise`), owner, year, and month. Persist only the aggregate mode-0600 snapshot; never persist raw API output, raw items, models, or owner.
9. Never install or activate background automation without explicit approval.
10. Report exact PASS/FAIL evidence and any unverified source or billing status.
