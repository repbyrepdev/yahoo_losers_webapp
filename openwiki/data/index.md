# Files

- [Market Data Cache and Background Warmers](market-data-cache-and-warmers.md) - Explains the market_data.py cache, TTL policy, yfinance producers, throttles, failover hooks, and two-lane background warming lifecycle.
- [Data Provenance and No-Fabrication Contract](provenance-and-honesty.md) - Explains how this app represents live, derived, and unavailable market data so missing inputs never become fabricated financial numbers.
- [Provider Failover Layer](provider-failover.md) - Source-grounded guide to Yahoo primary data, Alpaca/Finnhub/FMP/FINRA/SEC/FRED fallbacks, credentials, budgets, and provider-specific invariants.
