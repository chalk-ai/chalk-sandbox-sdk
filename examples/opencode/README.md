# OpenCode on Chalk Containers

Run [OpenCode](https://opencode.ai/docs) as a persistent server inside an ephemeral Chalk container. Connect from your local terminal or browser.

## Prerequisites

- Python 3.12+ with `chalkcompute` and `python-dotenv` installed
- A `.env` at the repo root with your Chalk credentials:

  ```
  CHALK_CLIENT_ID=...
  CHALK_CLIENT_SECRET=...
  CHALK_API_SERVER=...
  CHALK_ENVIRONMENT_ID=...
  ```

## Usage

Use `deploy_exec.py` — see the script docstring for all flags and env vars:

```bash
python examples/opencode/deploy_exec.py --help
```

Once running, the script prints a Web UI URL and a TUI attach command. Press `Ctrl-C` to stop the container.

> **Note:** `deploy.py` (image-build based) is currently not supported and should not be used.
