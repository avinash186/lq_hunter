"""Runtime settings store tests — seed-on-create (DRY), persistence, stored-wins on
reload. Uses a temp file so the real data/runtime_settings.json is never touched.

    PYTHONPATH=. python tests/test_settings.py    # or pytest
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from src import settings

_TMP = Path(tempfile.gettempdir()) / "lgbm_test_runtime_settings.json"


def _fresh():
    if _TMP.exists():
        _TMP.unlink()
    settings.PATH = _TMP
    settings._data = None


def test_seed_is_dry_on_first_creation():
    _fresh()
    assert settings.execute_broker() is False           # DRY by default on creation
    assert _TMP.exists()                                 # seeded + persisted


def test_set_persists_and_survives_reload():
    _fresh()
    settings.set("execute_broker", True)
    settings.set("max_risk_per_trade", 130)
    settings.reload()                                    # drop cache -> reload from file
    assert settings.execute_broker() is True             # LIVE survives a restart
    assert settings.max_risk() == 130


def test_stored_value_wins_over_seed():
    _fresh()
    _TMP.write_text(json.dumps({"execute_broker": True, "max_risk_per_trade": 99,
                                "account_id": "ABC"}), encoding="utf-8")
    settings._data = None
    assert settings.execute_broker() is True
    assert settings.max_risk() == 99 and settings.account_id() == "ABC"


def test_corrupt_file_reseeds_dry():
    _fresh()
    _TMP.write_text("{not json", encoding="utf-8")
    settings._data = None
    assert settings.execute_broker() is False            # falls back to safe seed


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} settings tests passed.")
