# Security Policy

## Supported Versions

Security fixes are applied to the latest release and current `main` branch. Older versions may be asked to update before a report is investigated.

## Report a Vulnerability Privately

Do not open a public issue for a suspected vulnerability or include a real Scout database, generated private dashboard, prompts, responses, raw session IDs, credentials, private paths, or account values.

Use [GitHub's private vulnerability reporting](https://github.com/Christoffer91/scout-usage-tracker/security/advisories/new).

Include a concise impact description, affected version or commit, safe reproduction steps, and sanitized evidence. Synthetic data is preferred.

## System and Scope

Scout Usage Tracker is a local desktop tool for Windows and macOS. It reads Scout's SQLite usage database read-only, retains minimized usage history, and generates a local standalone dashboard.

## Security Invariants

- Scout's source database must remain read-only.
- Private usage data and identifiers must not be uploaded or exposed.
- Stored history must exclude prompts, responses, raw session IDs, token-detail JSON, and source paths.
- Dashboard values must be escaped and remain protected by a restrictive CSP.
- Install, update, and uninstall operations must stay inside validated tracker-owned user paths.
- Network access, billing sync, background jobs, and session reporting must remain explicit opt-ins.

## Reportable Findings

Reports are relevant when they demonstrate realistic unauthorized data disclosure, writes to Scout's database, path traversal or unsafe deletion, injection in generated HTML, unexpected network transmission, secret exposure, or bypass of an explicit privacy control.

## Out of Scope

Issues in Microsoft Scout, GitHub Copilot, Python, SQLite, browsers, or operating systems are outside this repository unless the tracker introduces or materially worsens the exploit path. Reports requiring a user to intentionally replace trusted local code or disable documented safeguards are normally not treated as tracker vulnerabilities.

## Disclosure

Please allow the maintainer reasonable time to investigate and coordinate a fix before public disclosure. No response-time or bounty commitment is currently offered.
