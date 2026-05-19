from __future__ import annotations
import json
import logging
import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

from anthropic import Anthropic

logging.basicConfig(level=logging.INFO, format="[worker-%(name)s] %(levelname)s: %(message)s")
WORKER_ID = os.environ.get("WORKER_ID", "0")
log = logging.getLogger(WORKER_ID)

if "CHALK_ACTIVE_ENVIRONMENT" not in os.environ and "CHALK_ENVIRONMENT_ID" in os.environ:
    os.environ["CHALK_ACTIVE_ENVIRONMENT"] = os.environ["CHALK_ENVIRONMENT_ID"]
elif "CHALK_ENVIRONMENT_ID" not in os.environ and "CHALK_ACTIVE_ENVIRONMENT" in os.environ:
    os.environ["CHALK_ENVIRONMENT_ID"] = os.environ["CHALK_ACTIVE_ENVIRONMENT"]

WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace/project"))
SEARCH_BRANCH = os.environ.get("SEARCH_BRANCH", f"rule-searcher-{WORKER_ID}")
PROMOTE_BRANCH = os.environ.get("PROMOTE_BRANCH", "rule-promising")
PROMOTIONS_DIR = Path(os.environ.get("PROMOTIONS_DIR", "/data/promotions"))
PROMOTIONS_LOG = PROMOTIONS_DIR / f"worker-{WORKER_ID}.jsonl"
DONE_MARKER = PROMOTIONS_DIR / f"worker-{WORKER_ID}.done"
TRACE_LOG = Path(os.environ.get("TRACE_LOG", f"/data/traces/worker-{WORKER_ID}.jsonl"))
MAX_OUTPUT_CHARS = 8000
MODEL = "claude-sonnet-4-6"


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 600) -> tuple[int, str, str]:
    log.info("$ %s", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def _trace(event: dict[str, Any]) -> None:
    """Append a structured event to the per-worker trace JSONL on the shared volume.

    Survives container exit, so the hub (and humans) can inspect what the agent
    actually did even when `chalk logs` is unhelpful.
    """
    try:
        TRACE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with TRACE_LOG.open("a") as f:
            f.write(json.dumps({"worker_id": WORKER_ID, **event}, default=str) + "\n")
    except OSError as e:
        log.warning("trace write failed: %s", e)


# ## Tool implementations (same shape as the single-container agent.py)

def tool_list_project_files(_: dict[str, Any]) -> str:
    files = sorted(
        str(p.relative_to(WORKSPACE))
        for p in WORKSPACE.rglob("*.py")
        if ".venv" not in p.parts and "site-packages" not in p.parts
    )
    return "\n".join(files[:200]) or "(no .py files found)"


def tool_read_project_file(args: dict[str, Any]) -> str:
    path = WORKSPACE / args["path"]
    try:
        resolved = path.resolve()
        resolved.relative_to(WORKSPACE.resolve())
    except (ValueError, OSError) as e:
        return f"refused: {e}"
    try:
        return resolved.read_text()[:20_000]
    except OSError as e:
        return f"error: {e}"


def tool_write_candidate_rule(args: dict[str, Any]) -> str:
    filename = args["filename"]
    if "/" in filename or filename.startswith("."):
        return "filename must be a flat name like candidate_high_value.py"
    rules_dir = WORKSPACE / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    target = rules_dir / filename
    target.write_text(args["contents"])
    return f"wrote {target.relative_to(WORKSPACE)}"


def tool_chalk_apply_search(_: dict[str, Any]) -> str:
    rc, out, err = _run(["chalk", "apply", "--branch", SEARCH_BRANCH], cwd=WORKSPACE, timeout=300)
    return f"exit={rc}\n--- stdout (tail) ---\n{out[-MAX_OUTPUT_CHARS:]}\n--- stderr (tail) ---\n{err[-2000:]}"


def tool_chalk_query_search(args: dict[str, Any]) -> str:
    feature: str = args["feature"]
    input_feature: str = args["input_feature"]
    values: list[Any] = list(args.get("input_values") or [])[:25]

    rows: list[dict[str, Any]] = []
    for value in values:
        rc, out, err = _run(
            [
                "chalk", "query",
                "--branch", SEARCH_BRANCH,
                "--in", f"{input_feature}={value}",
                "--out", feature,
                "--json",
            ],
            cwd=WORKSPACE,
            timeout=60,
        )
        if rc == 0:
            try:
                parsed: Any = json.loads(out)
            except json.JSONDecodeError:
                parsed = {"raw": out[:1000]}
            rows.append({"in": value, "out": parsed})
        else:
            rows.append({"in": value, "error": err.strip()[:300] or out.strip()[:300]})

    truthy = sum(
        1 for r in rows
        if "out" in r and _looks_truthy(_extract_value(r["out"], feature))
    )
    errors = sum(1 for r in rows if "error" in r)
    payload = {"summary": {"n": len(rows), "truthy": truthy, "errors": errors}, "rows": rows}
    return json.dumps(payload, default=str, indent=2)[:MAX_OUTPUT_CHARS]


def _extract_value(parsed: Any, feature: str) -> Any:
    if isinstance(parsed, dict) and isinstance(parsed.get("data"), list):
        for row in parsed["data"]:
            if isinstance(row, dict) and row.get("field") == feature:
                return row.get("value")
    return parsed


def _looks_truthy(v: Any) -> bool:
    if v is None or v is False:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() not in {"", "false", "0", "null", "none"}
    return True


def tool_promote_rule(args: dict[str, Any]) -> str:
    rc, out, err = _run(["chalk", "apply", "--branch", PROMOTE_BRANCH], cwd=WORKSPACE, timeout=300)
    entry = {
        "event": "rule.promoted",
        "worker_id": WORKER_ID,
        "feature": args["feature"],
        "rule_file": args["rule_file"],
        "metric": args["metric"],
        "reasoning": args["reasoning"],
        "promote_branch": PROMOTE_BRANCH,
        "search_branch": SEARCH_BRANCH,
        "apply_exit": rc,
    }
    log.info("PROMOTED %s", json.dumps(entry))
    PROMOTIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PROMOTIONS_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return f"promoted={json.dumps(entry)}\napply stdout tail:\n{out[-2000:]}\napply stderr tail:\n{err[-1000:]}"


TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_project_files",
        "description": "List Python source files in the checked-out Chalk project (up to 200, relative paths). Use this first.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "read_project_file",
        "description": "Read a project file's contents (relative to project root, capped at 20KB).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_candidate_rule",
        "description": "Create a new Python file under `rules/` containing a candidate rule (derived feature + resolver). Use a flat filename like candidate_high_value.py.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "contents": {"type": "string"},
            },
            "required": ["filename", "contents"],
        },
    },
    {
        "name": "chalk_apply_search",
        "description": f"Run `chalk apply --branch {SEARCH_BRANCH}` from the project root. Returns exit code and output tail.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "chalk_query_search",
        "description": (
            f"Resolve a single feature for a list of primary-key values on the {SEARCH_BRANCH} branch. "
            "Returns a summary with truthy/errors counts plus per-row details."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "feature": {"type": "string"},
                "input_feature": {"type": "string"},
                "input_values": {
                    "type": "array",
                    "items": {"type": ["string", "number"]},
                    "description": "Up to 25 PK values",
                },
            },
            "required": ["feature", "input_feature", "input_values"],
        },
    },
    {
        "name": "promote_rule",
        "description": (
            f"Mark a candidate rule as promising. Re-applies on `{PROMOTE_BRANCH}` and appends a JSONL entry. "
            "Skip rules with truthy=0%, truthy=100%, or any errors."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "feature": {"type": "string"},
                "rule_file": {"type": "string"},
                "metric": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["feature", "rule_file", "metric", "reasoning"],
        },
    },
]

DISPATCH = {
    "list_project_files": tool_list_project_files,
    "read_project_file": tool_read_project_file,
    "write_candidate_rule": tool_write_candidate_rule,
    "chalk_apply_search": tool_chalk_apply_search,
    "chalk_query_search": tool_chalk_query_search,
    "promote_rule": tool_promote_rule,
}


# ## Bootstrap

def _bootstrap_project() -> None:
    if WORKSPACE.exists() and any(WORKSPACE.iterdir()):
        log.info("workspace %s already populated; skipping clone", WORKSPACE)
        return

    WORKSPACE.parent.mkdir(parents=True, exist_ok=True)
    repo = os.environ.get("PROJECT_REPO")
    ref = os.environ.get("PROJECT_REF", "main")
    if repo:
        token = os.environ.get("GITHUB_TOKEN")
        url = (
            f"https://oauth2:{token}@github.com/{repo}.git" if token
            else f"https://github.com/{repo}.git"
        )
        rc, out, err = _run(
            ["git", "clone", "--depth", "1", "--branch", ref, url, str(WORKSPACE)],
            timeout=300,
        )
        if rc != 0:
            raise RuntimeError(f"git clone failed (exit {rc}): {err.strip() or out.strip()}")
        return

    log.info("PROJECT_REPO unset; trying `chalk source`")
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    rc, out, err = _run(["chalk", "source"], cwd=WORKSPACE, timeout=300)
    if rc != 0:
        raise RuntimeError(f"chalk source failed (exit {rc}): {err.strip() or out.strip()}")


_AGENT_PROMPT_CANDIDATES = ("CLAUDE.md", "AGENTS.md", "agent-prompt.md", "ANTHROPIC.md")


def _generate_agent_prompt() -> str:
    rc, out, err = _run(
        ["chalk", "init", "agent-prompt", "claude"],
        cwd=WORKSPACE,
        timeout=120,
    )
    if rc != 0:
        log.warning(
            "`chalk init agent-prompt claude` failed (exit %d): %s",
            rc, (err.strip() or out.strip())[:500],
        )
        return ""

    for candidate in _AGENT_PROMPT_CANDIDATES:
        path = WORKSPACE / candidate
        if path.is_file():
            text = path.read_text()
            log.info("loaded agent prompt %s (%d bytes)", candidate, len(text))
            return text
    log.warning("`chalk init agent-prompt claude` produced no recognized file")
    return ""


SYSTEM_PROMPT = (
    "You are a rule-discovery agent operating on a Chalk feature-engineering project. "
    "You are worker {worker_id} in a parallel fleet — focus on your assigned area and "
    "do not duplicate work your peers might do.\n\n"
    "Job: read existing feature/resolver definitions, propose 1-2 candidate boolean *rules* "
    "(derived features) for a downstream rules engine.\n\n"
    "MANDATORY workflow — do every step, in order. Do not stop early.\n"
    "  1. list_project_files, then read_project_file on 2-3 relevant files in your focus area.\n"
    "  2. For each candidate, write_candidate_rule with a flat filename like "
    "`candidate_<worker_id>_<short_name>.py`. Wire into the existing namespace.\n"
    "  3. chalk_apply_search. If it fails, fix the rule file and retry, but cap apply attempts "
    "at 3. If it still fails after 3 tries, stop — produce a final message and exit.\n"
    "  4. AFTER apply succeeds, you MUST immediately call chalk_query_search ONCE PER candidate "
    "feature with the sample inputs from the user prompt. Do not stop after apply success — "
    "querying is the whole point. Read the truthy/errors counts from each query result.\n"
    "  5. A rule is *promising* when truthy is 1-99% of samples (i.e. it varies across inputs) "
    "and errors=0. Call promote_rule for promising ones with a one-line metric like "
    "`truthy=4/10`. If NO rule is promising, say so in your final message and stop.\n\n"
    "Prefer precision over recall. Don't promote rules with apply/query errors. After running "
    "all queries and promotions, produce a one-line final summary like "
    "`promoted: 1/3, skipped: 2/3 (always-true)` and stop."
)


def run() -> None:
    try:
        _bootstrap_project()
        project_overview = _generate_agent_prompt()

        sample_inputs = os.environ.get("SAMPLE_INPUTS", "1,2,3,4,5,6,7,8,9,10")
        pk_feature = os.environ.get("PRIMARY_KEY_FEATURE", "user.id")
        max_turns = int(os.environ.get("MAX_TURNS", "16"))
        focus = os.environ.get("WORKER_FOCUS", "")

        project_section = (
            f"\n## Project overview (from `chalk init agent-prompt claude`)\n\n{project_overview}\n"
            if project_overview else ""
        )
        focus_section = f"\n## Your focus area\n{focus}\n" if focus else ""

        user_prompt = (
            f"You are worker {WORKER_ID}. Project is at {WORKSPACE}."
            f"{focus_section}"
            f"{project_section}\n"
            f"When calling chalk_query_search:\n"
            f"  input_feature = {pk_feature!r}\n"
            f"  input_values  = [{sample_inputs}]\n\n"
            f"Apply on {SEARCH_BRANCH!r}. Promote to {PROMOTE_BRANCH!r}."
        )
        log.info("agent prompt:\n%s", user_prompt)

        client = Anthropic()
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
        system = SYSTEM_PROMPT.format(worker_id=WORKER_ID)

        for turn in range(max_turns):
            log.info("Turn %d", turn)
            resp = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=system,
                tools=TOOLS,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    text = block.text.strip()
                    if text:
                        log.info("  assistant: %s", text.splitlines()[0][:300])
                        _trace({"turn": turn, "kind": "text", "text": text[:2000]})
                elif getattr(block, "type", None) == "tool_use":
                    log.info("  tool_call:  %s(%s)", block.name, str(block.input)[:200])
                    _trace({"turn": turn, "kind": "tool_call", "tool": block.name, "input": dict(block.input)})

            if resp.stop_reason != "tool_use":
                log.info("stop_reason=%s; done after %d turn(s)", resp.stop_reason, turn + 1)
                _trace({"turn": turn, "kind": "stop", "reason": resp.stop_reason})
                break

            tool_results: list[dict[str, Any]] = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                handler = DISPATCH.get(block.name)
                if handler is None:
                    result_text = f"unknown tool {block.name}"
                    is_error = True
                else:
                    try:
                        result_text = handler(dict(block.input))
                        is_error = False
                    except Exception as e:  # noqa: BLE001
                        result_text = f"{type(e).__name__}: {e}"
                        is_error = True
                log.info(
                    "  tool_result(%s): %s",
                    block.name,
                    result_text.splitlines()[0][:300] if result_text else "",
                )
                _trace({
                    "turn": turn,
                    "kind": "tool_result",
                    "tool": block.name,
                    "is_error": is_error,
                    "result": result_text[:4000],
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text[:MAX_OUTPUT_CHARS],
                    "is_error": is_error,
                })

            messages.append({"role": "user", "content": tool_results})
        else:
            log.info("hit max_turns=%d", max_turns)
            _trace({"kind": "max_turns", "max_turns": max_turns})

    except Exception:  # noqa: BLE001
        log.error("worker failed:\n%s", traceback.format_exc())
        _write_done_marker(status="failed")
        sys.exit(1)

    _write_done_marker(status="ok")


def _write_done_marker(status: str) -> None:
    PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)
    DONE_MARKER.write_text(json.dumps({"worker_id": WORKER_ID, "status": status}))
    log.info("wrote done marker %s (status=%s)", DONE_MARKER, status)


if __name__ == "__main__":
    run()
