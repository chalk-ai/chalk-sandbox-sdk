#!/usr/bin/env python3
"""Read-only public-route oracle check against a separately captured fresh checkpoint.

Does not provision compute, publish weights, alter PIN, or prove native identities.
Run after every publication in the release qualification recipe.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


MAX_BYTES = 16 * 1024**2
TOLERANCE = 1e-3


def compare(actual: dict, reference: dict) -> dict:
    choices, expected = actual["choices"], reference["choices"]
    if len(choices) != 1 or len(expected) != 1:
        raise ValueError("Exactly one completion required")
    actual_choice, expected_choice = choices[0], expected[0]
    if actual_choice["text"] != expected_choice["text"]:
        raise ValueError("Greedy text differs from fresh checkpoint")
    observed, oracle = actual_choice["logprobs"], expected_choice["logprobs"]
    if not observed["tokens"] or observed["tokens"] != oracle["tokens"]:
        raise ValueError("Generated token strings differ from fresh checkpoint")
    records, refs = observed["top_logprobs"], oracle["top_logprobs"]
    if len(records) != len(observed["tokens"]) or len(refs) != len(records):
        raise ValueError("Incomplete top-five logprob trace")
    maximum = 0.0
    for record, ref in zip(records, refs):
        if not isinstance(record, dict) or len(record) != 5 or set(record) != set(ref):
            raise ValueError("Top-five token membership differs")
        for token, value in record.items():
            if not math.isfinite(value) or not math.isfinite(ref[token]):
                raise ValueError("Nonfinite logprob")
            maximum = max(maximum, abs(value - ref[token]))
    if maximum > TOLERANCE:
        raise ValueError("Top-five logprobs exceed fixed 1e-3 tolerance")
    return {"tokens": len(observed["tokens"]), "maximum_logprob_error": maximum}


def read_json(path: Path) -> dict:
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("JSON exceeds bound")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("JSON object required")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", required=True, help="Supervisor URL, never native control port"
    )
    parser.add_argument("--expected-version", required=True, type=int)
    parser.add_argument("--request-json", required=True, type=Path)
    parser.add_argument("--reference-json", required=True, type=Path)
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="New evidence file; never overwritten",
    )
    args = parser.parse_args()
    url = urlsplit(args.url)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/")
    ):
        parser.error("Use a credential-free HTTP(S) origin")
    if args.expected_version < 0:
        parser.error("Version must be nonnegative")
    body = read_json(args.request_json)
    if (
        body.get("temperature") != 0
        or body.get("logprobs") != 5
        or body.get("stream", False) is not False
        or body.get("n", 1) != 1
    ):
        parser.error("Request must use temperature=0, logprobs=5, stream=false, n=1")
    reference = read_json(args.reference_json)
    headers = {"Content-Type": "application/json"}
    bearer = os.environ.get("CHALK_WEIGHT_TEST_BEARER")
    if bearer:
        headers["Authorization"] = "Bearer " + bearer

    opener = build_opener(NoRedirect)

    def fetch(path: str, payload: dict | None = None) -> tuple[dict, str | None]:
        request = Request(
            args.url.rstrip("/") + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers=headers,
        )
        with opener.open(request, timeout=120) as response:
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("HTTP response exceeds bound")
            return json.loads(raw), response.headers.get("x-chalk-policy-version")

    # Exclusive creation preserves failed attempts and avoids overwriting evidence.
    with args.output.open("x") as output:
        report: dict = {
            "passed": False,
            "version": args.expected_version,
            "scope": "public HTTP oracle only",
            "tolerance": TOLERANCE,
        }
        try:
            for phase in ("before", "after"):
                health, _ = fetch("/chalk/health")
                if (
                    health.get("healthy") is not True
                    or type(health.get("applied_version")) is not int
                    or health["applied_version"] != args.expected_version
                ):
                    raise ValueError("Supervisor version/readiness differs")
                if phase == "before":
                    actual, stamp = fetch("/v1/completions", body)
                    report["actual"] = actual
                    if stamp != str(args.expected_version):
                        raise ValueError("Inference response version stamp differs")
                    report.update(compare(actual, reference))
            report["passed"] = True
        except Exception as error:
            report["error_type"] = type(error).__name__
            raise
        finally:
            json.dump(report, output, indent=2)
            output.write("\n")
    print("Public-route oracle check passed")


if __name__ == "__main__":
    main()
