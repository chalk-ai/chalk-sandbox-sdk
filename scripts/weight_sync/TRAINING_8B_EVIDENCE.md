# Qwen3-8B full-parameter training and live serving evidence

On 2026-11-05, the bounded `training-8b-live-r7` run passed correctness and independent resource cleanup using at most two A10080GB GPUs simultaneously and no AMD nodes. The fixed 45-minute budget began 08:37:15 UTC; all compute was released around 09:06 UTC and independent absence verification completed by 09:07:12 UTC. The deadline was never extended.

The CPU seed used `Qwen/Qwen3-8B` revision `b968826d9c46dd6066d109eabc6255188de91218`. Independent HF source bytes matched all five shards and 399 tensors / 8,190,735,360 elements. Seed plus proof took 301.43 seconds. The entrypoint ran once with durable proof and read-only progress polls; CPU compute was verified absent before the first GPU claim.

The trainer performed two full-parameter BF16 SGD steps on pinned GSM8K fixtures. Every parameter had a gradient. These aggressive steps validate synchronization correctness; probe loss increased from 1.42005 to 9.02268, so this run demonstrates no training-quality improvement.

| Publication | Actual changed elements | Actual changed fraction | Payload bytes | Publish time | Native apply | Admission pause | First observed response after publication |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 1,542,137,408 | 18.8278% | 4,651,465,108 | 84.24 s | 45.688 s | 45.688 s | 86.171 s |
| 2 | 857,810,763 | 10.4729% | 2,502,654,153 | 38.10 s | 21.817 s | 21.869 s | 86.787 s |

Both published versions were observed; no intermediate version was skipped. Publish-to-response values compare wall clocks on different hosts with unmeasured offset. Native apply/pause use local monotonic durations. The pauses materially miss the sub-second latency objective; this run is correctness evidence, not a latency success or a representative 1%-density benchmark. Per-phase attribution was not instrumented.

An independent reconstruction of version 2 matched every canonical trained tensor byte. Canonical state SHA256: `36ffb5a05b78b269aecd35910de215e276953abee4cba0777e18ac7d6d2d6ff9`. The live serving replica UID and native API/worker PID start times remained unchanged across both publications. A fresh engine then independently reconstructed source version 2 after trainer/live compute release and produced the identical greedy completion, `0000000000000000`. Its local audit stream intentionally started at version 0; its response header therefore identifies local version 0 rather than source version 2.

The original frozen launcher's disconnect gate checked only its first SSE data line. A supplementary token-aware probe on version 0 parsed nonempty generated text, closed without consuming EOF, and passed twenty healthy samples, follow-up inference, zero failure metrics, and unchanged replica/native identities. It proves at least one generated token, without an exact token count. The 64-token native completion might already have been buffered before physical close, so this does not independently establish that generation remained active at disconnect.

Independent cleanup checked both scaling-group API associations, CRs, immutable-owner containers/pods, the completed training run's CR and UID-owned pods, the active dataset catalog, all four owned volumes, and exact host runtime IDs. All were absent, with no cleanup errors. Both detached watchdogs and their owned sleep inhibitors exited.

Executed launcher commit: `6f9473f80a6d58da2a2eb5783c2408b360ebf6a8`. Runtime: `1546b67d`, plus the separately recorded shared-patch read backport in the integration checkout. Packaged trainer image: `ae7cf402af1c9ce3`; source overlay SHA256: `bdf6e34fb6fe1d72698bb4a67490f0a7d4eeb3fda82273b3cecc3025647f6f99`. The supplemental parser was executed separately before formatting and retained in `ffadd08f`; its retained source is not a byte-identical executed-source hash proof.

Full local artifacts were retained under `chalk-private/.artifacts/weight-sync-p0/training-8b-live-r7/`: launcher and trainer ledgers, visibility samples, native metrics, replica/process identities, supplemental disconnect evidence, executed-source provenance, and independent `cleanup-proof.json`.
