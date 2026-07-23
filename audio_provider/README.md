# Self-hosted ASR provider

This optional service exposes the OpenAI-compatible
`POST /v1/audio/transcriptions` endpoint used by
`backend/app/services/video_audio.py`. It runs the local
[`faster-whisper`](https://github.com/SYSTRAN/faster-whisper) implementation
of OpenAI Whisper models. The code and Whisper model weights are commercially
deployable under their respective MIT licenses; pin a model revision internally
if your organization needs reproducible supply-chain approvals.

It returns real model-produced segment timestamps (`start`, `end`, `text`). It
does not implement speaker diarization: every health and transcription response
reports `speaker: unsupported`, and no `speaker_id` is ever inferred.

## Contract

`GET /health` returns the exact backend health contract:

```json
{
  "contract_version": "audio-evidence.v1",
  "ok": true,
  "status": "ready",
  "provider_model": "small",
  "capability_statuses": {"asr": "available", "speaker": "unsupported"}
}
```

`POST /v1/audio/transcriptions` accepts multipart OpenAI fields including
`file`, `model`, `response_format`, `timestamp_granularities[]`, `language`,
and `prompt`. The configured model name must match the supplied `model` field.
It always returns segment timestamps, including when a generic `json` response
format is requested, because the platform rejects transcript-only evidence.

## Build and run

The Docker build downloads the configured Whisper model once and fails rather
than producing a ready image without model weights.
The prefetched model remains inside the immutable image instead of a named
runtime volume, so changing the build model cannot silently reuse stale weights.

```bash
docker build -t ai-audio-provider ./audio_provider
docker run --rm -p 127.0.0.1:8092:8092 \
  -e ASR_PROVIDER_API_KEY=replace-with-a-long-secret \
  -e ASR_PROVIDER_MODEL=small \
  ai-audio-provider
```

To bake another model into the image, use
`--build-arg ASR_PROVIDER_MODEL=large-v3`. The final image sets
`HF_HUB_OFFLINE=1`, so startup cannot silently fetch mutable model files.

Configure the existing backend/worker deployment to use the service over its
private network. Do not publish this port to the internet.

```dotenv
AUDIO_GATEWAY_ENABLED=true
AUDIO_GATEWAY_BASE_URL=http://audio_provider:8092
AUDIO_GATEWAY_API_KEY=replace-with-a-long-secret
AUDIO_TRANSCRIPTION_MODEL=small
AUDIO_GATEWAY_HEALTH_URL=http://audio_provider:8092/health
TRUSTED_ANALYZER_HOSTS=audio_provider
```

The repository Compose file exposes this service through the optional
`evidence` profile. The port is bound to localhost for operator probes while
API and reverse workers call it over `backend_internal`.

The service defaults to CPU `int8` inference. For a CUDA deployment, provide a
compatible CTranslate2 runtime and set `ASR_PROVIDER_DEVICE=cuda` and an
appropriate `ASR_PROVIDER_COMPUTE_TYPE` such as `float16`.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `ASR_PROVIDER_API_KEY` | empty | Optional bearer credential. Set in every non-local deployment. |
| `ASR_PROVIDER_MODEL` | `small` | Local Faster-Whisper model name/path; must match backend model config. |
| `ASR_PROVIDER_DEVICE` | `cpu` | `cpu`, `cuda`, or `auto`. |
| `ASR_PROVIDER_COMPUTE_TYPE` | `int8` | CTranslate2 compute mode. |
| `ASR_PROVIDER_EAGER_LOAD` | `true` | Load model before readiness is reported. |
| `ASR_PROVIDER_MAX_UPLOAD_BYTES` | `50331648` | Strict multipart audio size cap. |
| `ASR_PROVIDER_MAX_SEGMENTS` | `1000` | Maximum retained model segments. |
| `ASR_PROVIDER_BEAM_SIZE` | `5` | Faster-Whisper decoding beam size. |

Uploaded media is written to a unique temporary file only for inference and is
deleted in a `finally` block. The API does not log audio bytes, transcript text,
prompts, or authorization values.

## Test

```bash
python -m pip install -r audio_provider/requirements-dev.txt
python -m pytest -q audio_provider/tests
```

The contract suite injects a local fake inference engine. It verifies the
actual multipart wire contract, timestamp segment response shape, health
readiness, auth, model binding, and the explicit no-diarization guarantee
without downloading a model or calling a paid service.
