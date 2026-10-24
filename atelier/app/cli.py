"""Flask CLI commands (``flask --app wsgi <command>``).

``init-db``        create the tables (idempotent; ``--drop --yes`` starts from scratch)
``create-admin``   create (or promote) a site administrator - the only way to get an admin
``seed-demo``      load demo data via ``scripts/seed_demo.py`` (``main()``)
``routes-matrix``  every route x every role -> HTTP status, for QA and access-control reviews
"""

from __future__ import annotations

import csv
import importlib.util
import inspect
import io
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

import click
import sqlalchemy as sa
from flask import Flask, current_app
from flask.cli import with_appcontext

from app.extensions import db
from app.services.constants import Role, UserStatus

MATRIX_ROLES: tuple[str, ...] = ("anonymous", "admin", "hall_manager", "atelier_manager", "staff")


def _project_root() -> Path:
    return Path(current_app.root_path).parent


# --------------------------------------------------------------------------
# init-db
# --------------------------------------------------------------------------


@click.command("init-db")
@click.option("--drop", is_flag=True, help="Drop every table first (destroys all data).")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@with_appcontext
def init_db_command(drop: bool, yes: bool) -> None:
    """Create all database tables (safe to run again; existing tables are kept)."""
    from app import models  # noqa: F401  (registers the tables)

    if drop:
        if not yes and not click.confirm("This DELETES ALL DATA. Continue?", default=False):
            raise click.Abort()
        db.drop_all()
        click.echo("Dropped all tables.")
    db.create_all()
    tables = sorted(sa.inspect(db.engine).get_table_names())
    click.echo(f"Database ready: {len(tables)} tables ({db.engine.url.render_as_string(hide_password=True)}).")


# --------------------------------------------------------------------------
# create-admin
# --------------------------------------------------------------------------


@click.command("create-admin")
@click.option("--mobile", required=True, help="Mobile number, e.g. 09121234567 (Persian digits and +98 work too).")
@click.option("--name", "full_name", required=True, help="Full name shown in the UI.")
@click.option("--password", default=None, help="Optional password (visible in shell history; prefer --ask-password).")
@click.option("--ask-password", is_flag=True, help="Prompt for an optional password (hidden input).")
@click.option("--promote", is_flag=True, help="If the number already has an account, make it an admin.")
@with_appcontext
def create_admin_command(mobile: str, full_name: str, password: Optional[str], ask_password: bool, promote: bool) -> None:
    """Create a site administrator (admins cannot register through the website)."""
    from app.models import User, normalize_mobile
    from app.services import audit
    from app.services.security import validate_password

    try:
        number = normalize_mobile(mobile)
    except ValueError:
        raise click.BadParameter("not a valid Iranian mobile number", param_hint="--mobile") from None
    if ask_password and not password:
        password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
    if password:
        problems = validate_password(password, mobile=number)
        if problems:
            raise click.BadParameter(" ".join(problems), param_hint="--password")

    insp = sa.inspect(db.engine)
    if not insp.has_table("users"):
        raise click.ClickException("The database has no tables yet. Run: flask --app wsgi init-db")

    user = db.session.execute(sa.select(User).where(User.mobile == number)).scalar_one_or_none()
    if user is not None:
        if user.role == Role.ADMIN:
            raise click.ClickException(f"{number} is already an admin.")
        if not promote:
            raise click.ClickException(
                f"{number} already has a {user.role.value} account. Use --promote to make it an admin."
            )
        user.role = Role.ADMIN
        user.status = UserStatus.ACTIVE
        action = "promoted"
    else:
        user = User(mobile=number, full_name=full_name.strip(), role=Role.ADMIN, status=UserStatus.ACTIVE)
        db.session.add(user)
        action = "created"
    if full_name.strip():
        user.full_name = full_name.strip()
    user.is_verified = True
    if password:
        user.set_password(password)
    db.session.flush()
    audit.log(None, f"user.admin_{action}", user, {"via": "cli"})
    db.session.commit()
    click.echo(f"Admin {action}: {user.display_name} ({number}). Sign in with an SMS code at /auth/login.")


# --------------------------------------------------------------------------
# seed-demo
# --------------------------------------------------------------------------


def load_seed_script(path: Optional[Path] = None):
    """Import ``scripts/seed_demo.py`` (None when it does not exist)."""
    path = path or _project_root() / "scripts" / "seed_demo.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("atelier_seed_demo", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run_seed(app: Flask, path: Optional[Path] = None) -> bool:
    """Call ``main()`` of the seed script inside *app*'s context; False if there is no script."""
    module = load_seed_script(path)
    if module is None or not callable(getattr(module, "main", None)):
        return False
    main = module.main
    params = inspect.signature(main).parameters
    with app.app_context():
        if "app" in params:
            main(app=app)
        else:
            main()
    return True


@click.command("seed-demo")
@click.option("--script", "script", type=click.Path(path_type=Path), default=None, help="Seed script (default: scripts/seed_demo.py).")
@with_appcontext
def seed_demo_command(script: Optional[Path]) -> None:
    """Fill the database with demo data (calls main() of scripts/seed_demo.py)."""
    if not run_seed(current_app._get_current_object(), script):
        raise click.ClickException("scripts/seed_demo.py (with a main() function) was not found.")
    click.echo("Demo data loaded.")


# --------------------------------------------------------------------------
# routes-matrix
# --------------------------------------------------------------------------


_CONVERTER_SAMPLE = {
    "int": "1",
    "float": "1.0",
    "uuid": "00000000-0000-0000-0000-000000000001",
    "path": "x",
    "string": "x",
    "any": "x",
}


def _sample_url(rule: Any) -> str:
    """URL for *rule* with sample values (``1`` for ids) in every converter slot."""
    values: dict[str, str] = {}
    for argument in rule.arguments:
        converter = rule._converters.get(argument)
        name = type(converter).__name__.replace("Converter", "").lower() if converter else "string"
        values[argument] = _CONVERTER_SAMPLE.get(name, "1")
    _domain, path = rule.build(values, append_unknown=False)
    return path


def _status_label(response: Any) -> str:
    code = response.status_code
    if code in (301, 302, 303, 307, 308):
        location = response.headers.get("Location", "")
        path = re.sub(r"^https?://[^/]+", "", location).split("?")[0]
        return f"{code}->{path}"
    return str(code)


def build_matrix(
    app: Flask,
    *,
    methods: tuple[str, ...] = ("GET",),
    include_static: bool = False,
    users: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Request every matching route as every role; returns one row per (rule, method).

    *users* maps role name -> User (created on the fly when omitted).  The
    caller supplies an app whose database may be modified freely.
    """
    from app.models import User
    from app.services.testing import IsolatedClient, login_client

    app.test_client_class = IsolatedClient  # one app context / session per request, like production
    rows: list[dict[str, Any]] = []
    with app.app_context():
        accounts = dict(users or {})
        for role in Role:
            if role.value not in accounts:
                user = db.session.execute(
                    sa.select(User).where(User.role == role).order_by(User.id).limit(1)
                ).scalar_one_or_none()
                if user is None:
                    user = User(
                        mobile={"admin": "09100000001", "hall_manager": "09100000002",
                                "atelier_manager": "09100000003", "staff": "09100000004"}[role.value],
                        full_name=f"matrix {role.value}",
                        role=role,
                        status=UserStatus.ACTIVE,
                    )
                    db.session.add(user)
                    db.session.commit()
                accounts[role.value] = user
        clients: dict[str, Any] = {}
        for role_name in MATRIX_ROLES:
            client = app.test_client()
            if role_name != "anonymous":
                login_client(client, accounts[role_name])
            clients[role_name] = client

        rules = sorted(app.url_map.iter_rules(), key=lambda r: (r.rule, r.endpoint))
        for rule in rules:
            if rule.endpoint == "static" and not include_static:
                continue
            for method in sorted(set(rule.methods or ()) & set(methods)):
                try:
                    url = _sample_url(rule)
                except Exception:  # unbuildable rule: report, do not crash the whole run
                    rows.append({"rule": rule.rule, "endpoint": rule.endpoint, "method": method, "url": None,
                                 "status": {name: "?" for name in MATRIX_ROLES}})
                    continue
                statuses: dict[str, str] = {}
                for role_name, client in clients.items():
                    try:
                        response = client.open(url, method=method)
                        statuses[role_name] = _status_label(response)
                    except Exception as exc:  # a crashing view is itself a finding
                        statuses[role_name] = f"EXC {type(exc).__name__}"
                    finally:
                        db.session.rollback()
                rows.append({"rule": rule.rule, "endpoint": rule.endpoint, "method": method, "url": url,
                             "status": statuses})
    return rows


def format_matrix(rows: list[dict[str, Any]], fmt: str = "table") -> str:
    if fmt == "json":
        return json.dumps(rows, ensure_ascii=False, indent=2)
    if fmt == "csv":
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["method", "rule", "endpoint", *MATRIX_ROLES])
        for row in rows:
            writer.writerow([row["method"], row["rule"], row["endpoint"], *(row["status"][r] for r in MATRIX_ROLES)])
        return out.getvalue()
    head = ["METHOD", "ROUTE", *[r.upper() for r in MATRIX_ROLES]]
    body = [[row["method"], row["rule"], *(row["status"][r] for r in MATRIX_ROLES)] for row in rows]
    widths = [max(len(str(line[i])) for line in [head, *body]) for i in range(len(head))]
    lines = ["  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(line)).rstrip() for line in [head, *body]]
    return "\n".join(lines)


@click.command("routes-matrix")
@click.option("--format", "fmt", type=click.Choice(["table", "json", "csv"]), default="table", show_default=True)
@click.option("--methods", default="GET", show_default=True, help="Comma list, e.g. GET,POST (POST runs with CSRF off on a throw-away database).")
@click.option("--include-static", is_flag=True, help="Also list /static.")
@click.option("--seed", is_flag=True, help="Load scripts/seed_demo.py into the throw-away database first.")
@with_appcontext
def routes_matrix_command(fmt: str, methods: str, include_static: bool, seed: bool) -> None:
    """Print route x role status codes (anonymous, admin, hall_manager, atelier_manager, staff).

    Runs against a fresh in-memory database so nothing in your real data is touched.
    Typical reading: a protected route must show 302->/auth/login for anonymous and 403 for
    the wrong roles; a 200 where you expect 403 is an access-control bug.
    """
    from app import create_app

    base = current_app
    app = create_app(
        "testing",
        SQLALCHEMY_DATABASE_URI="sqlite://",
        WTF_CSRF_ENABLED=False,
        DISABLED_MODULES=tuple(base.config.get("DISABLED_MODULES") or ()),
        MODULES_STRICT=False,
    )
    wanted = tuple(m.strip().upper() for m in methods.split(",") if m.strip())
    if seed and not run_seed(app):
        raise click.ClickException("scripts/seed_demo.py (with a main() function) was not found.")
    rows = build_matrix(app, methods=wanted, include_static=include_static)
    click.echo(format_matrix(rows, fmt))
    errors = sum(1 for row in rows for status in row["status"].values() if str(status).startswith("EXC") or str(status).startswith("5"))
    if errors:
        click.echo(f"\n{errors} cell(s) crashed or returned 5xx.", err=True)
        sys.exit(1)


def init_app(app: Flask) -> None:
    app.cli.add_command(init_db_command)
    app.cli.add_command(create_admin_command)
    app.cli.add_command(seed_demo_command)
    app.cli.add_command(routes_matrix_command)
