# Scout Usage Tracker

Scout Usage Tracker is a local-only, standard-library Python utility that imports Scout's usage ledger into a private audit history and renders one standalone HTML dashboard. It exists because Scout's event data and an account-wide additional-usage total answer different questions: this tool measures Scout events; a manually entered administrator total remains explicitly account-wide.

The source SQLite database is opened with `mode=ro`, a bounded busy timeout, and `PRAGMA query_only=ON`. The tracker never stores prompts, summaries, raw token-detail JSON, raw session IDs, source paths, or source row IDs. It has no server, telemetry, external assets, or network dependency.

## Install

Requirements are Python 3.10+ with SQLite. The installer rejects older Python versions before making changes. Scout's default source is exactly `~/.scout/copilot/session-store.db`; review `config.example.json` before the first update.

```sh
./install.sh install
${HOME}/.local/bin/scout-usage status
${HOME}/.local/bin/scout-usage update
${HOME}/.local/bin/scout-usage open
```

The repeatable default install uses `~/.local/share/scout-usage-tracker`, places a launcher in `~/.local/bin`, and copies the example to `~/.config/scout-usage-tracker/config.json` only when no config exists. `update` is also available as `refresh`.

To update an existing installation from this package:

```sh
./install.sh update
```

To opt in to a macOS user LaunchAgent or install the Codex skill, request those actions explicitly:

```sh
./install.sh install --enable-auto-update
./install.sh install --install-skill
```

The LaunchAgent is never installed or activated by default. The opt-in path writes a user-level plist, bootstraps it with `launchctl`, and verifies it with `launchctl print`; it uses no `sudo`, `pkill`, or `killall`.

## Manual use and configuration

The JSON configuration supports `~` expansion:

- `source_database`, `history_database`, and `dashboard_path` select local files.
- `timezone` is an IANA name such as `Europe/Oslo`, or `local` for the system zone.
- `privacy.include_sessions` defaults to false. When true, the dashboard shows only 12-character labels derived from full local HMAC digests.
- `usd_per_credit_by_model` contains optional user-supplied USD-per-credit estimates. Unknown models show an em dash, and no total estimate is shown unless every active model has a rate.
- `usd_to_nok` is an optional manual exchange rate.
- `account_comparison` may contain a manual account-wide Copilot `total`, `additional_usage_usd`, `as_of`, and `scope`. Both totals are always labeled account-wide/manual and never inferred as Scout-only.

Legacy camelCase keys are migrated only when their snake_case replacement is absent. Writes are atomic and preserve restrictive permissions.

## Privacy and integrity

The runtime directory is mode `0700`. Config, HMAC secret, history database, WAL/SHM files, dashboard, and logs are mode `0600`. A source event is keyed with `HMAC(secret, normalized_source_path + NUL + source_row_id)`; neither component is persisted in plaintext. A SHA-256 content-version identifier makes repeated imports no-ops while retaining corrected versions as inactive audit history. Only active versions aggregate.

The generated HTML is intentionally private usage metadata. It escapes database/config text, embeds all CSS and JavaScript, declares a restrictive CSP, and makes no network requests. Review it before sharing.

## Credit self-verification

The authoritative exact total is:

```text
credits = Decimal(total_nano_aiu) / 1,000,000,000
```

Each event is independently recalculated from its token-detail entries:

```text
recalculated_nano_aiu = sum(Decimal(tokenCount) * Decimal(costPerBatch) / Decimal(batchSize))
```

Numbers must be finite; token counts and costs must be nonnegative; batch size must be positive. A difference of at most 0.5 nano-AIU verifies. Missing or invalid JSON is reported explicitly. A negative stored total is retained as an authoritative adjustment, while token counts remain nonnegative.

## Automatic refresh

Automatic refresh is macOS-only and opt-in. Enable it only after a successful manual update:

```sh
./install.sh install --enable-auto-update
```

Inspect with `launchctl print gui/$(id -u)/local.scout-usage-tracker`. Disable while preserving data with `./install.sh uninstall`.

## Troubleshooting

`scout-usage status` distinguishes a missing, locked, permission-denied, corrupt, or schema-incompatible source. A possible gap warning is heuristic, based only on aggregate source-ID continuity; raw IDs are not retained. If the source schema changes, update the tracker rather than modifying Scout's database. `open` requires an existing dashboard and invokes the platform opener without a shell.

## Uninstall

```sh
./install.sh uninstall
# or
./uninstall.sh
```

The default removes only owned launcher/program/skill/LaunchAgent files and preserves config, history, secret, dashboard, logs, and ownership markers. Installation refuses pre-existing unowned tracker roots or skills. Destructive removal requires the explicit option:

```sh
./install.sh uninstall --purge-data
```

## Local verification

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src scripts tests
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" skills/scout-usage
sh -n install.sh uninstall.sh
PYTHONPATH=src python3 scripts/generate_synthetic.py
PYTHONPATH=src python3 scripts/generate_synthetic.py --check
```

## Limitations

Purge removes only enumerated tracker-owned files and removes a tracker directory only when empty; unrelated files are preserved. This is ledger accounting, not a billing API. Prices and exchange rates are user-entered estimates; actual billed cost may be zero because credits are included. Event-level model attribution assigns the whole event to its recorded model. Source gaps are heuristic. The dashboard does not auto-refresh while open and does not expose a local server.
