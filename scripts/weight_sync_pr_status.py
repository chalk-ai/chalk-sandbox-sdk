#!/usr/bin/env python3
"""Inspect current PR heads and the latest attempt for every check.

Example: python scripts/weight_sync_pr_status.py --repo chalk-ai/chalk-sandbox-sdk 586 587 588
Requires an authenticated gh CLI. This read-only command never prints auth data.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


def latest_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for check in checks:
        workflow = check.get("workflowName") or ""
        name = check.get("name") or check.get("context") or "unnamed"
        key = (workflow, name)
        timestamp = check.get("startedAt") or check.get("createdAt") or ""
        previous = latest.get(key)
        if previous is None or timestamp >= (
            previous.get("startedAt") or previous.get("createdAt") or ""
        ):
            latest[key] = check
    return [latest[key] for key in sorted(latest)]


def check_state(check: dict[str, Any]) -> str:
    if check.get("__typename") == "StatusContext":
        return check.get("state") or "UNKNOWN"
    status = check.get("status") or "UNKNOWN"
    if status == "COMPLETED":
        return check.get("conclusion") or "UNKNOWN"
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub OWNER/REPO")
    parser.add_argument("prs", type=int, nargs="+", help="Pull request numbers")
    parser.add_argument("--json", action="store_true", help="Emit compact JSON")
    args = parser.parse_args()
    results = []
    failed = False
    for number in args.prs:
        process = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                args.repo,
                "--json",
                "number,url,title,state,isDraft,headRefOid,statusCheckRollup",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode:
            # gh stderr can include local host/auth details. Keep errors compact.
            print(
                f"{args.repo}#{number}: gh pr view failed (exit {process.returncode})",
                file=sys.stderr,
            )
            failed = True
            continue
        try:
            pull = json.loads(process.stdout)
        except json.JSONDecodeError:
            print(f"{args.repo}#{number}: gh returned invalid JSON", file=sys.stderr)
            failed = True
            continue
        checks = [
            {
                "workflow": check.get("workflowName") or "",
                "name": check.get("name") or check.get("context") or "unnamed",
                "state": check_state(check),
                "url": check.get("detailsUrl") or check.get("targetUrl") or "",
            }
            for check in latest_checks(pull.get("statusCheckRollup") or [])
        ]
        result = {
            "repo": args.repo,
            "number": pull["number"],
            "url": pull["url"],
            "title": pull["title"],
            "state": pull["state"],
            "draft": pull["isDraft"],
            "head": pull["headRefOid"],
            "checks": checks,
        }
        results.append(result)
        if not args.json:
            print(
                f"{args.repo}#{number} {result['state']}{' DRAFT' if result['draft'] else ''} {result['head'][:12]} {result['title']}"
            )
            if not checks:
                print("  NO_CHECKS")
            for check in checks:
                label = f"{check['workflow']}/" if check["workflow"] else ""
                print(f"  {check['state']:<18} {label}{check['name']}")
    if args.json:
        print(json.dumps(results, separators=(",", ":")))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
