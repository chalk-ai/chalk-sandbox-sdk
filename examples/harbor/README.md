# Harbor evals on Chalk sandboxes

[Harbor](https://www.harborframework.com/) runs agent benchmarks: each task is a container
image, an instruction, and a verifier that writes a reward. `chalk_harbor` is a Harbor
environment provider that runs every trial's container as a Chalk sandbox, so a benchmark
runs on Chalk compute without Docker on the machine driving it.

The provider is also published on its own as [chalk-ai/chalk-harbor](https://github.com/chalk-ai/chalk-harbor).

```
harbor run -p <tasks> -a <agent> -e chalk_harbor:ChalkSandboxEnvironment
```

| Path | What it is |
| --- | --- |
| `chalk_harbor/` | The environment provider (`--env chalk_harbor:ChalkSandboxEnvironment`). |
| `smoke/` | Three tiny tasks with reference solutions, runnable with the `oracle` agent (no LLM). |
| `enterprise_bench/prepare.py` | Stages [Enterprise-Bench/l1-l2-bench](https://hub.harborframework.com/datasets/Enterprise-Bench/l1-l2-bench) for sandboxes. |
| `chalk_eval.py` | Runs a Harbor task set as a Chalk evaluation: one row per task, reward as the score. |

## Setup

```bash
uv tool install harbor --with chalkcompute --with dockerfile-parse
export PYTHONPATH=$PWD          # so harbor can import chalk_harbor
chalk login                     # or CHALK_CLIENT_ID/SECRET, or CHALK_WEB_IDENTITY_TOKEN_FILE
```

## 1. Smoke test (no API keys)

```bash
harbor run -p smoke -a oracle -e chalk_harbor:ChalkSandboxEnvironment -n 3 --yes
```

Three trials, three sandboxes, mean reward 1.0 in about 25 seconds. `copy-context` checks that a
Dockerfile `COPY` followed by a `RUN` that reads the copied file builds correctly.

## 2. Enterprise-Bench L1-L2

The dataset assumes one Docker host: a locally built base image, and MCP tool servers reached
through `host.docker.internal`. `prepare.py` replaces both:

- **Data:** uploads the benchmark data (~33 MB) to a Chalk volume that each sandbox mounts at `/data`.
- **Base image:** has the Chalk image service build the base image, with the three MCP servers baked in.
- **Startup script:** the sandbox entrypoint starts the submission API and the MCP servers inside every task sandbox, so the agent reaches them on `localhost`.

```bash
harbor download Enterprise-Bench/l1-l2-bench -o ./eb
./enterprise_bench/prepare.py ./eb/l1-l2-bench --volume <your-volume-name>
export OPENAI_API_KEY=...        # the judge is openai/gpt-5; codex uses it too
# then run the `harbor run ...` command prepare.py prints
```

The printed command runs one task (`eng-l1-a`) with `codex` on `openai/gpt-5-mini`. Widen `-i`
for more tasks, or change `-m` for a stronger model. The image preinstalls codex because Harbor's
codex installer apt-installs Debian's `nodejs`/`npm`, which conflicts with the NodeSource Node
already in the base image.

## 3. As a Chalk evaluation

```bash
./chalk_eval.py                  # every task in smoke/, or name some
```

The task function runs one Harbor trial per dataset row; the trial's own environment is a
further sandbox, created with the function's platform identity (`chalk_identity=True`). The
scorer's metadata keeps the trial name, all rewards, any exception, and wall time.

## Environment kwargs

Pass with `--ek key=value`:

| Kwarg | Meaning |
| --- | --- |
| `image_map` | `src=dst[,...]`: rewrite a `FROM` or `docker_image` reference, e.g. to a prebuilt Chalk image. |
| `entrypoint` | JSON list run as the sandbox's main process instead of `sleep infinity`. |
| `ready_command` | Shell command polled after start until it exits 0, e.g. waiting for entrypoint services. |
| `volumes` | `name:/path[,...]`: existing Chalk volumes to mount. |
| `lifetime` | Maximum sandbox lifetime (default `3600s`), so a crashed run doesn't leak sandboxes. |

## Things that differ from Docker

- **The image's `ENTRYPOINT`/`CMD` does not run.** A sandbox's main process is `sleep infinity`
  unless `entrypoint` says otherwise. Tasks whose image starts a service need `entrypoint`.
- **Writes under `/workspace` at build time are discarded by the image service.** `COPY` into
  `/workspace` is staged under `/opt/.harbor-workspace` and restored when the sandbox starts.
  `RUN` steps that write there are still lost, so move that work elsewhere.
- **Memory is raised to 2 GiB per CPU when a task asks for less**, because the sandbox service
  rejects a smaller request.
- **Only single-stage Dockerfiles work, with no docker-compose sidecars.** Build context is embedded
  into `RUN` steps (4 MiB compressed per `COPY`); mount larger data with `volumes`.
- **Network mode is fixed when the sandbox is created.** `public` maps to an all-IPv4 egress
  route, since a sandbox with no route cannot even resolve DNS. `allowlist` maps hostnames and
  IPv4 CIDRs, and `no-network` maps to an empty policy. Switching mode between agent and
  verifier phases is not supported.
