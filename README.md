# Yahoo Losers Webapp

A Flask app that watches Yahoo Finance's daily losers, scores rebound
candidates with **empirical odds** (probabilities earned from this
system's own recorded track record — never invented), and rehearses the
strategy with a real paper-trading account before any dollar is real.

Live at <https://yahoo-losers-webapp.onrender.com> — this README renders
in-app as the [methodology](https://yahoo-losers-webapp.onrender.com/methodology) page.

## The honesty contract

1. **No fabrication.** A failed data fetch renders as unavailable with
   its reason. Never a substitute value, never a guess.
2. **Provenance everywhere.** Every number on every page names the
   provider it came from, and every provider has fallbacks probed and
   ranked before they were trusted.
3. **Odds are earned.** Displayed probabilities come from resolved
   historical predictions in the committed snapshot record, scored by
   Brier calibration on the track-record page — where being wrong is
   shown, not hidden.
4. **Paper before live.** The system trades a paper account with
   broker-resident protective orders. Real money stays unreachable until
   the recorded track record passes a graduation gate that is code, not
   judgment — the scoreboard is public on the track-record page.

## Where the real documentation lives

Deep documentation is layered inside the repository, one home per fact:

- `openwiki/` — the generated evidence index: architecture, every
  provider chain, the caching doctrine, the paper lifecycle. Maintained
  automatically by a weekday robot that pins every claim to source
  lines and opens a gated pull request when code and docs drift.
- `docs/` — the authored judgment layer: the doctrine and the review
  process, written and owned by humans (and one standing agent).
- Every change — code or docs — passes six required CI checks and a
  review before merge. The docs cannot silently rot: a deterministic
  check fails any pull request whose documented constants disagree with
  the code.

## Known limits (read before trusting any number)

- **Survivorship bias**: the universe is stocks that appeared on the
  losers screen and still trade; delisted names drop out of the record
  (the track-record page reports recent delistings alongside results).
- **Effective sample size**: probabilities are recency-weighted, so the
  true sample behind an odds figure is the effective count (`n_eff`),
  which is smaller than the raw resolved count — both are disclosed on
  the track-record page.
- **Grading basis**: predictions grade on daily closes, with partial
  credit windows checked against intraday highs where noted — a target
  "hit" on an intraday high that faded by close is graded as exactly
  that, never upgraded.
- **No cost model**: backtests ignore commissions, slippage, borrow
  costs, and taxes; paper fills are Alpaca's simulation, not real
  market impact.

## Run it

```bash
pip install -r requirements.txt
python app.py
```

Provider keys are optional — features without keys degrade to honest
unavailability, never to fake data. Tests: `pytest tests/` (~400 tests;
they pass on a keyless machine by design).
