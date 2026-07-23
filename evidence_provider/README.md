# Self-hosted image evidence provider

This optional service implements the platform's strict `region-analyzer.v1`
contract with TorchVision's pretrained CPU models:

- `SSDLite320 MobileNet V3` for COCO object detection.
- `LRASPP MobileNet V3` for VOC semantic segmentation.

It is isolated from the API/worker image so model dependencies do not increase
the normal web or Celery runtime. No evidence is returned unless the selected
model actually runs.

## Docker

```bash
docker compose --profile evidence up -d --build evidence_provider
```

Configure the backend API and reverse worker with the same optional key:

```dotenv
IMAGE_EVIDENCE_DETECTOR_URL=http://evidence_provider:8090/v1/region/analyze
IMAGE_EVIDENCE_DETECTOR_API_KEY=<same value as EVIDENCE_PROVIDER_API_KEY>
IMAGE_EVIDENCE_DETECTOR_HEALTH_URL=http://evidence_provider:8090/health/detector
IMAGE_EVIDENCE_SEGMENTER_URL=http://evidence_provider:8090/v1/region/analyze
IMAGE_EVIDENCE_SEGMENTER_API_KEY=<same value as EVIDENCE_PROVIDER_API_KEY>
IMAGE_EVIDENCE_SEGMENTER_HEALTH_URL=http://evidence_provider:8090/health/segmenter
TRUSTED_ANALYZER_HOSTS=evidence_provider
```

The Compose service is behind the `evidence` profile and is not enabled by the
default stack. Its published port is bound to localhost through the isolated
operator probe network; API and worker containers reach it on the internal
backend network.

## Local development

Use Python 3.11 and install model dependencies separately:

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-models.txt
TORCH_HOME=.torch .venv/bin/python -m app.prefetch
TORCH_HOME=.torch .venv/bin/uvicorn app.main:app --port 8090
```

Health is not considered ready until the actual model weights have loaded.
Weights are prefetched into the image and are not shadowed by a runtime volume,
so an image version and its analyzer weights are promoted and rolled back
together.
