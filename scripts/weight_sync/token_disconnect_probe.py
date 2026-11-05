"""Supplementary routed disconnect proof after nonempty generated SSE text.

Reads existing resources only. Never changes launcher ownership or deadline.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen


def generated_text_event(lines):
    """Return first nonempty text event; chunks are not exact token counts."""
    data = []
    events = 0
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        line = line.rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif line == "" and data:
            raw, data = "\n".join(data), []
            if raw == "[DONE]":
                raise ValueError("SSE completed before nonempty generated text")
            event = json.loads(raw)
            events += 1
            texts = [
                choice["text"]
                for choice in event.get("choices", [])
                if isinstance(choice.get("text"), str) and choice["text"]
            ]
            if texts:
                # The prompt is a fixed public counting fixture; retain only
                # completion evidence, never request headers or credentials.
                return {
                    "generated_text": "".join(texts),
                    "generated_characters": sum(map(len, texts)),
                    "nonempty_text_chunks": 1,
                    "generated_token_count_lower_bound": 1,
                    "exact_generated_token_count": None,
                    "parsed_event_count": events,
                    "finish_reasons": [
                        c.get("finish_reason") for c in event["choices"]
                    ],
                    "usage": event.get("usage"),
                }
    raise ValueError("SSE ended before nonempty generated text")


def main():
    from ftqa_smoke import load_env
    from training_smoke import (
        ResourceGuard,
        native_processes,
        sample,
        serving_replica_identities,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    record = json.loads(args.ledger.read_text())
    if time.time() >= record["gpu_deadline_epoch"]:
        raise TimeoutError("Original GPU deadline has expired")
    load_env(args.env_file)
    import chalkcompute as cc

    guard = ResourceGuard(
        record["name"],
        args.ledger.parent,
        record,
        record["kube_context"],
        record["fallback_kube_context"],
    )
    guard.check_budget()
    group = cc.ScalingGroup.from_name(record["name"] + "-serve")
    evidence = {"started_at": time.time(), "phase": "checking_identity"}

    def save(**fields):
        evidence.update(fields)
        args.output.write_text(json.dumps(evidence, indent=2))

    before = serving_replica_identities(group, guard)
    processes_before = native_processes(before[0][2], args.ssh_host)
    save(replicas_before=before, native_processes_before=processes_before)
    group._client._ensure_auth()
    request = Request(
        group.web_url + "/v1/completions",
        data=json.dumps(
            {
                "model": "policy",
                "prompt": "Count from one to one hundred:",
                "temperature": 0,
                "max_tokens": 64,
                "ignore_eos": True,
                "stream": True,
            }
        ).encode(),
        headers={
            "Authorization": f"Bearer {group._client._access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        assert response.status == 200
        version = response.headers.get("x-chalk-policy-version")
        assert version is not None
        parsed = generated_text_event(response)
    save(
        stream_closed_at=time.time(),
        stream_policy_version=version,
        generated_completion=parsed,
        phase="checking_health",
    )
    health = []
    for _ in range(20):
        guard.check_budget()
        response = group.call("/chalk/health", method="GET", timeout=10)
        assert response.status == 200
        payload = json.loads(response.content)
        assert payload["healthy"] is True
        header = response.headers["x-chalk-weight-version"]
        assert str(payload["applied_version"]) == header
        health.append({"payload": payload, "version_header": header})
        time.sleep(0.5)
    after = serving_replica_identities(group, guard)
    processes_after = native_processes(after[0][2], args.ssh_host)
    assert before == after, "Serving replica restarted"
    assert processes_before == processes_after, "Native processes restarted"
    response = group.call("/chalk/metrics", method="GET", timeout=10)
    assert response.status == 200
    metrics = {
        line.split()[0]: float(line.split()[1])
        for line in response.content.decode().splitlines()
        if line and not line.startswith("#")
    }
    assert metrics["chalk_weight_sync_failures"] == 0
    save(
        phase="passed",
        completed_at=time.time(),
        replicas_after=after,
        native_processes_after=processes_after,
        healthy_samples=health,
        metrics=metrics,
        next_request=sample(group),
    )
    print(json.dumps(evidence))


if __name__ == "__main__":
    main()
