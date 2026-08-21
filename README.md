# market_data_mirror

Market-data mirror generated directly by GitHub Actions using Python + `yfinance`.

Cloudflare Worker is no longer required for the normal data path.

## Data layout

- `data/market_report.json` — latest U.S. market snapshot
- `data/tw_market_report.json` — latest Taiwan market snapshot
- `data/stocks/<TICKER>.json` — latest U.S. per-stock snapshot
- `data/status.json` — refresh metadata, backend, success/failure state

## Source

`yfinance / Yahoo Finance`, fetched directly from the GitHub-hosted Actions runner.

The Python job uses `auto_adjust=False` and keeps data-quality checks for moving averages, 52-week range, large gaps, split/corporate-action signals, and adjusted-close divergence.

For known Taiwan corporate actions, explicit overrides take precedence for reporting. In particular, 瑞儀 (6176) uses the official pre-action reference during its 2026 capital-reduction suspension instead of treating vendor-adjusted historical snapshots as actual trades.

## Schedule

The workflow runs automatically at:

- 06:30 Asia/Taipei, Monday-Friday (`22:30 UTC`, Sunday-Thursday)
- 20:30 Asia/Taipei, Monday-Friday (`12:30 UTC`, Monday-Friday)

It can also be run manually with `workflow_dispatch`.

These times refresh the mirror shortly before the 07:00 Taiwan morning report and 21:00 U.S. evening report. A push that changes the workflow or scripts also triggers a test refresh.

## Failure behavior

Symbols are fetched independently. Partial failures are recorded in `data/status.json`. Generated JSON is validated before it is committed back to `main`.
