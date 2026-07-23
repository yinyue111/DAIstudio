FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TORCH_HOME=/models \
    EVIDENCE_PROVIDER_DEVICE=cpu \
    EVIDENCE_PROVIDER_EAGER_LOAD=true

WORKDIR /app

COPY requirements.txt requirements-models.txt ./
RUN pip install --index-url https://download.pytorch.org/whl/cpu -r requirements-models.txt \
    && pip install -r requirements.txt

COPY app ./app
RUN mkdir -p /models \
    && python -m app.prefetch \
    && useradd -m -u 10001 appuser \
    && chown -R appuser:appuser /app /models

USER appuser
EXPOSE 8090

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
