"""Lab labeling workspaces — persisted annotation documents the user builds before
training (Phase 1). A workspace is JSON: {name, trades:[...], annotations:[...], updated}.

Pure storage; no model logic. Phase 3 training consumes these. Stored under
data/lab/workspaces/<name>.json (atomic writes).
"""
from __future__ import annotations

import json
import os
import re

import config

WS_DIR = config.DATA_DIR / "lab" / "workspaces"
_NAME = re.compile(r"^[A-Za-z0-9 _-]{1,60}$")


def valid_name(name: str) -> bool:
    return bool(name and _NAME.match(name))


def _path(name: str):
    return WS_DIR / f"{name}.json"


def list_workspaces() -> list[dict]:
    if not WS_DIR.exists():
        return []
    out = []
    for p in sorted(WS_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append({"name": p.stem, "trades": len(d.get("trades", [])),
                        "annotations": len(d.get("annotations", [])),
                        "updated": d.get("updated")})
        except Exception:
            pass
    return out


def load(name: str):
    p = _path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save(name: str, data: dict) -> bool:
    WS_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["name"] = name
    tmp = _path(name).with_name(f"{name}.json.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, _path(name))                     # atomic
    return True


def delete(name: str) -> bool:
    p = _path(name)
    if p.exists():
        p.unlink()
        return True
    return False
