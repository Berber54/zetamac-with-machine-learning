# Zetamac Trainer

An adaptive mental-math drill in the style of [zetamac.com](https://arithmetic.zetamac.com/),
as a Python desktop app. It times every problem, learns which **specific numbers**
you are slow on under each operation, and serves those numbers (and their
operations) more often.

## Run

```
pip install customtkinter
python zetamac.py
```

The only dependency is [CustomTkinter](https://customtkinter.tomschimansky.com/)
(pulls in `darkdetect`). Python 3.10+.

## How to play

- Set a session length in seconds (default 120) or flip on **Endless**, then
  press **Start** (or just hit Enter).
- Type the answer — the app advances the instant the typed value is correct,
  no Enter needed. **Esc** stops a session early.
- **Stats** opens the dashboard: per-operation summary cards, a heatmap of
  every number colored by your average solve time (bright = slow, dark = fast,
  grey = not seen yet), and a ranked list of your slowest numbers. Hover a cell
  for details.

## Game rules

All answers are non-negative integers.

| Operation      | Numbers                              | Answer |
|----------------|--------------------------------------|--------|
| Addition       | a, b ∈ [2, 100]                      | a + b |
| Subtraction    | a, b ∈ [2, 100], shown larger − smaller | a − b ≥ 0 |
| Multiplication | a ∈ [1, 12], b ∈ [2, 100]            | a × b |
| Division       | divisor ∈ [1, 12], dividend ∈ [2, 100], divides evenly | quotient |

## How the adaptivity works

Each solve time is attributed to every *meaningful* number in the problem,
keyed by `(operation, number)`:

- addition / subtraction → both displayed operands
- multiplication → both factors
- division → the **divisor and the quotient** (the small factors that drive the
  mental effort — not the sparse dividend)

Per `(operation, number)` the app keeps an exponential moving average of solve
time, blended toward a prior when there are few samples. The next problem is
then generated non-uniformly:

1. the **operation** is sampled with weight `BASELINE + slowness^EXPONENT`,
   where its slowness is the mean of its numbers' current values, and
2. each **number slot** is sampled the same way over all numbers valid for that
   slot (ranges and the integer-answer rule always hold — division picks a
   weighted divisor and weighted quotient, then computes the dividend).

The additive baseline keeps every number and operation at a non-zero
probability, so the rotation never collapses onto a few items.

### Tunable constants (top of `zetamac.py`)

| Constant | Default | Meaning |
|----------|---------|---------|
| `DEFAULT_TIME` | 3.0 s | assumed solve time for a number with no data yet |
| `EMA_ALPHA` | 0.3 | EMA smoothing factor (higher = reacts faster, forgets faster) |
| `PRIOR_STRENGTH` | 3.0 | pseudo-samples of the prior blended into each EMA |
| `WEIGHT_BASELINE` | 0.4 | weight floor so everything keeps appearing |
| `WEIGHT_EXPONENT` | 1.5 | > 1 amplifies genuinely slow numbers/operations |
| `HEAT_FAST_S` / `HEAT_SLOW_S` | 1.0 / 6.0 s | solve times mapped to the ends of the heatmap color scale |

## Files

Created next to the script; both are human-readable:

- `zetamac_stats.json` — the per-`(operation, number)` statistics (JSON keys
  are stringified numbers) plus per-operation totals.
- `zetamac_log.csv` — one row per solved problem:
  `timestamp, operation, left, right, answer, seconds`
  (for division, `left` is the dividend and `right` the divisor).

Files written by the old per-operation-only version are detected and renamed
to `*_old.*` on first run; the app then starts with fresh statistics.
