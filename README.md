# market_data_mirror

Mirror of U.S. market data fetched from the Cloudflare Worker API for reliable downstream access by ChatGPT and other tools.

## Data layout

- `data/market_report.json` — latest validated market report snapshot
- `data/stocks/<TICKER>.json` — latest validated per-stock snapshot
- `data/status.json` — sync metadata and health status

## Source

`https://lively-tooth-f895.shlee123.workers.dev`

The GitHub Action refreshes the mirror every 30 minutes and can also be run manually. A failed fetch does not overwrite the last valid snapshot.
