# Contributing

Thanks for helping improve Scout Usage Tracker. Contributions should preserve its local-only, privacy-first design.

## Before you start

- Search existing [issues](https://github.com/Christoffer91/scout-usage-tracker/issues).
- Open an issue before a large behavior, storage, privacy, or installer change.
- Report vulnerabilities privately according to [SECURITY.md](SECURITY.md), not in a public issue.
- Never attach a real Scout database, generated private dashboard, prompts, raw session IDs, private paths, or account totals.

## Branches and pull requests

`main` is the only permanent branch. Work in a fork or a short-lived branch such as `fix/locked-database` or `feature/export-summary`, then open a pull request into `main`. A permanent `develop` branch is not used.

Keep pull requests focused. Explain the user-visible behavior, privacy impact, tests run, and any limitation. Synthetic fixtures and screenshots are required for examples.

## Local verification

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m compileall -q src scripts tests
sh -n install.sh uninstall.sh
PYTHONPATH=src python3 scripts/generate_synthetic.py --check
git diff --check
```

Do not use GitHub Actions as an iterative debugging loop. Run relevant checks locally before pushing.

## Design constraints

- Scout's database remains read-only.
- No telemetry, uploads, external dashboard assets, or implicit background jobs.
- Do not store prompts, responses, raw session IDs, raw token details, or source paths.
- Exact credits come from `total_nano_aiu / 1_000_000_000`.
- Currency values remain clearly labeled estimates.
- New opt-ins must default to disabled and be documented.

Please keep discussions constructive, respectful, and focused on improving the project.
