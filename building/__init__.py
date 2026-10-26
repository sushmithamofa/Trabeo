"""Shared building model for the universal CAD-to-Revit tool."""
import importlib.util as _ilu
import os as _os

# text measuring on a PC that blocks fontTools' compiled parts (cad2revit/core/fontguard.py)
_spec = _ilu.spec_from_file_location(
    "_c2r_fontguard", _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                                    "cad2revit", "core", "fontguard.py"))
if _spec is not None and _os.path.exists(_spec.origin or ""):
    _fg = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_fg)
    _fg.use_plain_python_if_blocked()
from .model import (Building, Element, Evidence, Issue, Sheet, Value,  # noqa: F401
                    READ, DEFAULT, MISSING, CONFLICT, OVERRIDE)
from .support import SUPPORT, support_of  # noqa: F401
