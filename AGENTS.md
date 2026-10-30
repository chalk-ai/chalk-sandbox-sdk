# Chalk Sandbox SDK

## Project tracking

Linear project: "Sandbox Devx/capabilities" — https://linear.app/chalk-ai/project/sandbox-devxcapabilities-1ddd7f00a4e7/overview
- Linear team: **Engineering** (key: CHA)
- Linear project ID: `8a522950-da1f-424e-bc6d-8f63e0110fcf`
- Initiative: "Sandbox Compute"

## Architecture

- `container.py` — High-level `Container(image=Image(...)).run().exec()` backed by `ContainerService`
- `sandbox.py` — Low-level `SandboxClient` / `Sandbox` backed by `SandboxService` (gRPC bidirectional streaming exec)
- `image.py` — Declarative image builder with lazy local file support (`add_local_file`, `add_local_dir`)
- `volume.py` — Volume SDK client backed by `VolumeService`

### Shared spec fields — change every surface

Several concepts (`env`, `secrets`, `volumes`, `cpu`/`memory`/`gpu`, image
config) are accepted by **multiple** entry points: functions (`_function.py`),
classes (`_cls.py`), containers (`_container.py`), scaling groups
(`_scaling_group.py`), sandboxes (`_sandbox.py` — including per-`exec` calls),
and images (`_image.py`). When you add validation, normalization, or behavior
for one of these, apply it to **all** of them — ideally via a shared helper in
a leaf module like `_naming.py` (e.g. `validate_env`) so the logic can't drift.
Grep for the field name across `chalkcompute/` before assuming one call site is
the only one.

## SDK Examples

When adding or updating SDK examples in the `examples/` directory, follow `examples/style_guide.md`.

## Function and class configuration

Start with `@cc.function` or `@cc.cls` and add only options required by the task.
An optional parameter in the signature is not a recommendation to set it. Check
the installed SDK's defaults and parameter documentation before overriding one;
do not copy configuration wholesale from another example.

Omit `name=` from `@cc.function` and `@cc.cls` by default. Use a descriptive Python
function or class name; the SDK derives the registered name from it. Cross-process
lookup and repeated deployment do not require an explicit override. Do not add
`name=` just to repeat the Python name or convert underscores to hyphens.
For a new task or scorer, put a useful prefix in the Python identifier itself;
evaluation and suite names provide the human-readable labels.

Use an override only for an intentional naming contract, such as preserving an
existing deployed name after a Python rename or isolating an integration test.
Keep existing overrides that callers depend on: changing or removing one changes
the target deployment and does not rename the existing function.

Leave `min_replicas`, `max_replicas`, and `concurrency` out of ordinary examples
and evaluations. Functions and classes currently default to one replica
(`min_replicas=1`, `max_replicas=1`) and no explicit concurrency policy
(`concurrency=None`). A dataset with many rows does not by itself justify
changing these settings. Concurrency is a cap on in-flight calls, not a switch
that enables parallel evaluation.

Override capacity only for a concrete requirement, such as deliberate
scale-to-zero, measured throughput needs, or a known downstream concurrency
limit. Explain that requirement next to the option. Apply the same standard to
CPU/memory, retries, timeouts, and other tuning: do not restate defaults or guess
values "just in case." Keep options needed for dependencies, authentication,
or other actual behavior.

When troubleshooting, fix the demonstrated cause and remove temporary tuning
afterward. For example, reverting a temporary `min_replicas=0` to the default
means removing the keyword, not leaving `min_replicas=1` in the final example.

## Testing

Tests run both **locally** and via **Buildkite** (PR-triggered). Credentials are SOPS-encrypted in `.env.enc`.

```
just env        # decrypt .env.enc to .env (needed for local runs)
uv run python -m pytest <test_file> -v   # run tests locally
```

All tests are expected to pass in both environments.

**Never run `just ci-run`.** It pushes and kicks off a Buildkite build; leave that to the human. Run tests locally instead.


Use 

```
just ty
```

to check `ty`
## Proto generation

Python stubs are generated from `chalk-private/protos/generate.sh`.
