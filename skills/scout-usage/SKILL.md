---
name: scout-usage
description: Install, update, inspect, or troubleshoot the private local Scout Usage Tracker and its static dashboard. Use for Scout usage imports, credit verification, dashboard refreshes, local installation, opt-in scheduling, privacy checks, and uninstall requests.
---

# Scout Usage Tracker

1. Inspect the package README and current local configuration before acting.
2. Keep Scout's database read-only. Never upload it, copy it into a bundle, or display prompts, summaries, raw session IDs, source paths, or source row IDs.
3. Run focused local verification before changing installation state.
4. Use `./install.sh install`, `update`, `status`, `open`, or `uninstall` as requested. Require explicit approval before `--enable-auto-update`, `--install-skill`, or `--purge-data`.
5. Treat the GitHub Admin additional-usage total as account-wide and manual, never as Scout-only.
6. Describe configured currency values as estimates. Never invent a missing rate or claim an estimate is exact billing.
7. Never install or activate background automation without explicit approval.
8. Report exact PASS/FAIL evidence and any unverified source status.
