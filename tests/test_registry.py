"""Isolation/regression tests for the model registry.

GUARANTEE under test: introducing the registry does NOT change existing behavior — with
no model promoted, score.py loads the same production model from data/models/ as before.

    PYTHONPATH=. python tests/test_registry.py      # or: pytest tests/test_registry.py
"""
from __future__ import annotations

import contextlib
import json

import config
from src import registry, score


@contextlib.contextmanager
def as_production():
    """Temporarily point ACTIVE at production (a real challenger may be promoted live, e.g.
    cand_eng), so the backward-compat guarantee can be checked, then restore the real state."""
    orig = registry.ACTIVE_FILE.read_text(encoding="utf-8").strip() if registry.ACTIVE_FILE.exists() else None
    registry.set_active(registry.PROD)
    try:
        yield
    finally:
        if orig is not None:
            registry.set_active(orig)
        elif registry.ACTIVE_FILE.exists():
            registry.ACTIVE_FILE.unlink()


def test_default_active_is_production():
    with as_production():                                # no/invalid ACTIVE => production root
        assert registry.active_name() == registry.PROD
        assert registry.active_dir() == registry.ROOT


def test_score_loads_production_unchanged():
    with as_production():                                # active=production => loads the root files
        assert score.available()
        cfg, primary, meta, iso = score._load()
        raw = json.loads((registry.ROOT / "config.json").read_text(encoding="utf-8"))
        assert cfg == raw                               # registry path resolves to root
        assert {"features", "best_q", "cost_max_r"} <= set(cfg)


def test_list_includes_production_active():
    with as_production():
        models = registry.list_models()
        prod = [m for m in models if m["name"] == registry.PROD]
        assert prod and prod[0]["active"] is True


def test_cannot_activate_unknown():
    with as_production():
        try:
            registry.set_active("does-not-exist")
            assert False, "expected ValueError"
        except ValueError:
            pass
        assert registry.active_name() == registry.PROD   # unchanged


def test_register_never_touches_root():
    # 'production' is reserved; register refuses it (root model never overwritten)
    try:
        registry.register(registry.PROD, registry.ROOT, {})
        assert False, "expected ValueError"
    except ValueError:
        pass


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} registry isolation tests passed.")
