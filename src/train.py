"""Baseline LightGBM trainer — end-to-end and runnable.

    python -m src.train --symbol NQ --timeframe 5min

Loads bars -> resamples -> builds features + a baseline label -> trains a
time-ordered LightGBM classifier (no shuffling — the split is chronological to
avoid look-ahead) -> saves the model to models/.

This is intentionally simple. It is a scaffold to build on, not a strategy.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import lightgbm as lgb
import numpy as np
from sklearn.metrics import roc_auc_score

import config
from src import data_loader, features


def build_dataset(symbol: str, timeframe: str):
    raw = data_loader.load_bars(symbol)
    bars = data_loader.resample(raw, timeframe)
    feat = features.add_features(bars)
    feat["label"] = features.make_label(feat)
    feat = feat.dropna(subset=features.FEATURE_COLUMNS + ["label"])
    X = feat[features.FEATURE_COLUMNS]
    y = feat["label"]
    return X, y, feat.index


def train(symbol: str, timeframe: str, test_frac: float = 0.2) -> dict:
    X, y, idx = build_dataset(symbol, timeframe)

    # Chronological split — never shuffle time series.
    split = int(len(X) * (1 - test_frac))
    X_tr, X_te = X.iloc[:split], X.iloc[split:]
    y_tr, y_te = y.iloc[:split], y.iloc[split:]

    train_set = lgb.Dataset(X_tr, label=y_tr)
    valid_set = lgb.Dataset(X_te, label=y_te, reference=train_set)

    params = {
        "objective": "binary",
        "metric": "auc",
        "learning_rate": 0.03,
        "num_leaves": 31,
        "min_data_in_leaf": 200,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "verbosity": -1,
        "seed": 42,
    }

    model = lgb.train(
        params,
        train_set,
        num_boost_round=600,
        valid_sets=[valid_set],
        callbacks=[lgb.early_stopping(50), lgb.log_evaluation(50)],
    )

    auc = roc_auc_score(y_te, model.predict(X_te))
    print(f"\nHold-out AUC: {auc:.4f}  (0.5 = no edge)")

    config.MODELS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    name = f"{symbol.lower()}_{timeframe}_{stamp}"
    model_path = config.MODELS_DIR / f"{name}.lgbm"
    model.save_model(str(model_path))

    meta = {
        "symbol": symbol,
        "timeframe": timeframe,
        "features": features.FEATURE_COLUMNS,
        "rows": int(len(X)),
        "train_rows": int(len(X_tr)),
        "test_rows": int(len(X_te)),
        "holdout_auc": round(float(auc), 4),
        "span": [str(idx[0]), str(idx[-1])],
        "best_iteration": model.best_iteration,
        "created_utc": stamp,
    }
    (config.MODELS_DIR / f"{name}.json").write_text(json.dumps(meta, indent=2))
    print(f"Saved model -> {model_path}")
    return meta


def main():
    ap = argparse.ArgumentParser(description="Baseline LightGBM trainer")
    ap.add_argument("--symbol", default="NQ", choices=config.SYMBOLS)
    ap.add_argument("--timeframe", default="5min", help="pandas offset, e.g. 5min, 15min, 1h")
    ap.add_argument("--test-frac", type=float, default=0.2)
    args = ap.parse_args()
    train(args.symbol, args.timeframe, args.test_frac)


if __name__ == "__main__":
    main()
