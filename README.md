# il-price-snapshot

Daily snapshot of supermarket prices that Israeli chains publish by law (Price Transparency Law). Public data only: no secrets, no personal data.

A scheduled GitHub Action runs `tools/price-ingest/price_ingest.py` (Python standard library only) and publishes `prices.json` to the `prices-data` branch, served by GitHub Pages.
