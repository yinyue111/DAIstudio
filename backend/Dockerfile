FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Minimal system deps (libpq for psycopg, curl for healthcheck, ffmpeg for
# sampling keyframes from reference videos in the reverse-prompt step).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt
# Dynamic-page scraping (XHS and other lazy-loaded sites) needs Chromium inside
# the container; installed to a shared path so the non-root runtime user can use it.
RUN python -m playwright install --with-deps chromium

# Run as a non-root user. The named `media` volume mounted at /app/storage
# inherits this ownership on first creation (Docker copies image dir perms).
RUN useradd -m -u 10001 appuser

COPY . .
RUN mkdir -p /app/storage && chown -R appuser:appuser /app /ms-playwright

USER appuser
EXPOSE 8000
# API service overrides this to run migrations first (see docker-compose.yml).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
