"""Rich console rendering for the fraud-agent demo. Story-named helpers
(`volume_reused`, `tool_call`, `verdict`, …) so `story.py` reads like prose
and aesthetics live entirely in this file."""

import json

from agents.items import ToolCallItem, ToolCallOutputItem
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

console = Console()

DASHBOARD_BASE = (
    "https://staging.chalk.dev/projects/tmnmau5shs3qe"
    "/environments/tmnmc9beyujew/containers"
)


def _truncate(s, n):
    s = str(s)
    return s if len(s) <= n else s[:n] + f"  …[+{len(s) - n} chars]"


def volume_reused(name):
    console.print(f"[bold cyan]\\[volume][/] [green]{name}[/] already exists [dim]→ reusing (skip upload)[/]")


def volume_creating(name):
    console.print(f"[bold cyan]\\[volume][/] [green]{name}[/] missing [dim]→ creating + uploading packet[/]")


def volume_added(name, size):
    console.print(f"         [dim]- {name} ({size} bytes)[/]")


def build_starting():
    console.print("[bold cyan]\\[build][/] building image + starting investigator container...")


def container_started(container):
    cid = container._container_id or "(unknown)"
    short = cid.split("-")[0] if "-" in cid else cid[:8]
    url = f"{DASHBOARD_BASE}/{cid}"
    console.print(
        f"[bold cyan]\\[ready][/]  [bold]{container._name}[/]  "
        f"[dim]→[/] [link={url}]dashboard ({short}…)[/]"
    )


def agent_starting():
    console.print("[bold cyan]\\[agent][/] [bold]investigation starting[/]")


def _format_args(raw):
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return _truncate(raw, 200)
    else:
        parsed = raw
    if isinstance(parsed, dict) and len(parsed) == 1:
        (k, v) = next(iter(parsed.items()))
        return f"{k}={v!r}" if not isinstance(v, str) else f"{k}={_truncate(v, 200)!r}"
    return _truncate(json.dumps(parsed), 200)


def tool_call(turn, item):
    raw = item.raw_item
    name = getattr(raw, "name", None) or "?"
    args = getattr(raw, "arguments", None) or "{}"
    console.print(
        f"\n[bold magenta]\\[{turn:02d}][/] [bold cyan]{name}[/]"
        f"[dim]([/][white]{_format_args(args)}[/][dim])[/]"
    )


def _try_json(chunk):
    stripped = chunk.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        return None


def tool_output(item):
    """Render a tool result for the viewer.

    JSON blobs get syntax-highlighted — they're small, structured, the eye
    parses them instantly. Plaintext (OCR dumps, PDF text, exit-only output)
    collapses to a single dim line: the *signals* the agent extracts are
    what surface in the Verdict, not the raw bytes.
    """
    output = item.output if isinstance(item.output, str) else str(item.output)
    chunks = [c for c in output.split("\n\n") if c.strip()]
    if not chunks:
        return
    for chunk in chunks:
        parsed = _try_json(chunk)
        if parsed is not None:
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
            console.print(Syntax(pretty, "json", theme="ansi_dark", background_color="default", word_wrap=True, padding=(0, 4)))
        else:
            stripped = chunk.strip()
            if stripped.startswith("[exit "):
                continue
            lines = stripped.splitlines()
            # Short output (ls, exit codes, one-line greps) is signal — show
            # verbatim. Long output (OCR dumps, PDF text) is noise to the viewer
            # — collapse to a one-line preview. The agent's Verdict signals
            # surface what actually mattered.
            if len(lines) <= 5 and len(stripped) <= 200:
                for line in lines:
                    console.print(f"    [dim]{line}[/]", highlight=False)
            else:
                preview = lines[0][:80] if lines else ""
                console.print(
                    f"    [dim]→ {len(lines)} lines  "
                    f"[italic]\"{preview}…\"[/][/]"
                )


def verdict(v):
    risk = v.risk.upper()
    color = {"LOW": "green", "MEDIUM": "yellow", "HIGH": "red"}.get(risk, "white")
    title = Text.assemble(
        ("VERDICT: ", "bold"),
        (risk, f"bold {color}"),
        ("   confidence=", "dim"),
        (f"{v.confidence:.2f}", "bold"),
    )
    body = Text()
    body.append("Reasoning\n", style="bold")
    body.append(f"  {v.reasoning}\n", style="white")
    if v.signals:
        body.append("\nSignals\n", style="bold")
        for s in v.signals:
            body.append(f"  • {s}\n", style="white")
    console.print()
    console.print(Panel(body, title=title, border_style=color, padding=(1, 2)))
    console.print()


async def stream(result):
    """Consume an Agents SDK streamed run, rendering call → output pairs.

    The agent can emit multiple parallel ToolCallItems in one turn before any
    ToolCallOutputItem fires; buffer them and pair on output so the viewer
    sees call → output, call → output, not 3 calls then 3 outputs.
    """
    pending: list = []
    turn = 0
    async for event in result.stream_events():
        if event.type != "run_item_stream_event":
            continue
        item = event.item
        if isinstance(item, ToolCallItem):
            pending.append(item)
        elif isinstance(item, ToolCallOutputItem):
            if pending:
                turn += 1
                tool_call(turn, pending.pop(0))
            tool_output(item)


def container_stopping():
    console.print("[bold cyan]\\[stop][/]  stopping container")


def container_kept(container):
    """Used by demo_tools/keep.py — leaves container alive for inspection."""
    cid = container._container_id
    url = f"{DASHBOARD_BASE}/{cid}"
    console.print("[bold yellow]\\[keep][/]  leaving container running for inspection")
    console.print(f"        [dim]dashboard:[/] [link={url}]{url}[/]")
    console.print(f"        [dim]stop with:[/]  [bold]uv run python -m demo_tools.stop {cid}[/]")
