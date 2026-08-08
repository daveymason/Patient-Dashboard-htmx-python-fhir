"""WSGI entry point.

    gunicorn 'wsgi:app'

Kept separate from app.py so importing the application module has no side
effects -- tests build their own instance with create_app(TestConfig).
"""
from app import create_app

app = create_app()
