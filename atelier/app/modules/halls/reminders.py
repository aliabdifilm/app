"""``flask --app wsgi halls-agreement-reminders``: tell both sides before an agreement ends.

Run it once a day from cron.  Idempotent: every agreement is reminded once per end date
(``HallAgreement.reminded_at``; changing the end date makes it eligible again).
"""

from __future__ import annotations

import click
from flask import Flask
from flask.cli import with_appcontext

from app.extensions import db
from app.modules.halls import rules


def register_cli(app: Flask) -> None:
    @app.cli.command("halls-agreement-reminders")
    @click.option("--days", default=14, show_default=True, type=click.IntRange(1, 120), help="remind this many days before the end")
    @with_appcontext
    def halls_agreement_reminders(days: int) -> None:
        """Notify the hall and atelier managers of agreements that end soon."""
        count = rules.remind_agreements(days)
        db.session.commit()
        click.echo(f"{count} agreement reminder(s) sent.")
