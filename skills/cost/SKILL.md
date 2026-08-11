---
name: cost
description: Show private local Scout usage for the current chat or explicitly all local chats, including last answer, day, ISO week, month, model calls, tokens, exact nano-AIU accounting, and gross cost estimates. Use whenever the user invokes /cost or asks what Scout usage has consumed, in English or another language.
---

# Scout Cost

Select the launcher for the host before every mapping below:

- Windows PowerShell: `& "$env:USERPROFILE\.local\bin\scout-usage.cmd"`
- macOS/POSIX: `${HOME}/.local/bin/scout-usage`

Never show the selected launcher path in the response. Treat the current chat as the default scope. For bare `/cost`, run only the short current-chat report:

```text
# Windows PowerShell
& "$env:USERPROFILE\.local\bin\scout-usage.cmd" cost --scope chat --period thread --language en --dashboard-link loopback
# macOS/POSIX
${HOME}/.local/bin/scout-usage cost --scope chat --period thread --language en
```

For `/cost FAQ` or an equivalent help request, return only the usage guide. This command does not read usage data:

```text
# Windows PowerShell
& "$env:USERPROFILE\.local\bin\scout-usage.cmd" cost --faq --language en
# macOS/POSIX
${HOME}/.local/bin/scout-usage cost --faq --language en
```

For `/cost open`, `open usage tracker`, or an equivalent request, open the generated dashboard through the
installed native launcher. Do not follow or reconstruct the dashboard's `file://` link: Scout can reject local
links outside the active workspace even when the file is valid.

```text
# Windows PowerShell
& "$env:USERPROFILE\.local\bin\scout-usage.cmd" open
# macOS/POSIX
${HOME}/.local/bin/scout-usage open
```

Return only a short success or failure message. Never display the launcher path or dashboard path.

Map explicit requests as follows:

```text
# Windows PowerShell: current chat (default scope)
& "$env:USERPROFILE\.local\bin\scout-usage.cmd" cost --scope chat --period last --dashboard-link loopback
& "$env:USERPROFILE\.local\bin\scout-usage.cmd" cost --scope chat --period thread --dashboard-link loopback
& "$env:USERPROFILE\.local\bin\scout-usage.cmd" cost --scope chat --period day --dashboard-link loopback
& "$env:USERPROFILE\.local\bin\scout-usage.cmd" cost --scope chat --period week --dashboard-link loopback
& "$env:USERPROFILE\.local\bin\scout-usage.cmd" cost --scope chat --period month --dashboard-link loopback

# Windows PowerShell: all locally retained Scout chats (only when explicitly requested)
& "$env:USERPROFILE\.local\bin\scout-usage.cmd" cost --scope all --period all --dashboard-link loopback
& "$env:USERPROFILE\.local\bin\scout-usage.cmd" cost --scope all --period day --dashboard-link loopback
& "$env:USERPROFILE\.local\bin\scout-usage.cmd" cost --scope all --period week --dashboard-link loopback
& "$env:USERPROFILE\.local\bin\scout-usage.cmd" cost --scope all --period month --dashboard-link loopback

# macOS/POSIX: current chat (default scope)
# Current chat (default scope)
${HOME}/.local/bin/scout-usage cost --scope chat --period last
${HOME}/.local/bin/scout-usage cost --scope chat --period thread
${HOME}/.local/bin/scout-usage cost --scope chat --period day
${HOME}/.local/bin/scout-usage cost --scope chat --period week
${HOME}/.local/bin/scout-usage cost --scope chat --period month

# macOS/POSIX: all locally retained Scout chats (only when explicitly requested)
${HOME}/.local/bin/scout-usage cost --scope all --period all
${HOME}/.local/bin/scout-usage cost --scope all --period day
${HOME}/.local/bin/scout-usage cost --scope all --period week
${HOME}/.local/bin/scout-usage cost --scope all --period month
```

Interpret equivalent natural-language phrases, including follow-ups. For example, `today`, `i dag`, `hoy`, or an equivalent phrase means the current chat today unless the user also explicitly says all chats.

## Language

- Bare `/cost` and bare `/cost FAQ` always use `--language en`, regardless of the configured default.
- When the user explicitly requests Norwegian, add `--language nb`; when the user explicitly requests English, add `--language en`.
- Return command stdout verbatim. Do not translate, reformat, summarize, shorten, or add inferred usage or billing claims.
- Preserve the command output's final `/cost FAQ` invitation verbatim, including its ASCII quotation marks. It is part of every normal report and must never be reformatted, summarized, shortened, or omitted.

The command uses Scout's active `SESSION_ID` when available. Calendar and automation surfaces may omit it; the tracker then accepts only one uniquely freshest local usage session within a strict time window. It reports usage before the current request, so the usage of the `/cost` response itself appears next time.

## Safety contract

- Keep Scout's SQLite database read-only.
- Do not use external network interfaces or upload any data. On Windows, the command-generated dashboard link may use its bounded, token-protected `127.0.0.1` viewer; macOS/POSIX keeps the direct local file link.
- Do not display prompts, responses, raw session IDs, database paths, or raw token details.
- Preserve the command-generated HTML link and its short visible label `Usage tracker` exactly; never reveal, construct, guess, or display the full launcher path, dashboard path, or link target in the skill response. Its random loopback URL expires after one successful dashboard fetch or five minutes.
- `/cost open` remains a native-launcher fallback if the short-lived hyperlink has expired or cannot start.
- Preserve the statement that nano-AIU accounting is exact while the whole-credit display is rounded. Never call a rounded integer itself exact.
- Do not describe the values as GitHub billing, an invoice, currency cost, or account-wide Copilot usage.
- Use only configured per-model price rates. Keep models without a rate in the separate credit remainder; never invent a rate.
- Do not substitute `m_get_context_usage`, context-window percentages, subscription pricing, or a generic monetary-cost answer for the tracker command.
- If the command is missing or fails, return the short error. Do not infer or fabricate values.
- If the command reports multiple active conversations, ask the user to wait a few seconds and invoke `/cost` again. Do not choose a session yourself.
- Do not install software, change configuration, refresh the dashboard, or enable a persistent background job while answering `/cost`. The approved on-demand loopback viewer is bounded to one fetch or five minutes.

## Meaning of the lines

- `Last completed answer` can include several model calls belonging to one completed turn.
- `Current chat` excludes the currently running `/cost` turn.
- Day, ISO week, and month use only the current chat unless the user explicitly requests all chats.
- `All chats` means locally retained Scout chats only, never account-wide GitHub Copilot usage.
- `AIU data` checks stored `total_nano_aiu` against independent recalculation from `token_details_json` for the selected report; it does not verify billing.
- Gross USD uses GitHub's published conversion of USD 0.01 per AI credit. Model token rates are already reflected in Scout's exact credits. Actual billed usage can be lower or zero because of included credits.
