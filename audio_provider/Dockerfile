FROM python:3.11-slim-bookworm

ARG ASR_PROVIDER_MODEL=small

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models \
    ASR_PROVIDER_MODEL=${ASR_PROVIDER_MODEL} \
    ASR_PROVIDER_DEVICE=cpu \
    ASR_PROVIDER_COMPUTE_TYPE=int8 \
    ASR_PROVIDER_EAGER_LOAD=true

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
RUN mkdir -p /models \
    && python -m app.prefetch \
    && useradd -m -u 10001 appuser \
    && chown -R appuser:appuser /app /models

# A production container must use the model baked into the image and fail
# readiness rather than downloading mutable weights during startup.
ENV HF_HUB_OFFLINE=1

USER appuser
EXPOSE 8092

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8092"]
