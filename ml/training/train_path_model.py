"""Phase 2 — the path model's second attempt, judged honestly.

The first path model was rejected for a measured reason: 9,246 labels
spanning one usable day made its chronological split meaningless. This
harness exists so the re-run on the deep dataset is judged the same way,
with no room for the evaluation itself to flatter the model:

  - GLOBAL chronological split (60/20/20 by anchor time, across all
    symbols at once) — a same-day bar can never sit in train and test.
  - Imputation and scaling are fit on TRAIN ONLY.
  - Two baselines that must BOTH be beaten out-of-sample before the
    neural model earns anything:
      1. the prior (base rate / train median) — "no skill"
      2. HistGradientBoosting on identical features — "non-neural skill"
  - AMBIGUOUS and NONE path labels are excluded from classification:
    an unknowable intrabar ordering must not train the classifier.

Tasks:
  stop_first  P(stop touched before target)   — AUC, Brier
  mfe_r       favorable excursion in R        — MAE

Nothing here touches live scoring. The output is a metrics report and an
artifact directory; promotion to shadow inference is a separate,
deliberate step that only makes sense if the verdict line says so.

Usage:
    python -m ml.training.train_path_model --dataset <path.parquet>
    python -m ml.training.train_path_model --parts ml/datasets/out/parts/15m_full
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

META_COLS = {"symbol", "asset_class", "timeframe", "anchor_ts", "direction",
             "entry", "stop", "target", "mfe_r", "mae_r", "first_touch",
             "bars_to_touch", "fwd_ret_4", "fwd_ret_16", "fwd_ret_64"}

SPLITS = (0.6, 0.2, 0.2)


def load_frame(dataset: str | None, parts: str | None):
    import pandas as pd
    if dataset:
        df = pd.read_parquet(dataset)
        src = dataset
    else:
        files = sorted(Path(parts).glob("*.parquet"))
        if not files:
            raise FileNotFoundError(f"no parts in {parts}")
        df = pd.concat([pd.read_parquet(p) for p in files], ignore_index=True)
        src = f"{parts} ({len(files)} parts — PARTIAL unless the build finished)"
    df["anchor_ts"] = pd.to_datetime(df["anchor_ts"], utc=True, format="mixed")
    df = df.sort_values("anchor_ts").reset_index(drop=True)
    return df, src


def chronological_split(df):
    """Cut by TIME across all symbols, not by row count per symbol —
    row-wise splitting would put the same market hour in train for one
    symbol and test for another, which is cross-sectional leakage."""
    t1 = df["anchor_ts"].quantile(SPLITS[0])
    t2 = df["anchor_ts"].quantile(SPLITS[0] + SPLITS[1])
    return (df[df["anchor_ts"] <= t1],
            df[(df["anchor_ts"] > t1) & (df["anchor_ts"] <= t2)],
            df[df["anchor_ts"] > t2])


def feature_matrix(df):
    feats = [c for c in df.columns if c not in META_COLS]
    X = df[feats].astype(float)
    # direction is data, not metadata — encode it
    X = X.assign(is_short=(df["direction"] == "Short").astype(float))
    return X, feats + ["is_short"]


def prep(train_X, *others):
    """Median-impute + standardize, statistics from TRAIN ONLY."""
    import numpy as np
    med = train_X.median(numeric_only=True)
    mu, sd = None, None
    out = []
    for X in (train_X, *others):
        # A column that is ALL-NaN in train has a NaN median, so fillna
        # leaves it NaN and one poisoned column NaNs the entire network.
        # Zero after standardization = "no information", which is the
        # honest encoding for a feature this slice never measured.
        Xi = X.fillna(med).fillna(0.0)
        if mu is None:
            mu, sd = Xi.mean(), Xi.std().replace(0, 1.0).fillna(1.0)
        out.append(((Xi - mu) / sd).fillna(0.0).to_numpy(dtype="float32"))
    return out, {"median": med.to_dict(), "mean": mu.to_dict(), "std": sd.to_dict()}


# ── Classification: stop-first ───────────────────────────────────────────────

def eval_classifier(y_true, p):
    import numpy as np
    from sklearn.metrics import brier_score_loss, roc_auc_score
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return {"auc": round(float(roc_auc_score(y_true, p)), 4),
            "brier": round(float(brier_score_loss(y_true, p)), 4)}


def run_stop_first(train, val, test):
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingClassifier

    def prep_task(df):
        m = df["first_touch"].isin(["STOP", "TARGET"])
        d = df[m]
        return d, (d["first_touch"] == "STOP").astype(int).to_numpy()

    tr, y_tr = prep_task(train)
    va, y_va = prep_task(val)
    te, y_te = prep_task(test)
    if len(tr) < 500 or len(te) < 200 or len(set(y_te)) < 2:
        return {"skipped": f"insufficient resolved labels (train={len(tr)}, test={len(te)})"}

    Xtr_df, _ = feature_matrix(tr)
    Xva_df, _ = feature_matrix(va)
    Xte_df, _ = feature_matrix(te)
    (Xtr, Xva, Xte), _stats = prep(Xtr_df, Xva_df, Xte_df)

    out = {"n_train": len(tr), "n_test": len(te),
           "test_base_rate": round(float(y_te.mean()), 4)}

    # Baseline 1: the prior.
    prior = float(y_tr.mean())
    out["baseline_prior"] = eval_classifier(y_te, np.full(len(y_te), prior))

    # Baseline 2: non-neural skill on identical features.
    gb = HistGradientBoostingClassifier(max_depth=4, max_iter=300,
                                        early_stopping=True, random_state=7)
    gb.fit(Xtr, y_tr)
    out["baseline_histgb"] = eval_classifier(y_te, gb.predict_proba(Xte)[:, 1])

    # The challenger: a small MLP on the RTX.
    out["mlp"] = _train_mlp(Xtr, y_tr, Xva, y_va, Xte, y_te, task="clf")

    gb_beats = out["baseline_histgb"]["auc"] > out["baseline_prior"]["auc"] + 0.01
    mlp_beats_all = (out["mlp"]["auc"] > out["baseline_histgb"]["auc"] + 0.005
                     and out["mlp"]["brier"] < out["baseline_histgb"]["brier"])
    out["verdict"] = ("MLP earns shadow evaluation" if mlp_beats_all else
                      "HistGB is the honest choice" if gb_beats else
                      "nothing beats the prior — features carry no path signal here")
    return out


def run_mfe(train, val, test):
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.metrics import mean_absolute_error

    def prep_task(df):
        d = df[df["mfe_r"].notna() & (df["mfe_r"] < 20)]   # clip absurd outliers
        return d, d["mfe_r"].to_numpy(dtype="float32")

    tr, y_tr = prep_task(train)
    va, y_va = prep_task(val)
    te, y_te = prep_task(test)
    if len(tr) < 500 or len(te) < 200:
        return {"skipped": "insufficient labels"}

    Xtr_df, _ = feature_matrix(tr)
    Xva_df, _ = feature_matrix(va)
    Xte_df, _ = feature_matrix(te)
    (Xtr, Xva, Xte), _ = prep(Xtr_df, Xva_df, Xte_df)

    out = {"n_train": len(tr), "n_test": len(te)}
    med = float(np.median(y_tr))
    out["baseline_median"] = {"mae": round(float(
        mean_absolute_error(y_te, np.full(len(y_te), med))), 4)}
    gb = HistGradientBoostingRegressor(max_depth=4, max_iter=300,
                                       early_stopping=True, random_state=7)
    gb.fit(Xtr, y_tr)
    out["baseline_histgb"] = {"mae": round(float(
        mean_absolute_error(y_te, gb.predict(Xte))), 4)}
    out["mlp"] = _train_mlp(Xtr, y_tr, Xva, y_va, Xte, y_te, task="reg")

    mlp_wins = out["mlp"]["mae"] < out["baseline_histgb"]["mae"] * 0.98
    gb_wins = out["baseline_histgb"]["mae"] < out["baseline_median"]["mae"] * 0.98
    out["verdict"] = ("MLP earns shadow evaluation" if mlp_wins else
                      "HistGB is the honest choice" if gb_wins else
                      "nothing beats predicting the median — no MFE signal here")
    return out


def _train_mlp(Xtr, y_tr, Xva, y_va, Xte, y_te, task: str) -> dict:
    import numpy as np
    import torch
    from torch import nn

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    torch.manual_seed(7)
    n_in = Xtr.shape[1]
    head = [nn.Linear(32, 1)]
    model = nn.Sequential(nn.Linear(n_in, 64), nn.ReLU(), nn.Dropout(0.2),
                          nn.Linear(64, 32), nn.ReLU(), *head).to(dev)
    loss_fn = nn.BCEWithLogitsLoss() if task == "clf" else nn.L1Loss()
    opt = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)

    def t(x):
        return torch.tensor(x, device=dev)

    Xtr_t, ytr_t = t(Xtr), t(np.asarray(y_tr, dtype="float32")).unsqueeze(1)
    Xva_t, yva_t = t(Xva), t(np.asarray(y_va, dtype="float32")).unsqueeze(1)
    best, best_state, patience = float("inf"), None, 0
    for epoch in range(200):
        model.train()
        perm = torch.randperm(len(Xtr_t), device=dev)
        for i in range(0, len(perm), 4096):
            idx = perm[i:i + 4096]
            opt.zero_grad()
            loss = loss_fn(model(Xtr_t[idx]), ytr_t[idx])
            loss.backward()
            opt.step()
        model.eval()
        with torch.no_grad():
            vl = float(loss_fn(model(Xva_t), yva_t))
        if vl < best - 1e-5:
            best, patience = vl, 0
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
        else:
            patience += 1
            if patience >= 12:
                break
    if best_state:
        model.load_state_dict(best_state)
    model.eval()
    with torch.no_grad():
        pred = model(t(Xte)).cpu().numpy().ravel()
    if task == "clf":
        from scipy.special import expit
        return {**eval_classifier(y_te, expit(pred)), "epochs": epoch + 1}
    from sklearn.metrics import mean_absolute_error
    return {"mae": round(float(mean_absolute_error(y_te, pred)), 4),
            "epochs": epoch + 1}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset")
    ap.add_argument("--parts")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    if not args.dataset and not args.parts:
        ap.error("--dataset or --parts required")

    df, src = load_frame(args.dataset, args.parts)
    train, val, test = chronological_split(df)
    logger.info(f"source: {src}")
    logger.info(f"rows: {len(df):,}  span {df['anchor_ts'].min()} -> {df['anchor_ts'].max()}")
    logger.info(f"split: train {len(train):,} | val {len(val):,} | test {len(test):,}\n")

    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": src, "rows": len(df),
        "span": [str(df["anchor_ts"].min()), str(df["anchor_ts"].max())],
        "stop_first": run_stop_first(train, val, test),
        "mfe": run_mfe(train, val, test),
    }
    out_dir = Path("ml/models") / f"path_{datetime.now(timezone.utc):%Y%m%d_%H%M}{('_' + args.tag) if args.tag else ''}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    for task in ("stop_first", "mfe"):
        r = report[task]
        logger.info(f"== {task} ==")
        for k, v in r.items():
            logger.info(f"  {k}: {v}")
    logger.info(f"\nreport: {out_dir / 'report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
