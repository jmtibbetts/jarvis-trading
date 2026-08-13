# Path model — trained, evaluated, and NOT shipped

Date: 2026-08-13. Dataset: 9,246 rows, 33 features, schema `2d3a8af9fce536ff`.

## Verdict

**The path model does not demonstrate value on the data currently available,
so it is not wired into anything.** The guide is explicit about this
(§23, §69): *"If a model adds no measurable OOS value, disable or remove
it"* and *"Prefer deleting a model that adds no measurable value over
keeping it for architectural novelty."*

The infrastructure built to reach this conclusion is sound and stays. The
model does not.

## Measured, out of sample

Every target was scored against a constant baseline — predicting the
training quantile for every row. A model that cannot beat a constant has
learned nothing.

| target | winner | vs baseline |
|---|---|---|
| `mfe_r` | gbm_quantile | **+0.9%** — noise |
| `mae_r` | **baseline_constant** | nothing beat a constant |
| first touch | gbm_classifier | Brier 0.2286 vs 0.2353 — **+2.9%** |

R² is negative for every regression variant on both targets, on both GBM
and a CUDA-trained MLP. These features do not explain MFE/MAE variance.

## Why — three reasons, none of them "the model needs tuning"

**1. Every row is replay.** `live_rows: 0`. Replay assumes perfect fills
and that a bar's high *and* low were both reachable. The paths are
therefore synthetic, systematically optimistic, and partly an artefact of
the replay rules rather than of the market. A model fitted to them learns
the simulator.

**2. The test window is one day.** 11,243 of 11,785 labelled signals were
generated this month, and the daily distribution is extreme:

```
2026-08-11   4,247
2026-08-10   3,304
2026-08-12   3,009
2026-08-13     638
2026-04-30     190
```

A chronological 60/20/20 split therefore trains through 2026-08-11 and
tests on 2026-08-12 onward — roughly a single day. That is the correct
split (random splitting would leak), but it cannot support a claim about
out-of-sample generalisation. The data is not deep in TIME, only in COUNT.

**3. 15% of features are masked.** Mean missingness 0.151, largely regime
axes abstaining where a benchmark or derivatives snapshot was unavailable.
Honest, but it thins the signal.

## What would change the answer

Not tuning. Data:

- **Live path labels**, accumulated over months, from
  `path_source = LIVE_OBSERVED` rather than `REPLAY_OHLC`. Phase 5's
  recorders are the prerequisite.
- **Calendar spread**, not row count. Several months where each month has
  meaningful volume, so a chronological split spans regimes rather than
  days.
- Re-run `ml/datasets/build_path_dataset.py` and
  `ml/training/train_path_model.py`. Both are reproducible and take about
  25 minutes end to end.

## What was kept

- `ml/datasets/build_path_dataset.py` — leakage-safe feature reconstruction.
  Bars are sliced to `index <= generated_at` per signal and re-checked by
  `assert_no_lookahead()`. 0 lookahead violations across 11,785 rows.
- `ml/training/train_path_model.py` — chronological split, quantile
  (pinball) loss, constant baseline, GBM and CUDA MLP, early stopping on
  validation only.
- The path labels themselves, which are useful regardless: winners average
  MFE 1.664R / MAE 0.313R, losers 0.526R / 1.260R.

The negative result is the deliverable. Shipping a model that beats a
constant by 0.9% on one day of simulated fills would have been worse than
shipping nothing, because it would have looked like progress.
