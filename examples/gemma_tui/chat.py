#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["requests", "rich"]
# ///
"""
Rich TUI to chat with Gemma 3 4B running on a Chalk ScalingGroup.

Usage:
    # With kubectl port-forward to the scaling group service:
    uv run python examples/gemma_tui/chat.py

    # Or point directly at the scaling group URL:
    GEMMA_URL=https://<env>-gemma-vllm.staging.internal.gcp.chalk.dev uv run python examples/gemma_tui/chat.py
"""

from __future__ import annotations

import json
import os

import requests
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

BASE_URL = os.environ.get(
    "GEMMA_URL",
    "http://localhost:18000",
)
MODEL = "google/gemma-3-4b-it"

console = Console()


def stream_completion(messages: list[dict[str, str]]) -> str:
    """Send a chat completion request and stream the response token-by-token."""
    collected = ""
    with requests.post(
        f"{BASE_URL}/v1/chat/completions",
        json={
            "model": MODEL,
            "messages": messages,
            "max_tokens": 1024,
            "stream": True,
        },
        stream=True,
        timeout=120,
    ) as resp:
        resp.raise_for_status()
        for raw_line in resp.iter_lines(decode_unicode=True):
            if raw_line is None or not raw_line.startswith("data: "):
                continue
            payload = raw_line[len("data: ") :]
            if payload.strip() == "[DONE]":
                break
            chunk = json.loads(payload)
            delta = chunk["choices"][0].get("delta", {})
            token = delta.get("content", "")
            if token:
                collected += token
                yield collected  # type: ignore[misc]
    return collected  # type: ignore[return-value]


def main() -> None:
    console.print(
        Panel(
            Text.from_markup(
                "[bold]Gemma 3 4B[/bold] on Chalk ScalingGroup\n"
                f"[dim]{BASE_URL}[/dim]\n\n"
                "[dim]Type your message and press Enter. Ctrl-C to quit.[/dim]"
            ),
            border_style="bright_blue",
        )
    )

    messages: list[dict[str, str]] = []

    while True:
        try:
            console.print()
            user_input = console.input("[bold green]You:[/bold green] ")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Goodbye![/dim]")
            break

        if not user_input.strip():
            continue

        messages.append({"role": "user", "content": user_input})

        console.print()
        final_text = ""
        try:
            with Live(
                Text("Thinking...", style="dim"),
                console=console,
                refresh_per_second=12,
                vertical_overflow="visible",
            ) as live:
                for partial in stream_completion(messages):
                    final_text = partial
                    live.update(
                        Panel(
                            Markdown(partial),
                            title="[bold magenta]Gemma[/bold magenta]",
                            border_style="magenta",
                        )
                    )
        except requests.HTTPError as e:
            console.print(
                f"[bold red]Error:[/bold red] {e.response.status_code} - {e.response.text}"
            )
            messages.pop()
            continue
        except requests.ConnectionError:
            console.print(
                "[bold red]Error:[/bold red] Cannot connect. Is the port-forward running?"
            )
            messages.pop()
            continue

        if final_text:
            messages.append({"role": "assistant", "content": final_text})


if __name__ == "__main__":
    main()
