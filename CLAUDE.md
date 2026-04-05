# Chalk Sandbox SDK

## Project tracking

Linear project: https://linear.app/chalk-ai/project/sandbox-devxcapabilities-1ddd7f00a4e7/overview

## Architecture

- `container.py` — High-level `Container(image=Image(...)).run().exec()` backed by `ContainerService`
- `sandbox.py` — Low-level `SandboxClient` / `Sandbox` backed by `SandboxService` (gRPC bidirectional streaming exec)
- `image.py` — Declarative image builder with lazy local file support (`add_local_file`, `add_local_dir`)
- `volume.py` — Volume SDK client backed by `VolumeService`

## Testing

Tests run via Buildkite (PR-triggered). Credentials are SOPS-encrypted in `.env.enc`.

```
just ci-run     # push + print Buildkite build link
```

## Proto generation

Python stubs are generated from `proto/` using `grpc_tools.protoc`. Server-side protos live in `chalk-private/protos/protos/chalk/`.
