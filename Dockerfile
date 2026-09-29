FROM python:3.12-slim

WORKDIR /app

# Create non-root runtime user
RUN groupadd --system app \
    && useradd --system --gid app --home /app app

# Install dependencies
COPY backend/requirements.txt ./backend/requirements.txt

RUN pip install --no-cache-dir -r backend/requirements.txt \
    && pip install --no-cache-dir gunicorn

# Copy backend and frontend
COPY backend ./backend
COPY frontend ./frontend
COPY gunicorn.conf.py ./

# Create writable directories
RUN mkdir -p /app/backend/uploads \
    && chown -R app:app /app/backend \
    && chown -R app:app /app/frontend \
    && chown -R app:app /app

ENV PYTHONUNBUFFERED=1 \
    FLASK_ENV=production \
    HOST=0.0.0.0

EXPOSE 10000

USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; port=os.environ.get('PORT', '10000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=4)"]

CMD ["gunicorn", "-c", "gunicorn.conf.py", "backend.wsgi:application"]
