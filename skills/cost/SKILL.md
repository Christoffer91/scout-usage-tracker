---
name: cost
description: Show private local Scout usage for the current chat or explicitly all local chats, including last answer, day, ISO week, month, model calls, tokens, exact nano-AIU accounting, and gross cost estimates. Use whenever the user invokes /cost or asks what Scout usage has consumed, in English or another language.
---

# Scout Cost

Treat the current chat as the default scope. For bare `/cost`, run only the short current-chat report:

```sh
${HOME}/.local/bin/scout-usage cost --scope chat --period thread
```

For `/cost FAQ` or an equivalent help request, return only the usage guide. This command does not read usage data:

```sh
${HOME}/.local/bin/scout-usage cost --faq
```

Map explicit requests as follows:

```sh
# Current chat (default scope)
${HOME}/.local/bin/scout-usage cost --scope chat --period last
${HOME}/.local/bin/scout-usage cost --scope chat --period thread
${HOME}/.local/bin/scout-usage cost --scope chat --period day
${HOME}/.local/bin/scout-usage cost --scope chat --period week
${HOME}/.local/bin/scout-usage cost --scope chat --period month

# All locally retained Scout chats (only when explicitly requested)
${HOME}/.local/bin/scout-usage cost --scope all --period all
${HOME}/.local/bin/scout-usage cost --scope all --period day
${HOME}/.local/bin/scout-usage cost --scope all --period week
${HOME}/.local/bin/scout-usage cost --scope all --period month
```

Interpret equivalent natural-language phrases, including follow-ups. For example, `today`, `i dag`, `hoy`, or an equivalent phrase means the current chat today unless the user also explicitly says all chats.

## Language

- English is the default when the request contains no language signal.
- For Norwegian requests, add `--language nb`.
- For English requests, add `--language en` or use the configured default.
- For another language, run the English command, translate only headings and explanatory prose into the user's language, and preserve every number, model name, unit, scope, integrity state, and caveat exactly.
- Return command output without adding inferred usage or billing claims.

The command uses Scout's active `SESSION_ID` when available. Calendar and automation surfaces may omit it; the tracker then accepts only one uniquely freshest local usage session within a strict time window. It reports usage before the current request, so the usage of the `/cost` response itself appears next time.

## Safety contract

- Keep Scout's SQLite database read-only.
- Do not use the network or upload any data.
- Do not display prompts, responses, raw session IDs, database paths, or raw token details.
- The generated dashboard hyperlink is the only permitted local path in the response. Preserve the command-generated HTML link and its short visible label `Usage tracker` exactly; never reveal, construct, or guess the target path in the skill.
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
- Day, ISO week, and month use only the current chat unless the user explicitly requests all chats.
- `All chats` means locally retained Scout chats only, never account-wide GitHub Copilot usage.
- `AIU data` checks stored `total_nano_aiu` against independent recalculation from `token_details_json` for the selected report; it does not verify billing.
- Gross USD uses GitHub's published conversion of USD 0.01 per AI credit. Model token rates are already reflected in Scout's exact credits. Actual billed usage can be lower or zero because of included credits.
