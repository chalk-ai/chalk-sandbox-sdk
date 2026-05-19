"""Rich console rendering for the fraud-agent demo."""

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


def volume_creating(name):
    console.print(f"[bold cyan]\\[volume][/] [green]{name}[/] [dim]→ uploading packet[/]")


def volume_added(name, size):
    console.print(f"         [dim]- {name} ({size} bytes)[/]")


def build_starting():
    console.print("[bold cyan]\\[build][/] building image + starting investigator container...")


def container_started(container):
    cid = container._container_id or "(unknown)"
    short = cid.split("-")[0] if "-" in cid else cid[:8]
    url = f"{DASHBOARD_BASE}/{cid}"
    console.print(
        f"[bold cyan]\\[ready][/] [bold]{container._name}[/] "
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
    args_raw = getattr(raw, "arguments", None) or "{}"
    try:
        parsed = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
    except (ValueError, TypeError):
        parsed = {}
    thought = parsed.pop("thought", None) if isinstance(parsed, dict) else None
    if thought:
        agent_thought(thought)
    console.print(
        f"[bold magenta]\\[{turn:02d}][/] [bold cyan]{name}[/]"
        f"[dim]([/][white]{_format_args(parsed)}[/][dim])[/]"
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
    output = item.output if isinstance(item.output, str) else str(item.output)
    body = output.rstrip()
    lines_all = body.splitlines()
    if lines_all and lines_all[-1].strip().startswith("[exit "):
        body = "\n".join(lines_all[:-1]).rstrip()
    if not body.strip():
        return
    parsed = _try_json(body)
    if parsed is not None:
        pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
        console.print(Syntax(pretty, "json", theme="ansi_dark", background_color="default", word_wrap=True, padding=(0, 4)))
        return
    lines = body.splitlines()
    if len(lines) <= 5 and len(body) <= 200:
        for line in lines:
            console.print(f"    [dim]{line}[/]", highlight=False)
    else:
        first = next((l for l in lines if l.strip()), "")[:80]
        console.print(f"    [dim]→ {len(lines)} lines: [italic]\"{first}…\"[/][/]")


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


def agent_thought(text: str) -> None:
    console.print(f"\n[bold yellow]\\[agent][/] [italic]{text.strip()}[/]")


async def stream(result):
    # Parallel ToolCallItems may all fire before any ToolCallOutputItem;
    # buffer them and pair on output so the viewer sees call→output pairs.
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


def container_kept(container):
    cid = container._container_id
    url = f"{DASHBOARD_BASE}/{cid}"
    stop_cmd = (
        f"""uv run python -c "from dotenv import load_dotenv; load_dotenv(); """
        f"""from chalkcompute import Container; Container.from_id('{cid}').stop()" """
    ).strip()
    console.print(f"\n[bold yellow]\\[kept][/] container left running for inspection")
    console.print(f"        [dim]dashboard:[/] [link={url}]{url}[/]")
    console.print(f"        [dim]stop with:[/] [bold]{stop_cmd}[/]")
