from __future__ import annotations
from types import SimpleNamespace
import pytest
from tests.test_sglang_resident_sparse import install_module, workers as workers

torch = pytest.importorskip("torch")


def test_full_offline_driver_sequence_with_real_codec_and_cpu_resident_writes(
    workers, monkeypatch, tmp_path
):
    import json
    import time
    from scripts.sglang_sparse import driver, native

    monkeypatch.setattr(torch.cuda, "synchronize", lambda _: None)
    install_module(
        monkeypatch,
        "sglang.srt.managers.io_struct",
        PauseGenerationReqInput=lambda **kw: SimpleNamespace(**kw),
        ContinueGenerationReqInput=lambda: SimpleNamespace(),
    )
    schedulers = []
    for worker in workers:
        worker.gpu_id = worker.rank
        scheduler = SimpleNamespace(
            tp_worker=worker,
            draft_worker=None,
            enable_overlap=False,
            _engine_paused=False,
            is_fully_idle=lambda: True,
            flush_cache=lambda **kw: True,
        )
        scheduler.pause_generation = lambda req, scheduler=scheduler: setattr(
            scheduler, "_engine_paused", True
        )
        scheduler.continue_generation = lambda req, scheduler=scheduler: setattr(
            scheduler, "_engine_paused", False
        )
        worker.model_runner = SimpleNamespace(model=SimpleNamespace())
        schedulers.append(scheduler)

    class Engine:
        def collective_rpc(self, method, *, call_id, operation, payload_json):
            assert method == "ws_sparse_rpc"
            root = tmp_path / "receipts"
            root.mkdir(exist_ok=True)
            for rank, scheduler in enumerate(schedulers):
                envelope = dict(
                    schema_version=1, call_id=call_id, method=operation, rank=rank
                )
                try:
                    envelope.update(
                        ok=True,
                        result=native._operation(
                            scheduler, operation, json.loads(payload_json)
                        ),
                    )
                except Exception as error:
                    envelope.update(
                        ok=False, error_type=type(error).__name__, error=str(error)
                    )
                (root / f"{call_id}.{rank}.json").write_text(json.dumps(envelope))

        def generate(self, *args, **kwargs):
            assert all(not item._engine_paused for item in schedulers)
            return {
                "text": "ok",
                "meta_info": {
                    "output_token_logprobs": [[-0.5, 0, "x"] for _ in range(8)],
                    "output_top_logprobs": [
                        [[-float(i + 1), i, str(i)] for i in range(5)] for _ in range(8)
                    ],
                },
            }

    result = driver.run(driver.Proof(Engine(), tmp_path, time.time() + 3600))
    assert result["byte_control_passed"] and result["generation_passed"]
    assert result["full_smoke_accepted"] is False
    assert all(worker._ws_resident_version == 2 for worker in workers)
    assert all(
        torch.equal(value, torch.ones_like(value))
        for worker in workers
        for value in worker.targets.values()
    )


def test_generation_gate_keeps_exact_tokens_topkeys_and_fixed_tolerance():
    import copy
    from scripts.sglang_sparse.driver import compare_generation, validate_generation

    value = {
        "text": "x",
        "meta_info": {
            "output_token_logprobs": [[-0.5, 0, "x"] for _ in range(8)],
            "output_top_logprobs": [
                [[-float(i + 1), i, str(i)] for i in range(5)] for _ in range(8)
            ],
        },
    }
    validate_generation(value)
    other = copy.deepcopy(value)
    other["meta_info"]["output_token_logprobs"][0][0] += 0.01
    assert not compare_generation([value], [other])["passed"]
    other = copy.deepcopy(value)
    other["meta_info"]["output_top_logprobs"][0][0][1] = 99
    assert not compare_generation([value], [other])["passed"]
    other["meta_info"]["output_token_logprobs"][0][0] = float("nan")
    with pytest.raises(ValueError):
        validate_generation(other)
