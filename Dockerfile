FROM python:3.12-slim

WORKDIR /app

# Create non-root runtime user
RUN groupadd --system app \
    && useradd --system --gid app --home /app app

# Install dependencies
COPY backend/requirements.txt ./backend/requirements.txt

RUN pip install --no-cache-dir -r backend/requirements.txt \
    && pip install --no-cache-dir gunicorn

# Copy application
COPY backend ./backend
COPY gunicorn.conf.py ./

# Create directories that the application needs to write to
RUN mkdir -p /app/backend/uploads \
    && chown -R app:app /app/backend \
    && chown -R app:app /app

ENV PYTHONUNBUFFERED=1 \
    FLASK_ENV=production \
    HOST=0.0.0.0

# Render provides PORT automatically.
# Do not hard-code PORT=5000 here.

EXPOSE 10000

USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:10000/api/health', timeout=4)"]

CMD ["gunicorn", "-c", "gunicorn.conf.py", "backend.wsgi:application"]
