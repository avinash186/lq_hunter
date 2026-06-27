"""Tests for the weekly-retrain registry extensions: lineage, the cumulative trial
counter, manifest mutation, and champion/challenger promotion + rollback.

Every test runs against a TEMP registry (no touching real data/models) and restores the
module globals afterward, so it is safe under a single pytest session alongside
test_registry.py.

    PYTHONPATH=. python tests/test_registry_v2.py    # or: pytest tests/test_registry_v2.py
"""
from __future__ import annotations

import contextlib
import json
import shutil
import tempfile
from pathlib import Path

from src import registry


@contextlib.contextmanager
def tmp_registry():
    orig = (registry.ROOT, registry.ACTIVE_FILE, registry.TRIAL_FILE)
    tmp = Path(tempfile.mkdtemp())
    registry.ROOT, registry.ACTIVE_FILE, registry.TRIAL_FILE = tmp, tmp / "ACTIVE", tmp / "TRIAL_N"
    try:
        yield tmp
    finally:
        registry.ROOT, registry.ACTIVE_FILE, registry.TRIAL_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)


def _mk_src(tmp, name="_src", cfg=None):
    s = tmp / name
    s.mkdir(parents=True, exist_ok=True)
    (s / "config.json").write_text(json.dumps(cfg or {"features": ["x"]}))
    return s


# ── trial counter ──
def test_trial_counter_starts_zero_and_persists():
    with tmp_registry():
        assert registry.trial_n() == 0
        assert registry.bump_trial() == 1
        assert registry.bump_trial() == 2
        assert registry.trial_n() == 2                    # persisted to disk
        assert int(registry.TRIAL_FILE.read_text()) == 2


# ── register defaults ──
def test_register_sets_challenger_defaults():
    with tmp_registry() as tmp:
        registry.bump_trial(); registry.bump_trial()      # N = 2
        registry.register("wk1", _mk_src(tmp), {"metrics": {"oos": {}}})
        m = registry.manifest("wk1")
        assert m["status"] == registry.CHALLENGER         # inert until promoted
        assert isinstance(m["created_at"], int)
        assert m["trial_n"] == 2                           # stamped with the trial index
        assert m["name"] == "wk1"


def test_register_honors_explicit_status():
    with tmp_registry() as tmp:
        registry.register("wk1", _mk_src(tmp), {"status": registry.CHAMPION})
        assert registry.manifest("wk1")["status"] == registry.CHAMPION


# ── promotion + lineage ──
def test_promote_sets_live_and_stamps():
    with tmp_registry() as tmp:
        registry.register("a", _mk_src(tmp, "_a"), {})
        res = registry.promote("a")
        assert registry.active_name() == "a"
        assert registry.live_name() == "a" and registry.is_live("a")
        assert registry.manifest("a")["status"] == registry.CHAMPION
        assert "promoted_at" in registry.manifest("a")
        assert res["previous"] == registry.PROD


def test_promote_archives_previous_champion():
    with tmp_registry() as tmp:
        registry.register("a", _mk_src(tmp, "_a"), {})
        registry.register("b", _mk_src(tmp, "_b"), {})
        registry.promote("a")
        registry.promote("b")
        assert registry.active_name() == "b"
        assert registry.manifest("b")["status"] == registry.CHAMPION
        assert registry.manifest("a")["status"] == registry.ARCHIVED   # prior champion retired


def test_rollback_to_older_model_anytime():
    # any registered model can be promoted at any time -> manual rollback path
    with tmp_registry() as tmp:
        registry.register("a", _mk_src(tmp, "_a"), {})
        registry.register("b", _mk_src(tmp, "_b"), {})
        registry.promote("a"); registry.promote("b")
        registry.promote("a")                              # roll back to the older model
        assert registry.active_name() == "a"
        assert registry.manifest("a")["status"] == registry.CHAMPION
        assert registry.manifest("b")["status"] == registry.ARCHIVED


def test_promote_auto_flag_recorded():
    with tmp_registry() as tmp:
        registry.register("a", _mk_src(tmp, "_a"), {})
        registry.promote("a", auto=True)
        assert registry.manifest("a")["promoted_auto"] is True


def test_promote_unknown_raises_and_keeps_live():
    with tmp_registry() as tmp:
        registry.register("a", _mk_src(tmp, "_a"), {})
        registry.promote("a")
        try:
            registry.promote("ghost")
            assert False, "expected ValueError"
        except ValueError:
            pass
        assert registry.active_name() == "a"               # unchanged on failure


# ── list flags + default behavior ──
def test_list_models_live_flag():
    with tmp_registry() as tmp:
        registry.register("a", _mk_src(tmp, "_a"), {})
        registry.register("b", _mk_src(tmp, "_b"), {})
        registry.promote("b")
        by = {m["name"]: m for m in registry.list_models()}
        assert by["b"]["live"] is True and by["b"]["active"] is True
        assert by["a"]["live"] is False


def test_default_active_is_production_when_unset():
    with tmp_registry():
        assert registry.active_name() == registry.PROD     # no ACTIVE pointer => root
        assert registry.active_dir() == registry.ROOT


def test_update_manifest_is_additive():
    with tmp_registry() as tmp:
        registry.register("a", _mk_src(tmp, "_a"), {"keep": 1})
        registry.update_manifest("a", note="hello")
        m = registry.manifest("a")
        assert m["keep"] == 1 and m["note"] == "hello"      # merge, not overwrite


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} registry-v2 tests passed.")
