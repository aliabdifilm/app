"""Module auto-discovery.

Every sub-package of ``app.modules`` is one feature module (``auth``, ``main``,
``halls``, ``ateliers``, ``staff``, ``marketplace``, ``accounting``,
``inventory``, ``admin`` ...).  :func:`register_modules` - called once by
:func:`app.create_app` - imports each package and wires it into the app:

* ``bp``                  a ``flask.Blueprint`` whose **name equals the package name**
                          (endpoints are then ``<package>.<view>``) and which carries its
                          own ``url_prefix``; registered with ``app.register_blueprint``.
* ``register(app)``       optional hook called after the blueprint is registered
                          (error handlers, CLI commands, Jinja filters, event-bus subscriptions ...).

Nothing else is required: a module adds its menu entries in its own
``__init__.py`` with ``app.services.nav.register(...)`` (see ``docs/ARCHITECTURE.md``).

Packages whose name starts with ``_`` are ignored; ``DISABLED_MODULES``
(config / env var) skips modules by name.  Load order: ``main``, ``auth``, then
the rest alphabetically (``admin`` last), so the shell is always first.

A module that raises on import aborts startup (``MODULES_STRICT``, the default)
so a broken deploy fails loudly; the test configuration records the traceback
in ``app.extensions["atelier"].module_errors`` instead, and
``tests/test_shell_modules.py`` fails with that traceback.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import traceback
from typing import Iterable

from flask import Blueprint, Flask

from app.extensions import get_state

logger = logging.getLogger(__name__)

FIRST = ("main", "auth")
LAST = ("admin",)


def discover_names() -> list[str]:
    """Names of all module packages, in load order."""
    package = importlib.import_module(__name__)
    found = sorted(
        info.name for info in pkgutil.iter_modules(package.__path__) if info.ispkg and not info.name.startswith("_")
    )
    first = [name for name in FIRST if name in found]
    last = [name for name in LAST if name in found]
    middle = [name for name in found if name not in first and name not in last]
    return first + middle + last


def register_modules(app: Flask, names: Iterable[str] | None = None) -> list[str]:
    """Import and register the modules; returns the names that were registered."""
    state = get_state(app)
    disabled = set(app.config.get("DISABLED_MODULES") or ())
    strict = bool(app.config.get("MODULES_STRICT", True))
    registered: list[str] = []
    for name in list(names) if names is not None else discover_names():
        if name in disabled:
            logger.info("module %s is disabled", name)
            continue
        try:
            module = importlib.import_module(f"{__name__}.{name}")
            blueprint = getattr(module, "bp", None)
            if blueprint is not None:
                if not isinstance(blueprint, Blueprint):
                    raise TypeError(f"app.modules.{name}.bp must be a flask.Blueprint")
                if blueprint.name != name:
                    raise ValueError(
                        f"blueprint of app.modules.{name} is named {blueprint.name!r}; "
                        f"it must equal the package name so endpoints read '{name}.<view>'"
                    )
                app.register_blueprint(blueprint)
            hook = getattr(module, "register", None)
            if callable(hook):
                hook(app)
        except Exception:
            state.module_errors[name] = traceback.format_exc()
            logger.error("module %s failed to load:\n%s", name, state.module_errors[name])
            if strict:
                raise
            continue
        registered.append(name)
    state.modules = registered
    return registered
