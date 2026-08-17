# ml/ — offline training and evaluation

Nothing here is imported by the trading system. `requirements-research.txt`
is a separate install, and `tests/test_ml_env_isolation.py` asserts by AST
that no module on the trading path imports torch, scikit-learn or openvino
at top level. JARVIS must start, trade AND LEARN with none of this present:
calibration, expectancy, strategy lifecycle, venue and wallet learning and
attribution are stdlib and SQLAlchemy, and the predictive runtime is numpy
and OpenVINO on CPU.

**This directory is the only thing in the repository that imports torch**,
and the supported install is the CPU wheel. The device line reads
`"cuda" if torch.cuda.is_available() else "cpu"`, the model is
33 -> 64 -> 32 -> 1 over ~5,500 rows, and a GPU changes the wall clock and
nothing else. CUDA is an optional extra (`requirements-cuda.txt`), never a
prerequisite — the RTX 5090 reaches JARVIS through LM Studio's HTTP API,
which is a network hop, not a Python dependency.

```
datasets/   leakage-safe dataset builders
training/   train + evaluate, baseline first
evaluation/ results, including negative ones
```

## Rules that are not negotiable

**Chronological splits only.** Random splitting a time series puts trades
from the same hour on the same symbol on both sides of the split. The model
then scores well on states it has effectively already seen.

**A non-neural baseline first.** These are ~33 tabular features. Gradient
boosting frequently beats a neural net on that shape, and a constant is the
bar both must clear. If the constant wins, the constant ships.

**Replay is not live.** `path_source` distinguishes `REPLAY_OHLC` from
`LIVE_OBSERVED`. Replay assumes perfect fills and that a bar's high and low
were both reachable, so it is systematically optimistic. Never pool them.

**A model that adds no measurable out-of-sample value does not ship.** See
`evaluation/PATH_MODEL_FINDINGS.md` for a worked example — the path model
was built, trained on CUDA, evaluated, and rejected.

## Reproduce

```bash
pip install -r requirements-research.txt      # CPU only, plain PyPI
python ml/datasets/build_path_dataset.py      # ~20 min
python ml/training/train_path_model.py        # ~5 min
```

Only if an experiment is genuinely too large for CPU — note
`--extra-index-url`, not `--index-url`; the CUDA index is partial and does
not carry numpy, scipy or scikit-learn:

```bash
pip install -r requirements-cuda.txt --extra-index-url https://download.pytorch.org/whl/cu128
```
