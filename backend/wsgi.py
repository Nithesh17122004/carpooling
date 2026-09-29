"""WSGI entry point for production servers (gunicorn, waitress, uwsgi).

    gunicorn -b 0.0.0.0:5000 --timeout 60 backend.wsgi:application
"""

from .app import create_app

application = create_app()