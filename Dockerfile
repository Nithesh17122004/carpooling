FROM python:3.12-slim

WORKDIR /app

# Non-root runtime user (best practice; the app must never run as root).
RUN groupadd --system app && useradd --system --gid app --home /app app \
    && chown -R app:app /app

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt \
    && pip install --no-cache-dir gunicorn

COPY backend ./backend
COPY gunicorn.conf.py ./

ENV PYTHONUNBUFFERED=1 \
    FLASK_ENV=production \
    HOST=0.0.0.0 \
    PORT=5000

EXPOSE 5000

USER app

# HEALTHCHECK vs /api/health: reports live, never a hard dependency.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/health', timeout=4)"]

CMD ["gunicorn", "-c", "gunicorn.conf.py", "backend.wsgi:application"]