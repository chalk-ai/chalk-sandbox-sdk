# OpenCode on Chalk Containers

Run [OpenCode](https://opencode.ai/docs) as a persistent server inside an ephemeral Chalk container. Connect from your local terminal or browser.

## Prerequisites

- Python 3.12+ and [`uv`](https://github.com/astral-sh/uv) (or `pip install chalkcompute python-dotenv`)
- A `.env` at the repo root with your Chalk credentials:

  ```
  CHALK_CLIENT_ID=...
  CHALK_CLIENT_SECRET=...
  CHALK_API_SERVER=...
  CHALK_ENVIRONMENT_ID=...
  ```

- An `OPENCODE_SERVER_PASSWORD` for the server (any string):

  ```
  OPENCODE_SERVER_PASSWORD=your-secret-password
  ```

## Usage

```bash
# Basic — clones chalk-ai/chalk-sandbox-sdk@main, 12h timeout
python deploy.py

# Custom repo and timeout
python deploy.py --github-repo owner/repo --timeout 2h

# Private repo with a GitHub PAT
python deploy.py --github-repo owner/private-repo --github-token $(gh auth token)

# Short session
python deploy.py --timeout 30m
```

Once the container is running the script prints:

```
--- OpenCode server ready ---
  Web UI:  https://...
  TUI:     OPENCODE_SERVER_PASSWORD=<password> opencode attach https://...
-----------------------------
```

Press `Ctrl-C` to stop the container early.

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--timeout` | `12` | Lifetime: `2h`, `90m`, or bare number (hours) |
| `--name` | `opencode-server` | Container name |
| `--password` | `$OPENCODE_SERVER_PASSWORD` | OpenCode server password |
| `--github-repo` | `chalk-ai/chalk-sandbox-sdk` | Repo to clone into `/root/code` |
| `--github-ref` | `main` | Branch, tag, or SHA |
| `--github-token` | `$GITHUB_TOKEN` | PAT for private repos; grants `gh` CLI auth |
