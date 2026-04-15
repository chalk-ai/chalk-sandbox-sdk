# Content Moderation Pipeline

Image classification in an isolated Chalk container, orchestrated by Temporal with human-in-the-loop approval for borderline results.

## Prerequisites

- Chalk credentials in `.env` (run `just env` from the repo root)
- Temporal CLI: `brew install temporal`

## Quick start

### 1. Deploy the Chalk classify function

```bash
set -a && source ../../.env && set +a
uv run python classify.py
```

This deploys `classify-image` as a serverless function on Chalk. The container image includes `transformers`, `torch` (CPU), and `Pillow` — untrusted images are downloaded and classified entirely inside the sandbox.

### 2. Start Temporal

```bash
temporal server start-dev
```

Web UI available at http://localhost:8233.

### 3. Start the worker

```bash
set -a && source ../../.env && set +a
uv run python worker.py
```

### 4. Submit an image for moderation

```bash
uv run python run.py https://images.pexels.com/photos/45201/kitty-cat-kitten-pet-45201.jpeg
```

If the model is confident (>= 85%), the image is auto-approved. If confidence is below 85%, the workflow pauses and waits for human review.

### 5. Approve or reject (human-in-the-loop)

**Option A — Temporal web UI:**

1. Open http://localhost:8233
2. Click on your workflow (e.g. `moderation-abc123`)
3. Click **Signal** (top-right)
4. Signal name: `review`
5. Input: `"approved"` or `"rejected"`
6. Click Send

**Option B — CLI:**

```bash
uv run python approve.py <workflow_id> approved
uv run python approve.py <workflow_id> rejected
```

## Architecture

```
Temporal Server
  └─ ModerationWorkflow
       ├─ Activity: classify_image_activity
       │    └─ Calls Chalk RemoteFunction "classify-image"
       │         └─ Runs ML inference in isolated container
       ├─ Wait: workflow.wait_condition (human signal)
       └─ Returns: final decision + reviewer info
```

## Testing

Tests exercise the Chalk function directly (no Temporal server needed):

```bash
uv run python -m pytest test_content_moderation.py -v
```
