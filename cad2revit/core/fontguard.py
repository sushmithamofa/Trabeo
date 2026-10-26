"""Text measuring that survives a PC blocking fontTools' compiled parts.

ezdxf measures every TEXT / MTEXT with fontTools. Windows Application Control (Smart
App Control, or an office's code policy) can refuse to load fontTools' compiled
bezierTools module (seen 2026-10-24 on this PC: "DLL load failed while importing
bezierTools: An Application Control policy has blocked this file"). Every text
measurement then fails - and with it the size of every block holding text, so a
floor plan kept in blocks looked empty and was dropped as a scrap.

fontTools ships the same module as plain Python next to the compiled one. When the
compiled one cannot load, the plain one is used instead: the same code and the same
numbers, a little slower. Nothing changes on a PC where the compiled module loads.
"""
from __future__ import annotations

import importlib.util
import os
import sys

_NAME = "fontTools.misc.bezierTools"


def use_plain_python_if_blocked() -> str:
    """"" when the compiled module loads (or fontTools is absent); else what was done."""
    if _NAME in sys.modules:
        return ""
    try:
        import fontTools.misc as misc
    except Exception:
        return ""
    try:
        importlib.import_module(_NAME)
        return ""
    except ImportError:
        pass
    except Exception:
        return ""
    path = os.path.join(os.path.dirname(misc.__file__), "bezierTools.py")
    if not os.path.exists(path):
        return ""
    try:
        spec = importlib.util.spec_from_file_location(_NAME, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_NAME] = mod
        spec.loader.exec_module(mod)
        misc.bezierTools = mod
        return "fontTools' compiled bezierTools is blocked on this PC: its plain-Python copy is used"
    except Exception:
        sys.modules.pop(_NAME, None)
        return ""
