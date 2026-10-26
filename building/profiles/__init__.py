"""Consultant profiles: one consultant's conventions, kept out of the code.

A profile is a small JSON file:

    {
      "name": "Sasan Gir consultant",
      "confirmed": false,
      "layer_roles": {"beam": ["BEAM-230X600", "BEAM-*X*"], "column": ["STR.COLUMN"]},
      "prefix_families": {"GB": "beam", "PD": "footing"},
      "evidence": {"BEAM-230X600": "511 long lines, 78% in parallel pairs; name says BEAM"}
    }

It is laid over the base config (cad2revit\\config.json) without changing it:
its layer patterns are added in front of the base ones, so they win. A project
can lay its own overrides over the consultant's the same way (apply() takes
several profiles, later ones winning).

A suggested profile (suggest.py) is written with "confirmed": false. The set
reader only uses a profile that is confirmed, unless told explicitly to use
suggestions - a guess never drives extraction silently.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE_CONFIG = os.path.join(HERE, "cad2revit", "config.json")


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(profile: dict, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=1)
    return path


def base_config() -> dict:
    with open(BASE_CONFIG, encoding="utf-8") as f:
        return json.load(f)


def apply(cfg: dict, *profiles: dict) -> dict:
    """The base config with these profiles laid over it, later profiles winning."""
    out = copy.deepcopy(cfg)
    roles = out.setdefault("layer_roles", {})
    for p in profiles:
        for role, patterns in (p.get("layer_roles") or {}).items():
            # the profile's layers leave every other role, then go first in their own
            mine = {x.upper() for x in patterns}
            for r in roles:
                roles[r] = [x for x in roles[r] if x.upper() not in mine]
            roles[role] = list(patterns) + roles.get(role, [])
        for fam, prefixes in (p.get("mark_prefixes") or {}).items():
            out.setdefault("mark_prefixes", {}).setdefault(fam, [])
            out["mark_prefixes"][fam] = sorted(set(out["mark_prefixes"][fam]) | set(prefixes))
    return out


def config_file(*profiles: dict, work_dir: str | None = None, extra: dict | None = None) -> str:
    """A config.json for the extractor with these profiles applied (and extra keys,
    e.g. the set's legend sheet)."""
    cfg = apply(base_config(), *profiles)
    cfg.update(extra or {})
    path = os.path.join(work_dir or tempfile.mkdtemp(prefix="c2r_cfg_"),
                        "config_with_profile.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1)
    return path


def families(*profiles: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in profiles:
        out.update({k.upper(): v for k, v in (p.get("prefix_families") or {}).items()})
    return out
