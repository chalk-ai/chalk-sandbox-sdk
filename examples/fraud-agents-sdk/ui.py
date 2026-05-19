"""Rich console rendering for the fraud-agent demo."""

import json

from agents.items import MessageOutputItem, ToolCallItem, ToolCallOutputItem
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


_NARRATION_NOISE_PREFIXES = (
    "now i", "i'll", "i will", "let me", "let's", "i'm going to",
    "next, i", "first, i", "going to ",
)


def _is_useful_narration(text: str) -> bool:
    t = text.strip()
    if not t or len(t.split()) > 40:
        return False
    lower = t.lower()
    return not any(lower.startswith(p) for p in _NARRATION_NOISE_PREFIXES)


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
        elif isinstance(item, MessageOutputItem):
            text = _extract_text(item)
            if _is_useful_narration(text):
                agent_thought(text)


def _extract_text(item: MessageOutputItem) -> str:
    raw = getattr(item, "raw_item", item)
    content = getattr(raw, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            t = getattr(c, "text", None) or (c.get("text") if isinstance(c, dict) else None)
            if t:
                parts.append(t)
        return " ".join(parts)
    return str(content or "")


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
