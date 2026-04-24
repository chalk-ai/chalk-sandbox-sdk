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

## Testing

Tests run both **locally** and via **Buildkite** (PR-triggered). Credentials are SOPS-encrypted in `.env.enc`.

```
just env        # decrypt .env.enc to .env (needed for local runs)
uv run python -m pytest <test_file> -v   # run tests locally
just ci-run     # push + print Buildkite build link
```

All tests are expected to pass in both environments.


Use 

```
just ty
```

to check `ty`
## Proto generation

Python stubs are generated from `chalk-private/protos/generate.sh`.
