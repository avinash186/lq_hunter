"""Feature engineering for the macro-liquidity-sweep model (see docs/model_spec.md).

Modules:
  brackets   as-of-t immediate bracket A/B/IBM + level metadata, per 1m bar
  technical  ATR (5m/daily), VWAP, momentum, volume, session/temporal (vectorized)
"""
