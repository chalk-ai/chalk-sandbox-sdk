#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute", "pyarrow>=14"]
# ///
"""Evaluate an LLM's top ten suppliers with NDCG@10 on three mock purchases.

Requires an SDK with scorers.ndcg, Chalk credentials for the target environment,
and an environment secret CHALK_ROUTER_API_KEY holding a Chalk gateway key.
CHALK_API_SERVER selects the API host; SUPPLIER_MODEL optionally selects a model.
The fictional catalog and item descriptions are sent to that model through Chalk.
The relevance grades are illustrative labels, never included in the LLM prompt.

The suite, dataset, evaluation, and results remain available for inspection.
The task and scorer remain deployed for reruns and may consume billable capacity;
each run also incurs three model calls. Function names are intentionally reused.
"""

import json
import os
from uuid import uuid4

import chalkcompute as cc


CATALOG = {
    "boltworks": "Stainless-steel bolts and nuts; bulk industrial orders.",
    "precision": "Certified high-tolerance stainless-steel fasteners.",
    "hardware": "General hardware including steel bolts and tools.",
    "industrial": "Industrial supplies: fasteners, gloves, and packaging.",
    "mediglove": "Medical-grade powder-free nitrile examination gloves.",
    "safewear": "Nitrile work gloves and protective equipment.",
    "clinic": "Clinical consumables including examination gloves.",
    "boxcraft": "Recycled corrugated shipping boxes in bulk.",
    "packright": "Corrugated cartons, packing tape, and mailers.",
    "officesupply": "Office supplies, small cartons, and stationery.",
    "freshfoods": "Fresh produce and catering ingredients.",
    "cloudsoft": "Software subscriptions and cloud consulting.",
}


@cc.function(
    image=cc.Image.debian_slim().pip_install(["openai"]),
    secrets=[cc.Secret.from_chalk_env("CHALK_ROUTER_API_KEY")],
    env={
        "OPENAI_BASE_URL": os.environ.get(
            "CHALK_API_SERVER", "https://api.chalk.ai"
        ).rstrip("/")
        + "/v1/router",
        "SUPPLIER_MODEL": os.environ.get("SUPPLIER_MODEL", "openai/gpt-4.1-mini"),
    },
)
def supplier_ranking(item_description: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["CHALK_ROUTER_API_KEY"])
    response = client.chat.completions.create(
        model=os.environ["SUPPLIER_MODEL"],
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": "Produce 10 suppliers for this item description, ranked "
                "best fit first. Choose exactly 10 distinct IDs from this catalog. "
                'Return only JSON: {"suppliers": ["id", ...]}.\n' + json.dumps(CATALOG),
            },
            {"role": "user", "content": item_description},
        ],
    )
    suppliers = json.loads(response.choices[0].message.content or "")["suppliers"]
    if (
        not isinstance(suppliers, list)
        or len(suppliers) != 10
        or any(not isinstance(item, str) or item not in CATALOG for item in suppliers)
        or len(set(suppliers)) != 10
    ):
        raise ValueError("Expected 10 distinct supplier IDs from the catalog")
    return json.dumps(suppliers)


supplier_ndcg = cc.scorers.ndcg(k=10, name="supplier-ndcg-at-10")


if __name__ == "__main__":
    purchases = [
        (
            "500 stainless-steel bolts for industrial machinery",
            {"boltworks": 3, "precision": 3, "hardware": 2, "industrial": 1},
        ),
        (
            "1,000 medical-grade powder-free nitrile examination gloves",
            {"mediglove": 3, "clinic": 2, "safewear": 1, "industrial": 1},
        ),
        (
            "2,000 recycled corrugated shipping boxes",
            {"boxcraft": 3, "packright": 2, "officesupply": 1, "industrial": 1},
        ),
    ]
    # Unique dataset/suite names keep separate executions available for comparison.
    name = f"supplier-ranking-{uuid4().hex[:8]}"
    datasets = cc.DatasetClient()
    dataset = datasets.upload(
        name,
        [
            {"item_description": item, "relevance": json.dumps(grades)}
            for item, grades in purchases
        ],
    )
    suite = cc.EvaluationSuite.create(name)
    evaluation = cc.Evaluation.create(
        name,
        suite_id=suite.id,
        dataset=dataset,
        task=supplier_ranking,
        scorers=[supplier_ndcg],
    )
    run = evaluation.run()
    print(f"Suite: {suite.id}\nEvaluation: {evaluation.id}\nRun: {run.id}", flush=True)
    run = run.wait()
    print(datasets.read(run.result_dataset).to_pylist())
