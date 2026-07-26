# Self-hosted Video Semantic Evidence Provider

This isolated FastAPI service implements the existing
`video-semantic-evidence.v1` contract used by
`backend/app/services/video_evidence_analysis.py`.

It uses OpenCV (Apache-2.0) and NumPy (BSD-3-Clause), both suitable for
commercial deployment. It intentionally does not download model weights or
claim capabilities that it does not implement.

## Evidence boundary

- `subject_tracking`: tracks sustained, pixel-level moving regions across
  sampled frames. The label is always `moving_subject`; it is not a person,
  product, face, or object-class claim.
- `transition`: identifies abrupt visual discontinuities from adjacent sampled
  frame colour histograms. It does not distinguish editorial cuts from every
  possible lighting change.
- `pose`: opt-in. By default it reports `unsupported`. When
  `VIDEO_SEMANTIC_POSE_ENABLED=true` and the optional MediaPipe stack from
  `requirements-pose.txt` is installed, single-person pose keypoints are
  attached to validated subject tracks (a majority of confident keypoints must
  fall inside the track's observation bbox). If the dependency is missing or
  inference fails, pose reports `degraded` with empty evidence — keypoints are
  never fabricated. `VIDEO_SEMANTIC_POSE_MIN_CONFIDENCE` (default `0.25`)
  filters low-visibility keypoints.
- `action`: always `unsupported`. No action recognition model is bundled, so
  this service never fabricates action names.

No sustained moving region or abrupt discontinuity produces `partial` with a
clear reason and empty evidence, as required by the caller contract.

## Run

```bash
cd video_semantic_provider
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
VIDEO_SEMANTIC_PROVIDER_API_KEY=change-me \
  .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8091
```

Configure the API and reverse workers with the same optional bearer key:

```dotenv
VIDEO_EVIDENCE_SEMANTIC_URL=http://video_semantic_provider:8091/v1/video-semantic/analyze
VIDEO_EVIDENCE_SEMANTIC_API_KEY=change-me
VIDEO_EVIDENCE_SEMANTIC_HEALTH_URL=http://video_semantic_provider:8091/health
TRUSTED_ANALYZER_HOSTS=video_semantic_provider
```

Copy `.env.example` into the deployment's secret/configuration mechanism; do
not commit a real provider key.

The existing backend already bounds analysis requests to 64 frames and validates
every returned evidence reference, timestamp, geometry and confidence. The
repository Compose file exposes this service through the optional `evidence`
profile and keeps it on the private backend network.

## Configuration

| Variable | Default | Meaning |
|---|---:|---|
| `VIDEO_SEMANTIC_PROVIDER_API_KEY` | empty | When set, requires `Authorization: Bearer ...`. |
| `VIDEO_SEMANTIC_MAX_FRAMES` | 64 | Maximum accepted sampled frames, hard capped at 64. |
| `VIDEO_SEMANTIC_MAX_FRAME_BYTES` | 12 MiB | Per-frame encoded PNG limit. |
| `VIDEO_SEMANTIC_MAX_FRAME_PIXELS` | 24 MP | Per-frame decoded image limit. |
| `VIDEO_SEMANTIC_MIN_MOTION_AREA_RATIO` | 0.002 | Ignores tiny frame-difference noise. |
| `VIDEO_SEMANTIC_MAX_MOTION_AREA_RATIO` | 0.70 | Rejects near-full-frame changes as a subject. |
| `VIDEO_SEMANTIC_TRACK_IOU_THRESHOLD` | 0.12 | Association threshold for adjacent moving regions. |
| `VIDEO_SEMANTIC_TRANSITION_THRESHOLD` | 0.46 | Bhattacharyya histogram distance required for a discontinuity. |

## Verification

```bash
PYTHONPATH=. pytest -q tests/test_contract.py
docker build -t video-semantic-provider .
```

Health returns the exact metadata required by the existing adapter. A `200` health
response means the OpenCV runtime is available, not that pose/action recognition
is available; those two capability statuses remain `unsupported`.
