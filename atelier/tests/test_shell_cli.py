"""The flask CLI: init-db, create-admin, seed-demo, routes-matrix."""

from __future__ import annotations

import csv
import io
import json
import textwrap

import pytest
import sqlalchemy as sa

from app import create_app
from app.cli import build_matrix, format_matrix
from app.extensions import db
from app.models import AuditLog, User
from app.services.constants import Role, UserStatus


@pytest.fixture
def runner(app):
    return app.test_cli_runner()


# --------------------------------------------------------------------------
# init-db
# --------------------------------------------------------------------------


def test_init_db_creates_the_tables(runner):
    db.drop_all()
    result = runner.invoke(args=["init-db"])
    assert result.exit_code == 0, result.output
    assert "Database ready" in result.output
    assert sa.inspect(db.engine).has_table("users") and sa.inspect(db.engine).has_table("assignments")


def test_init_db_is_idempotent_and_keeps_data(runner, make_user):
    make_user("staff")
    for _ in range(2):
        assert runner.invoke(args=["init-db"]).exit_code == 0
    assert db.session.execute(sa.select(sa.func.count(User.id))).scalar_one() == 1


def test_init_db_drop_needs_confirmation(runner, make_user):
    make_user("staff")
    refused = runner.invoke(args=["init-db", "--drop"], input="n\n")
    assert refused.exit_code != 0
    assert db.session.execute(sa.select(sa.func.count(User.id))).scalar_one() == 1
    done = runner.invoke(args=["init-db", "--drop", "--yes"])
    assert done.exit_code == 0 and "Dropped all tables" in done.output
    assert db.session.execute(sa.select(sa.func.count(User.id))).scalar_one() == 0


# --------------------------------------------------------------------------
# create-admin
# --------------------------------------------------------------------------


def test_create_admin(runner):
    result = runner.invoke(args=["create-admin", "--mobile", "۰۹۱۲۱۲۳۴۵۶۷", "--name", " علی عبدی "])
    assert result.exit_code == 0, result.output
    user = db.session.execute(sa.select(User)).scalar_one()
    assert (user.mobile, user.full_name, user.role, user.status) == ("09121234567", "علی عبدی", Role.ADMIN, UserStatus.ACTIVE)
    assert user.is_verified and user.password_hash is None
    audit = db.session.execute(sa.select(AuditLog).where(AuditLog.action == "user.admin_created")).scalar_one()
    assert audit.data == {"via": "cli"} and audit.actor_id is None


def test_create_admin_with_password(runner):
    result = runner.invoke(args=["create-admin", "--mobile", "09121234567", "--name", "علی", "--password", "Sup3r-secret-pass"])
    assert result.exit_code == 0
    user = db.session.execute(sa.select(User)).scalar_one()
    assert user.check_password("Sup3r-secret-pass")
    assert "Sup3r" not in result.output


def test_create_admin_prompts_for_a_hidden_password(runner):
    result = runner.invoke(
        args=["create-admin", "--mobile", "09121234567", "--name", "علی", "--ask-password"],
        input="Sup3r-secret-pass\nSup3r-secret-pass\n",
    )
    assert result.exit_code == 0, result.output
    assert db.session.execute(sa.select(User)).scalar_one().check_password("Sup3r-secret-pass")


@pytest.mark.parametrize("mobile", ["12345", "abc", "08121234567", ""])
def test_create_admin_validates_the_number(runner, mobile):
    result = runner.invoke(args=["create-admin", "--mobile", mobile, "--name", "علی"])
    assert result.exit_code != 0
    assert db.session.execute(sa.select(User)).first() is None


def test_create_admin_rejects_weak_passwords(runner):
    result = runner.invoke(args=["create-admin", "--mobile", "09121234567", "--name", "علی", "--password", "12345678"])
    assert result.exit_code != 0 and db.session.execute(sa.select(User)).first() is None


def test_create_admin_refuses_existing_accounts_unless_promoted(runner, make_user):
    make_user("hall_manager", mobile="09121234567", full_name="مدیر")
    refused = runner.invoke(args=["create-admin", "--mobile", "09121234567", "--name", "علی"])
    assert refused.exit_code != 0 and "--promote" in refused.output
    assert db.session.execute(sa.select(User)).scalar_one().role == Role.HALL_MANAGER
    promoted = runner.invoke(args=["create-admin", "--mobile", "09121234567", "--name", "علی", "--promote"])
    assert promoted.exit_code == 0 and "promoted" in promoted.output
    db.session.expire_all()
    assert db.session.execute(sa.select(User)).scalar_one().role == Role.ADMIN
    again = runner.invoke(args=["create-admin", "--mobile", "09121234567", "--name", "علی", "--promote"])
    assert again.exit_code != 0 and "already an admin" in again.output


def test_create_admin_needs_the_tables(runner):
    db.drop_all()
    result = runner.invoke(args=["create-admin", "--mobile", "09121234567", "--name", "علی"])
    assert result.exit_code != 0 and "init-db" in result.output


def test_admin_cannot_be_created_through_the_website(client, outbox):
    """The CLI is the only door: /auth/register never offers or accepts the admin role."""
    response = client.post("/auth/register", data={"full_name": "x y", "role": "admin", "mobile": "09121234567"})
    assert response.status_code == 422


# --------------------------------------------------------------------------
# seed-demo
# --------------------------------------------------------------------------


def test_seed_demo_reports_a_missing_script(runner, tmp_path):
    result = runner.invoke(args=["seed-demo", "--script", str(tmp_path / "nope.py")])
    assert result.exit_code != 0 and "seed_demo.py" in result.output


def test_seed_demo_calls_main(runner, tmp_path):
    script = tmp_path / "seed_demo.py"
    script.write_text(
        textwrap.dedent(
            """
            from app.extensions import db
            from app.models import User

            def main():
                db.session.add(User(mobile="09120000001", full_name="seeded", role="staff"))
                db.session.commit()
            """
        )
    )
    result = runner.invoke(args=["seed-demo", "--script", str(script)])
    assert result.exit_code == 0, result.output
    assert db.session.execute(sa.select(User.full_name)).scalar_one() == "seeded"


def test_seed_demo_main_may_take_the_app(runner, tmp_path, app):
    script = tmp_path / "seed_demo.py"
    script.write_text("CALLS = []\n\ndef main(app):\n    CALLS.append(app.name)\n")
    result = runner.invoke(args=["seed-demo", "--script", str(script)])
    assert result.exit_code == 0 and "Demo data loaded" in result.output


def test_seed_demo_script_without_main_is_an_error(runner, tmp_path):
    script = tmp_path / "seed_demo.py"
    script.write_text("x = 1\n")
    assert runner.invoke(args=["seed-demo", "--script", str(script)]).exit_code != 0


# --------------------------------------------------------------------------
# routes-matrix
# --------------------------------------------------------------------------


def matrix_row(rows, rule, method="GET"):
    return next(row for row in rows if row["rule"] == rule and row["method"] == method)


@pytest.fixture(scope="module")
def matrix(tmp_path_factory):
    app = create_app("testing", UPLOAD_DIR=str(tmp_path_factory.mktemp("matrix")))
    return build_matrix(app)


def test_matrix_covers_every_role(matrix):
    row = matrix_row(matrix, "/dashboard")
    assert set(row["status"]) == {"anonymous", "admin", "hall_manager", "atelier_manager", "staff"}


def test_matrix_shows_protection(matrix):
    dashboard = matrix_row(matrix, "/dashboard")["status"]
    assert dashboard["anonymous"].startswith("302->/auth/login")
    assert all(dashboard[role] == "200" for role in ("admin", "hall_manager", "atelier_manager", "staff"))
    landing = matrix_row(matrix, "/")["status"]
    assert set(landing.values()) == {"200"}
    health = matrix_row(matrix, "/healthz")["status"]
    assert set(health.values()) == {"200"}


def test_matrix_login_pages_bounce_signed_in_users(matrix):
    login = matrix_row(matrix, "/auth/login")["status"]
    assert login["anonymous"] == "200"
    assert login["staff"].startswith("302->/dashboard")


def test_matrix_skips_static_and_post_by_default(matrix):
    assert not any(row["endpoint"] == "static" for row in matrix)
    assert not any(row["method"] == "POST" for row in matrix)


def test_matrix_uses_sample_ids_for_parameterised_routes(matrix):
    row = matrix_row(matrix, "/media/<path:relpath>")
    assert row["url"] == "/media/x"
    assert set(row["status"].values()) <= {"404"}


def test_matrix_post_methods(tmp_path):
    app = create_app("testing", UPLOAD_DIR=str(tmp_path))
    rows = build_matrix(app, methods=("POST",))
    row = matrix_row(rows, "/notifications/read", "POST")
    assert row["status"]["anonymous"].startswith("302->/auth/login")
    assert row["status"]["staff"].startswith("302")  # signed in: redirected back to the list


def test_matrix_formats(matrix):
    table = format_matrix(matrix)
    assert table.splitlines()[0].split()[:2] == ["METHOD", "ROUTE"]
    assert "/dashboard" in table
    parsed = json.loads(format_matrix(matrix, "json"))
    assert parsed[0]["status"]
    rows = list(csv.reader(io.StringIO(format_matrix(matrix, "csv"))))
    assert rows[0] == ["method", "rule", "endpoint", "anonymous", "admin", "hall_manager", "atelier_manager", "staff"]


def test_matrix_does_not_touch_the_real_database(runner, make_user):
    make_user("staff")
    before = db.session.execute(sa.select(sa.func.count(User.id))).scalar_one()
    result = runner.invoke(args=["routes-matrix"])
    assert result.exit_code == 0, result.output
    assert "/dashboard" in result.output
    db.session.expire_all()
    assert db.session.execute(sa.select(sa.func.count(User.id))).scalar_one() == before


def test_matrix_command_options(runner):
    result = runner.invoke(args=["routes-matrix", "--format", "json", "--methods", "GET,POST"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert {row["method"] for row in data} == {"GET", "POST"}


def test_matrix_command_without_seed_script_complains(runner, monkeypatch):
    monkeypatch.setattr("app.cli.load_seed_script", lambda path=None: None)
    result = runner.invoke(args=["routes-matrix", "--seed"])
    assert result.exit_code != 0 and "seed_demo.py" in result.output


def test_matrix_reports_crashing_views(app, tmp_path):
    other = create_app("testing", UPLOAD_DIR=str(tmp_path))

    @other.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    rows = build_matrix(other)
    assert any(status.startswith("EXC") or status.startswith("5") for status in matrix_row(rows, "/boom")["status"].values())
