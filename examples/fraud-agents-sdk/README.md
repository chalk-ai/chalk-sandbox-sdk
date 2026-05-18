# KYC Fraud Agent — OpenAI Agents SDK on Chalk Compute

An LLM agent (OpenAI Agents SDK) investigates a KYC packet by shell-ing into a
Chalk Compute container running in your cloud. ID images and utility bills
stay inside the sandbox; only shell commands and text outputs round-trip to
OpenAI.

## What it does

```
$ uv run python story.py case_fraud

[volume] kyc-case-case-fraud already exists → reusing (skip upload)
[build] building image + starting investigator container...

╭─────────────────────────────────────────────────╮
│ container: kyc-investigator-a4f2b1              │
│ id:        cnt_…                                │
│ dashboard: https://staging.chalk.dev/…          │
╰─────────────────────────────────────────────────╯

[agent] investigation starting

[01] bash(command='ls -la /workspace')
[02] bash(command='cat /workspace/application.json')
[03] bash(command='tesseract /workspace/id.jpeg - --psm 6')
[04] bash(command='pdftotext /workspace/utility_bill.pdf -')
…

╭─ VERDICT: HIGH   confidence=0.92 ─────────────────╮
│ Reasoning                                         │
│   The ID OCR's to "John Smith / 1990-08-04" while │
│   application.json claims "Jane Doe / 1985-03-12".│
│   Utility bill is addressed to John Smith too.    │
│ Signals                                           │
│   • id.jpeg name "John Smith" ≠ application name  │
│   • dob mismatch across application and ID        │
│   • utility bill account holder is John Smith     │
╰───────────────────────────────────────────────────╯

[stop]  stopping container
```

`case_legit` runs the same flow against a packet where everything agrees and
returns LOW.

## How it's wired

The agent gets **one** function tool: `bash`. Behind it sits a long-lived
`chalkcompute.Container` with the case packet mounted from a `Volume` at
`/workspace`. The investigator image is a Debian slim with `tesseract`,
`exiftool`, `pdftotext`, `jq`, and python with `pillow` + `pypdf` +
`exifread` — the agent composes whatever commands it needs.

The lighter alternative to implementing the OpenAI Agents SDK
`BaseSandboxClient` provider contract end-to-end. Visually identical to the
viewer; ~300 LOC across `story.py` + `ui.py`.

## Layout

```
fraud-agents-sdk/
  story.py            # the demo: Container + Volume + agent + streaming loop
  ui.py               # rich console rendering (Panels, syntax-highlighted JSON, …)
  bash_tool.py        # the agent's one @function_tool, bound to a Container
  samples/
    case_legit/       # all three documents agree
    case_fraud/       # application + ID + bill name three ways apart
  demo_tools/         # auxiliary CLIs, kept out of story.py
    keep.py           #   run but leave container alive
    refresh.py        #   re-upload case packet after editing samples/
    stop.py           #   stop a kept-alive container by ID
```

## Prereqs

```
CHALK_CLIENT_ID
CHALK_CLIENT_SECRET
CHALK_ENVIRONMENT_ID
CHALK_API_SERVER
OPENAI_API_KEY
```

Drop them in a `.env` next to `story.py` (loaded via `python-dotenv`).

## Running

```bash
uv sync
uv run python story.py case_fraud
uv run python story.py case_legit
```

First run takes a couple of minutes to build the investigator image (apt
install of `tesseract-ocr`, `libimage-exiftool-perl`, `poppler-utils`, `jq`).
Subsequent runs reuse the cached image.

The per-case Volume is persistent — `story.py` skips upload when it already
exists. After editing files in `samples/`, force a re-upload:

```bash
uv run python story.py case_fraud --refresh
```

To leave the container alive afterward for dashboard poking:

```bash
uv run python story.py case_fraud --keep
# … inspect in the dashboard …
uv run python -m demo_tools.stop <container_id>
```

Both flags delegate to `demo_tools/` — the behavior lives there, story.py
just dispatches.

## Adding a new fraud check

The image is the API. To give the agent a new capability:

1. Add the package / binary to the inline `Image` block in `story.py`
   (`apt-get install …` or `.pip_install([…])`).
2. Optionally mention it in the `SYSTEM_PROMPT`.

The agent discovers and composes; no code change in the runner.

## Swapping to a local model

The agent loop runs on the host where you invoke `story.py`, so the model
call is the only thing that round-trips outside your VPC. Point the Agents
SDK at a vLLM scaling group (see `examples/vllm-gemma/`) and override the
model in `story.py`:

```python
from openai import AsyncOpenAI
from agents import OpenAIChatCompletionsModel

agent = Agent(
    ...,
    model=OpenAIChatCompletionsModel(
        model="Qwen/Qwen2.5-7B-Instruct",
        openai_client=AsyncOpenAI(base_url="https://your-vllm-endpoint/v1",
                                  api_key="not-used"),
    ),
)
```

## Out of scope

- Real KYC accuracy. The showcase is the agent's investigative *loop*, not
  state-of-the-art ID verification.
- GPU inference. Image-tampering and face-match models would slot into the
  image the same way `tesseract` does.
- A true `BaseSandboxClient` provider port for Chalk Compute.
