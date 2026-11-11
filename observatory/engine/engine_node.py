"""The engine's own `node` package, whatever else this process imported under the name `node` (OBS-507).

`tools/node.py` is a module named `node`. A program started from `tools/` (the keyserver, the HTTP doors), or one
that put `tools/` first on `sys.path`, binds the name `node` to that module, and `from node import actions` then
raises ImportError. `module(name)` answers `node.<name>` of the package beside this file:

* when the name `node` is already the engine's package, or importing it now would find the engine's package, the
  ordinary import (one copy of each module in the process);
* otherwise the package is loaded under a private name from its own folder, so its relative imports resolve and the
  name `node` is never bound to `tools/node.py` by this lookup (that would shadow the package for every later import
  in the process).

Only submodules whose imports are relative work under the private name: `node/lifecycle/` imports `node.lifecycle`
by its absolute name and still needs the engine's package on the path. Standard library only.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

PACKAGE = Path(__file__).resolve().parent / "node"
#: The name the package is loaded under when `node` is not the engine's.
PRIVATE = "_observatory_engine_node"


def _is_engine(locations) -> bool:
    try:
        return bool(locations) and Path(list(locations)[0]).resolve() == PACKAGE
    except (OSError, TypeError, IndexError):
        return False


def module(name: str) -> ModuleType:
    """`node.<name>` of the engine beside this file (`name` is a submodule, for example `actions` or `shares`)."""
    loaded = sys.modules.get("node")
    if loaded is not None and _is_engine(getattr(loaded, "__path__", None)):
        return importlib.import_module("node." + name)              # the engine's package is the one imported
    if loaded is None:
        spec = importlib.util.find_spec("node")
        if spec is not None and _is_engine(spec.submodule_search_locations):
            return importlib.import_module("node." + name)          # importing `node` now finds the engine's package
    if PRIVATE not in sys.modules:
        spec = importlib.util.spec_from_file_location(PRIVATE, PACKAGE / "__init__.py",
                                                      submodule_search_locations=[str(PACKAGE)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[PRIVATE] = package
        try:
            spec.loader.exec_module(package)
        except BaseException:
            del sys.modules[PRIVATE]
            raise
    return importlib.import_module(PRIVATE + "." + name)
