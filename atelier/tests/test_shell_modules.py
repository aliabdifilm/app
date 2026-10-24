"""Module auto-discovery (app.modules) and the module contract."""

from __future__ import annotations

import sys
import textwrap

import pytest

import app.modules as modules_pkg
from app import create_app
from app.extensions import get_state
from app.modules import discover_names, register_modules


def test_every_module_in_the_tree_loads(app):
    """Guards the whole tree: a module that fails to import shows its traceback here."""
    errors = get_state(app).module_errors
    assert errors == {}, "\n\n".join(f"app.modules.{name}:\n{trace}" for name, trace in errors.items())


def test_shell_modules_are_registered_first(app):
    registered = get_state(app).modules
    assert registered[:2] == ["main", "auth"]
    assert set(registered) == set(discover_names())


def test_admin_loads_last_when_present():
    names = discover_names()
    if "admin" in names:
        assert names[-1] == "admin"
    assert names[:2] == ["main", "auth"]


def test_every_blueprint_is_named_after_its_package(app):
    for name in get_state(app).modules:
        assert name in app.blueprints, f"app.modules.{name} must expose bp = Blueprint('{name}', __name__, ...)"


def test_endpoints_are_unique_and_prefixed(app):
    seen = {}
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        assert "." in rule.endpoint, rule.endpoint
        key = (rule.rule, tuple(sorted(rule.methods - {"HEAD", "OPTIONS"})))
        assert key not in seen, f"{rule.rule} registered by {seen[key]} and {rule.endpoint}"
        seen[key] = rule.endpoint


def test_state_mutating_routes_are_not_get(app):
    """Nothing that changes state may answer GET (CSRF/prefetch safety)."""
    suspicious = ("delete", "remove", "cancel", "approve", "reject", "suspend", "mark", "toggle")
    offenders = []
    for rule in app.url_map.iter_rules():
        if "GET" in rule.methods and rule.endpoint != "static":
            tail = rule.endpoint.split(".", 1)[1]
            if any(tail.startswith(word) or tail.endswith(word) for word in suspicious):
                if "POST" not in rule.methods and not tail.endswith(("_form", "_page", "_confirm")):
                    offenders.append(rule.endpoint)
    assert offenders == [], f"GET routes that look state-changing: {offenders}"


# --------------------------------------------------------------------------
# discovery mechanics with throw-away packages
# --------------------------------------------------------------------------


@pytest.fixture
def fake_modules(tmp_path, monkeypatch):
    """Put extra packages on app.modules' search path."""
    monkeypatch.setattr(modules_pkg, "__path__", [*modules_pkg.__path__, str(tmp_path)])
    created: list[str] = []

    def make(name, source):
        package = tmp_path / name
        package.mkdir()
        (package / "__init__.py").write_text(textwrap.dedent(source), encoding="utf-8")
        created.append(f"app.modules.{name}")

    yield make
    for qualified in created:
        sys.modules.pop(qualified, None)


GOOD = """
    from flask import Blueprint
    bp = Blueprint("zz_good", __name__, url_prefix="/zz")

    @bp.get("/ping")
    def ping():
        return "pong"

    CALLED = []

    def register(app):
        CALLED.append(app)
"""


def test_a_new_package_is_picked_up_and_wired(fake_modules, tmp_path):
    fake_modules("zz_good", GOOD)
    app = create_app("testing", UPLOAD_DIR=str(tmp_path / "up"))
    assert "zz_good" in get_state(app).modules
    assert app.test_client().get("/zz/ping").get_data(as_text=True) == "pong"
    assert sys.modules["app.modules.zz_good"].CALLED == [app]  # register(app) hook ran


def test_packages_without_blueprint_are_fine(fake_modules, tmp_path):
    fake_modules("zz_plain", "VALUE = 1\n")
    app = create_app("testing", UPLOAD_DIR=str(tmp_path / "up"))
    assert "zz_plain" in get_state(app).modules


def test_underscore_packages_are_ignored(fake_modules):
    fake_modules("_private", "raise RuntimeError('must never be imported')\n")
    assert "_private" not in discover_names()


def test_disabled_modules_are_skipped(fake_modules, tmp_path):
    fake_modules("zz_good", GOOD)
    app = create_app("testing", UPLOAD_DIR=str(tmp_path / "up"), DISABLED_MODULES=("zz_good",))
    assert "zz_good" not in get_state(app).modules
    assert app.test_client().get("/zz/ping").status_code == 404


def test_wrong_blueprint_name_is_rejected(fake_modules, tmp_path):
    fake_modules("zz_misnamed", 'from flask import Blueprint\nbp = Blueprint("other_name", __name__)\n')
    app = create_app("testing", UPLOAD_DIR=str(tmp_path / "up"))
    error = get_state(app).module_errors["zz_misnamed"]
    assert "must equal the package name" in error
    assert "zz_misnamed" not in get_state(app).modules


def test_non_blueprint_bp_is_rejected(fake_modules, tmp_path):
    fake_modules("zz_notbp", "bp = object()\n")
    app = create_app("testing", UPLOAD_DIR=str(tmp_path / "up"))
    assert "flask.Blueprint" in get_state(app).module_errors["zz_notbp"]


def test_broken_module_is_recorded_in_testing_mode_and_others_still_load(fake_modules, tmp_path):
    fake_modules("zz_broken", "raise RuntimeError('boom at import')\n")
    fake_modules("zz_good", GOOD)
    app = create_app("testing", UPLOAD_DIR=str(tmp_path / "up"))
    state = get_state(app)
    assert "boom at import" in state.module_errors["zz_broken"]
    assert "zz_good" in state.modules and "main" in state.modules
    assert app.test_client().get("/zz/ping").status_code == 200


def test_broken_module_aborts_startup_in_strict_mode(fake_modules, tmp_path):
    fake_modules("zz_broken", "raise RuntimeError('boom at import')\n")
    with pytest.raises(RuntimeError, match="boom at import"):
        create_app("testing", UPLOAD_DIR=str(tmp_path / "up"), MODULES_STRICT=True)


def test_register_hook_failure_is_reported(fake_modules, tmp_path):
    fake_modules("zz_hook", "def register(app):\n    raise ValueError('hook failed')\n")
    app = create_app("testing", UPLOAD_DIR=str(tmp_path / "up"))
    assert "hook failed" in get_state(app).module_errors["zz_hook"]


def test_register_modules_can_target_specific_names(tmp_path):
    app = create_app("testing", UPLOAD_DIR=str(tmp_path / "up"), DISABLED_MODULES=tuple(discover_names()))
    assert get_state(app).modules == []
    assert register_modules(app, ["main"]) == []  # still disabled by config
