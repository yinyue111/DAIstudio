FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Minimal system deps (libpq for psycopg, curl for healthcheck, git + ssh for
# optional admin online updates, ffmpeg for sampling keyframes/audio evidence,
# and Tesseract with Simplified Chinese data for independent image/frame OCR).
ARG DEBIAN_MIRROR=https://deb.debian.org
RUN sed -i "s|http://deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
    build-essential libpq-dev curl git openssh-client ffmpeg fonts-noto-cjk \
    tesseract-ocr tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --index-url "$PIP_INDEX_URL" -r requirements.txt
# Dynamic-page scraping (XHS and other lazy-loaded sites) needs Chromium inside
# the container; installed to a shared path so the non-root runtime user can use it.
# Keep OS packages and the browser download in separate layers so a transient
# download failure can reuse the dependency layer on retry.
RUN python -m playwright install-deps chromium
ARG PLAYWRIGHT_DOWNLOAD_HOST
RUN python -m playwright install chromium

# Run as a non-root user. The named `media` volume mounted at /app/storage
# inherits this ownership on first creation (Docker copies image dir perms).
RUN useradd -m -u 10001 appuser

COPY . .
RUN mkdir -p /app/storage && chown -R appuser:appuser /app /ms-playwright

USER appuser
EXPOSE 8000
# API service overrides this to run migrations first (see docker-compose.yml).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
