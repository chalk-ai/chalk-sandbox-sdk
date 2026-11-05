# Disposable weight sync validation

These opt-in scripts claim one FTQA A100 80GB, run real ChalkFS/vLLM 0.33 checks,
copy lightweight evidence locally, terminate the sandbox and verify its terminal
status, then delete and verify removal of the unique stream volume. They do not
run from imports or ordinary tests. The sandbox also has a one-hour maximum lifetime.

The source checkout must integrate both the publisher and serving feature branches.
This scripts PR deliberately adds no publisher implementation. For the recorded
8B run, the tested integration revision was `6caddc4f`; the exact Python source
archive and SHA256 are preserved alongside its evidence. Later runs should use
the latest integrated feature revisions rather than treating that pin as production.

```sh
PYTHONPATH=/path/to/integrated-sdk python scripts/weight_sync/ftqa_smoke.py \
  --source-root /path/to/integrated-sdk --env-file /path/to/ftqa.env \
  --model 8b --out /path/to/new-evidence-directory
```

Use an environment with the SDK, codec dependencies and valid FTQA credentials.
No model artifacts download to the workstation. The remote install preserves the
image's existing torch/vLLM versions through pip constraints. The script currently
runs as root and applies `chmod 1777 /dev/shm`, pending the independent hypervisor fix.

The check seeds a pinned HF checkpoint, loads native vLLM, changes one final norm
tensor densely, restores it, changes one norm element sparsely, verifies worker
tensor bytes, restores it, exercises a backward PIN and unpin, and compares the
sparse output with a fresh engine loaded from an independently materialized checkpoint.

Recorded Qwen3-8B/A100 result: all checks passed, including exact greedy output
equivalence for a sparse version. The sparse payload was **83 bytes**, changing
one of 8,190,735,360 model elements. Apply/pause was 104.18/104.24 ms for that
update and 15.35/15.40 ms for its restore. Publisher scans took 2.7–3.6 seconds
per publication. Backward PIN required a full 16.38 GB keyframe replay and paused
admissions for 8.892 seconds. These are correctness checks on a tiny changed
tensor, not evidence of representative model-wide 1% delta performance.

The recorded integration had an older `density` metric denominator (changed
tensor elements). Use `full_model_density` from its sparse phase for global
density. Current serving density counts transmitted elements across the whole
model; dense fallback is an upper bound on actual changed elements. Older
`publish_to_visible_s` evidence included byte auditing and metadata reads; new
runs stop this timer immediately after the first versioned inference response.

Always inspect `launcher.json` and require both `sandbox_released` and
`volume_deleted` to be true, even after failure. If the launcher is interrupted,
recover the exact recorded sandbox ID and stream name and release only those
owned resources. Do not infer GPU cleanup merely from remote driver completion.
