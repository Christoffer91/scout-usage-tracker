---
name: cost
description: Show exact local Scout AIU usage as rounded whole credits for the last completed answer, the completed part of the current conversation, and today. Use when the user invokes /cost or asks what the latest Scout answer, thread, or day has consumed.
---

# Scout Cost

Run the installed tracker command and return its output without adding estimates:

```sh
${HOME}/.local/bin/scout-usage cost
```

The command uses Scout's active `SESSION_ID` automatically. It reports usage before the `/cost` request, so the cost of the `/cost` response itself appears on the next invocation.

## Safety contract

- Keep Scout's SQLite database read-only.
- Do not use the network or upload any data.
- Do not display prompts, responses, raw session IDs, database paths, or raw token details.
- Preserve the `≈` marker and the whole-credit rounding disclaimer. Stored nano-AIU is exact; the displayed integer is approximate.
- Do not describe the values as GitHub billing, an invoice, currency cost, or account-wide Copilot usage.
- If the command is missing or fails, return the short error. Do not infer or fabricate values.
- Do not install software, change configuration, refresh the dashboard, or enable a background job while answering `/cost`.

## Meaning of the lines

- `Last completed answer` can include several model/tool calls belonging to one completed turn.
- `Completed thread` excludes a currently running turn.
- `Today` includes all local Scout chats in the configured IANA timezone, while excluding the current running `/cost` turn.
- `AIU data` checks stored `total_nano_aiu` against independent recalculation from `token_details_json`; it does not verify billing.
