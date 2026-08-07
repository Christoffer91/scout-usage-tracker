---
name: cost
description: Show private local Scout usage for the current chat, last completed answer, day, ISO week, or month, including model calls, tokens, exact nano-AIU accounting, and configured cost estimates. Use whenever the user invokes /cost or asks what Scout usage has consumed.
---

# Scout Cost

For `/cost`, run the default thread report and return its output verbatim:

```sh
${HOME}/.local/bin/scout-usage cost --period thread
```

For a follow-up, select exactly one supported period:

```sh
${HOME}/.local/bin/scout-usage cost --period last
${HOME}/.local/bin/scout-usage cost --period day
${HOME}/.local/bin/scout-usage cost --period week
${HOME}/.local/bin/scout-usage cost --period month
```

The command uses Scout's active `SESSION_ID` when available. Calendar and automation surfaces may omit it; the tracker then accepts only one uniquely freshest local usage session within a strict time window. It reports usage before the current request, so the usage of the `/cost` response itself appears next time.

## Safety contract

- Keep Scout's SQLite database read-only.
- Do not use the network or upload any data.
- Do not display prompts, responses, raw session IDs, database paths, or raw token details.
- Preserve the statement that nano-AIU accounting is exact while the whole-credit display is rounded. Never call a rounded integer itself exact.
- Do not describe the values as GitHub billing, an invoice, currency cost, or account-wide Copilot usage.
- Use only configured per-model price rates. Keep models without a rate in the separate credit remainder; never invent a rate.
- Do not substitute `m_get_context_usage`, context-window percentages, subscription pricing, or a generic monetary-cost answer for the tracker command.
- If the command is missing or fails, return the short error. Do not infer or fabricate values.
- If the command reports multiple active conversations, ask the user to wait a few seconds and invoke `/cost` again. Do not choose a session yourself.
- Do not install software, change configuration, refresh the dashboard, or enable a background job while answering `/cost`.

## Meaning of the lines

- `Last completed answer` can include several model calls belonging to one completed turn.
- `Current chat` excludes the currently running `/cost` turn.
- `Today` includes all local Scout chats in the configured IANA timezone, while excluding the current running `/cost` turn.
- Week means the local ISO week; month means the local calendar month.
- `AIU data` checks stored `total_nano_aiu` against independent recalculation from `token_details_json`; it does not verify billing.
