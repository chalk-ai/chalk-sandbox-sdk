# Content Moderation Pipeline

Image classification in an isolated Chalk container, orchestrated by Temporal with human-in-the-loop approval for borderline results.

## Prerequisites

- Chalk credentials in `.env` (run `just env` from the repo root)
- Temporal CLI: `brew install temporal`

## Quick start

### 1. Deploy the Chalk classify function

```bash
python classify.py
```

This deploys `classify_image` as a serverless function on Chalk. The container image includes `transformers`, `torch` (CPU), and `Pillow` — untrusted images are downloaded and classified entirely inside the sandbox.

### 2. Start Temporal

```bash
temporal server start-dev
```

Web UI available at http://localhost:8080.

### 3. Start the worker

```bash
python worker.py
```

### 4. Submit an image for moderation

```bash
python run.py https://example.com/some-image.jpg
```

If the model is confident, the image is auto-approved. If confidence is below 85%, the workflow pauses and waits for human review.

### 5. Approve or reject (human-in-the-loop)

```bash
python approve.py <workflow_id> approved
python approve.py <workflow_id> rejected
```

## Architecture

```
Temporal Server
  └─ ModerationWorkflow
       ├─ Activity: classify_image_activity
       │    └─ Calls Chalk RemoteFunction "classify_image"
       │         └─ Runs ML inference in isolated container
       ├─ Wait: workflow.wait_condition (human signal)
       └─ Returns: final decision + reviewer info
```

## Testing

Tests exercise the Chalk function directly (no Temporal server needed):

```bash
uv run python -m pytest test_content_moderation.py -v
```
