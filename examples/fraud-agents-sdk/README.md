# KYC Fraud Agent — OpenAI Agents SDK on Chalk Compute

An LLM agent (OpenAI Agents SDK) investigates a synthetic KYC packet by
shelling into a Chalk sandbox. The files stay in the sandbox; tool arguments,
command output, and the final verdict are sent to OpenAI.

This is an agent/tool-use demonstration, not a production KYC or fraud system.
Do not use its prompt, sample checks, or verdict as a compliance control.
The model controls a general shell in the sandbox, including its network
access. Run only the bundled synthetic cases unless you add an appropriate
network policy and review the data-handling implications.

## What it does

```
$ ./story.py case-fraud

[volume] kyc-case-fraud-a4f2b1 → uploading packet
[build] building image + starting investigator sandbox...

╭─────────────────────────────────────────────────╮
│ sandbox: kyc-investigator-a4f2b1                │
│ id:      sbx_…                                  │
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

[cleanup] terminating sandbox sbx_…
[cleanup] deleted volume kyc-case-fraud-a4f2b1
```

`case-legit` runs the same flow against a packet where everything agrees and
returns LOW.

## How it's wired

The agent gets **one** function tool: `bash`. Behind it sits a long-lived
Chalk sandbox with the case packet mounted from a `Volume` at
`/workspace`. The investigator image is a Debian slim with `tesseract`,
`exiftool`, `pdftotext`, `jq`, and python with `pillow` + `pypdf` +
`exifread` — the agent composes whatever commands it needs.

This is the lightweight alternative to implementing the OpenAI Agents SDK
`BaseSandboxClient` provider contract end-to-end.

## Layout

```
fraud-agents-sdk/
  story.py            # the demo: sandbox + Volume + agent + streaming loop
  ui.py               # rich console rendering (Panels, syntax-highlighted JSON, …)
  bash_tool.py        # formats results from the agent's shell tool
  samples/
    case-legit/       # application + ID + signed lease, all agree
    case-fraud/       # application + ID + utility bill, names disagree
```

## Prereqs

```
CHALK_CLIENT_ID
CHALK_CLIENT_SECRET
CHALK_ENVIRONMENT_ID
CHALK_API_SERVER
OPENAI_API_KEY
```

Export them, or put them in a `.env` in the directory where you run the
example (loaded via `python-dotenv`). Use synthetic or otherwise non-sensitive
case files: command output is sent to OpenAI.

First run takes a couple of minutes to build the investigator image (apt
install of `tesseract-ocr`, `libimage-exiftool-perl`, `poppler-utils`, `jq`).
Subsequent runs reuse the cached image.

Each run creates a fresh per-run volume and uploads the case packet — the
upload shows up in the terminal every time, which is what you want during a
demo.

The sandbox and per-run volume are deleted in `finally` blocks. The sandbox
also has a 10-minute maximum lifetime as a fallback if the local process is
terminated before cleanup completes.

## Adding a new fraud check

The image is the API. To give the agent a new capability:

1. Add the package / binary to the inline `Image` block in `story.py`
   (`apt-get install …` or `.pip_install([…])`).
2. Optionally mention it in the `SYSTEM_PROMPT`.

The agent discovers and composes; no code change in the runner.

## Swapping to a local model

The agent loop runs on the host where you invoke `story.py`, so the model
call is the only thing that round-trips outside your VPC. Point the Agents
SDK at a compatible OpenAI-style endpoint and override the model in
`story.py`:

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
