"""Train the path model — and be willing to conclude it is not worth having.

The guide requires a non-neural baseline before an MLP, and it is not a
formality. These are 33 tabular features with a few thousand rows; gradient
boosting is frequently better than a neural net on exactly that shape, and
if it wins we ship it and never touch the NPU for this model. The point is
better trading decisions, not neural ones.

Three disciplines, each of which is a way results get faked:

  CHRONOLOGICAL SPLIT. Random splitting a time series puts trades from the
  same hour on the same symbol on both sides. The model then "predicts"
  states it has effectively already seen, and validation is meaningless.

  QUANTILE TARGETS. MFE and MAE are heavily skewed. A mean is not the
  useful summary — "this setup usually reaches 1.1R and reaches 1.9R a
  quarter of the time" is actionable; "expected MFE 1.35R" is not.

  A NAIVE BASELINE TO BEAT. Predicting the training median for every row is
  the bar. A model that cannot beat a constant has learned nothing, however
  impressive its loss curve.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

QUANTILES = (0.25, 0.50, 0.75)
TRAIN_FRAC, VAL_FRAC = 0.60, 0.20        # remainder is the untouched test set


def load(path: Path | None = None) -> dict:
    path = path or (ROOT / "ml" / "datasets" / "path_dataset.npz")
    d = np.load(path, allow_pickle=True)
    return {k: d[k] for k in d.files}


def chronological_split(n: int) -> tuple[slice, slice, slice]:
    """By position, on a dataset the builder already sorted by time."""
    a, b = int(n * TRAIN_FRAC), int(n * (TRAIN_FRAC + VAL_FRAC))
    return slice(0, a), slice(a, b), slice(b, n)


def pinball(y: np.ndarray, pred: np.ndarray, q: float) -> float:
    """Quantile (pinball) loss — the correct scorer for a quantile forecast.
    Squared error would reward predicting the mean of a skewed target."""
    d = y - pred
    return float(np.mean(np.maximum(q * d, (q - 1) * d)))


def evaluate(name: str, y: np.ndarray, preds: dict) -> dict:
    out = {"model": name}
    for q, p in preds.items():
        out[f"pinball_q{int(q * 100)}"] = round(pinball(y, p, q), 5)
    out["mean_pinball"] = round(
        float(np.mean([pinball(y, p, q) for q, p in preds.items()])), 5)
    med = preds.get(0.50)
    if med is not None:
        out["mae"] = round(float(np.mean(np.abs(y - med))), 5)
        # Fraction of the target's variance explained. Negative means the
        # model is worse than predicting the mean.
        ss_res = float(np.sum((y - med) ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        out["r2"] = round(1 - ss_res / ss_tot, 5) if ss_tot > 0 else None
    return out


def train_target(X, y, tr, va, te, label: str, seed: int = 0) -> dict:
    """Baseline vs GBM vs MLP on one target. Returns every result, so a
    disappointing model is visible rather than quietly dropped."""
    results = []

    # ── 1. constant. The bar every model must clear. ────────────────────
    const = {q: np.full(te.stop - te.start, float(np.quantile(y[tr], q)))
             for q in QUANTILES}
    results.append(evaluate("baseline_constant", y[te], const))

    # ── 2. gradient boosting, one model per quantile ────────────────────
    try:
        from sklearn.ensemble import GradientBoostingRegressor
        gbm = {}
        for q in QUANTILES:
            m = GradientBoostingRegressor(loss="quantile", alpha=q,
                                          n_estimators=200, max_depth=3,
                                          learning_rate=0.05,
                                          random_state=seed)
            m.fit(X[tr], y[tr])
            gbm[q] = m.predict(X[te])
        results.append(evaluate("gbm_quantile", y[te], gbm))
    except Exception as e:
        results.append({"model": "gbm_quantile", "error": str(e)[:120]})

    # ── 3. MLP with a genuine multi-quantile head ───────────────────────
    try:
        import torch
        import torch.nn as nn
        torch.manual_seed(seed)
        dev = "cuda" if torch.cuda.is_available() else "cpu"

        Xtr = torch.tensor(X[tr], dtype=torch.float32, device=dev)
        Ytr = torch.tensor(y[tr], dtype=torch.float32, device=dev).unsqueeze(1)
        Xva = torch.tensor(X[va], dtype=torch.float32, device=dev)
        Yva = torch.tensor(y[va], dtype=torch.float32, device=dev).unsqueeze(1)
        Xte = torch.tensor(X[te], dtype=torch.float32, device=dev)

        net = nn.Sequential(
            nn.Linear(X.shape[1], 64), nn.ReLU(), nn.Dropout(0.1),
            nn.Linear(64, 32), nn.ReLU(),
            nn.Linear(32, len(QUANTILES)),
        ).to(dev)
        opt = torch.optim.Adam(net.parameters(), lr=1e-3, weight_decay=1e-4)
        qs = torch.tensor(QUANTILES, dtype=torch.float32, device=dev)

        def qloss(pred, target):
            d = target - pred
            return torch.mean(torch.maximum(qs * d, (qs - 1) * d))

        best, best_state, patience = float("inf"), None, 0
        for epoch in range(400):
            net.train(); opt.zero_grad()
            qloss(net(Xtr), Ytr).backward(); opt.step()
            net.eval()
            with torch.no_grad():
                v = float(qloss(net(Xva), Yva))
            # Early stopping on the VALIDATION set, never the test set —
            # peeking at test to choose an epoch is how a held-out set
            # quietly stops being held out.
            if v < best - 1e-5:
                best, patience = v, 0
                best_state = {k: t.clone() for k, t in net.state_dict().items()}
            else:
                patience += 1
                if patience > 40:
                    break
        if best_state:
            net.load_state_dict(best_state)
        net.eval()
        with torch.no_grad():
            p = net(Xte).cpu().numpy()
        results.append(evaluate("mlp_quantile", y[te],
                                {q: p[:, i] for i, q in enumerate(QUANTILES)}))
        results[-1]["device"] = dev
        results[-1]["val_loss"] = round(best, 5)
        globals()[f"_net_{label}"] = net
    except Exception as e:
        results.append({"model": "mlp_quantile", "error": str(e)[:200]})

    ranked = sorted([r for r in results if "mean_pinball" in r],
                    key=lambda r: r["mean_pinball"])
    winner = ranked[0] if ranked else None
    baseline = next((r for r in results if r["model"] == "baseline_constant"), None)
    verdict = "no model beat the constant baseline"
    if winner and baseline and winner["model"] != "baseline_constant":
        gain = (baseline["mean_pinball"] - winner["mean_pinball"]) / baseline["mean_pinball"]
        verdict = f"{winner['model']} beats baseline by {gain:.1%}"
    return {"target": label, "results": results,
            "winner": winner["model"] if winner else None, "verdict": verdict}


def main(dataset: Path | None = None) -> dict:
    d = load(dataset)
    X, y_mfe, y_mae = d["X"], d["y_mfe"], d["y_mae"]
    meta = d["meta"]
    n = len(X)
    tr, va, te = chronological_split(n)

    report = {
        "rows": n, "features": int(X.shape[1]),
        "schema_hash": str(d["schema_hash"]),
        "split": {"train": tr.stop - tr.start, "val": va.stop - va.start,
                  "test": te.stop - te.start},
        "span": {"train_end": str(meta[tr.stop - 1][0])[:10],
                 "test_start": str(meta[te.start][0])[:10]},
        "targets": [],
    }
    for y, label in ((y_mfe, "mfe_r"), (y_mae, "mae_r")):
        report["targets"].append(train_target(X, y, tr, va, te, label))

    # First touch, where it resolved unambiguously.
    y_touch = d["y_touch"]
    resolved = y_touch >= 0
    if resolved.sum() > 200:
        try:
            from sklearn.ensemble import GradientBoostingClassifier
            from sklearn.metrics import brier_score_loss, log_loss
            idx = np.where(resolved)[0]
            cut_a, cut_b = int(len(idx) * TRAIN_FRAC), int(len(idx) * (TRAIN_FRAC + VAL_FRAC))
            itr, ite = idx[:cut_a], idx[cut_b:]
            clf = GradientBoostingClassifier(n_estimators=150, max_depth=3,
                                             learning_rate=0.05, random_state=0)
            clf.fit(X[itr], y_touch[itr])
            p = clf.predict_proba(X[ite])[:, 1]
            base_rate = float(y_touch[itr].mean())
            report["first_touch"] = {
                "n_train": len(itr), "n_test": len(ite),
                "base_rate_target_first": round(base_rate, 4),
                "brier": round(float(brier_score_loss(y_touch[ite], p)), 5),
                "brier_baseline": round(float(brier_score_loss(
                    y_touch[ite], np.full(len(ite), base_rate))), 5),
                "log_loss": round(float(log_loss(y_touch[ite], p, labels=[0, 1])), 5),
            }
            b = report["first_touch"]
            b["beats_baseline"] = b["brier"] < b["brier_baseline"]
        except Exception as e:
            report["first_touch"] = {"error": str(e)[:160]}
    else:
        report["first_touch"] = {"skipped": f"only {int(resolved.sum())} resolved rows"}

    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=None)
    args = ap.parse_args()
    print(json.dumps(main(args.dataset), indent=1, default=str))
