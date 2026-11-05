# Small sharded SDK pipeline evidence

The exact reviewed frozen v4 pipeline completed once on 2026-11-05 using one
zero-GPU Oracle HOST sandbox, 4 CPU and 8 GiB RAM. It used 16 MiB of generated
BF16 source data across four shards. This measures the small SDK publication
pipeline; it does not measure full-model memory, native inference mapping, or
training-to-inference latency.

- Plan SHA-256: `35869f0d1e99e25ab8572a24b57dbc39e59b4e2e5b58547390ec948c7d5a013d`
- Phase: `32f0876a0e3a4641b6b3e579040fe46c`
- Sandbox: `c5677275-9cf0-493d-a998-bacf771d922a`
- Proof SHA-256: `8b3cfe60cda929d3f8c7f1f53a516ab35129370b26fc3151764af3c9865be4ac`
- Local artifacts: `chalk-private/.artifacts/weight-sync-p0/sharded-synthetic-frozen-v4/`

The phase started at 11:36:27 UTC with an unchanged absolute 11:46:27 deadline
and 120-second cleanup reserve. Controller and watchdog completed successfully
around 11:37:56. The 42.970-second measured publication/proof pipeline excludes
sandbox preparation, imports, quota probing and teardown. Fresh membership and
actual physical capacity checks passed before metadata creation and sandbox
submission; exact native cgroup limits were checked before and after proof.

## Byte proofs and measured observations

All 16 tensors, 8,388,608 elements and exactly 83,886 changed elements passed
trainer hash checks, whole-byte reconstruction, the independent scalar XOR
oracle, and fresh immutable reference verification across four remote shards.
Baseline, atomic patch payload/manifest/CURRENT, and independent reference each
used the actual existing SDK grouped Volume upload path. The patch snapshot was
2 and reference snapshot 1.

| Observation | Bytes |
| --- | ---: |
| Source BF16 tensor data | 16,777,216 |
| Verified nonsparse quota reservation, released before generation | 134,217,728 |
| Sampled peak owned logical files | 33,754,600 |
| Sampled peak owned allocated files | 33,760,768 |
| Conservative remote publication inputs | 33,716,942 |
| Reserved remote publication allowance | 50,331,648 |
| Native cgroup memory.current before / after | 548,691,968 / 633,860,096 |
| Native cgroup memory.peak before / after | 647,053,312 / 655,392,768 |
| Guest process ru_maxrss peak | 4,524,158,976 |

The native runtime enforced `memory.max=8589934592` and
`cpu.max=4000000 1000000`. All 20 controlled samples reported zero allocated
bytes in the owned HOME/cache/TMPDIR trees. Directory samples do not observe
continuous peaks or open/unlinked files, and statvfs changes do not provide
exclusive attribution. The guest ru_maxrss counter differs materially from the
native cgroup counter; both are preserved without assuming they measure the
same quantity. Guest-visible ancestor cgroups were unavailable, while exact
native runtime counters were available and validated.

These tiny-fixture observations do not establish a safe full-8B resource
request. The full-8B frozen package and its 120 GiB host-free-disk gate remain
unchanged. No full-model projection, GPU test, inference test, HF download or
AMD claim occurred in this phase.

## Release verification

The detached watchdog reported successful compute/data release with no cleanup
or monitoring errors. An early read-only independent verifier correctly refused
before the original compute create-reconciliation horizon; that refusal is
preserved in `independent-release-early-attempt.json`. No horizon was shortened.

After the horizon, the exact frozen verifier completed successfully. It observed
the exact sandbox API status TERMINATED, no matching CR/pod/retained-owner
resources, absent runsc/containerd task, both owned volumes absent, and no owned
controller/watchdog/caffeinate/cleanup process groups. The retained
`independent-release/` proof reports `all_owned_resources_absent=true`. The
controller and verifier exited before Oracle serialization was returned to the
parent for its separate verification.
