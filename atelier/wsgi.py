"""WSGI / Flask CLI entry point.

    flask --app wsgi init-db
    flask --app wsgi create-admin --mobile 09121234567 --name "Ali"
    flask --app wsgi run --port 5000
    gunicorn wsgi:app            # production: set ATELIER_ENV=production and SECRET_KEY first
"""

from app import create_app

app = create_app()
