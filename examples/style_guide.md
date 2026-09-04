# Example style guide

This guide is for SDK developers and reviewers who create or maintain public examples in
the Chalk Sandbox SDK repository. The examples and `examples/README.md` are customer-facing;
this file defines the engineering and review contract behind them.

## Philosophy

Examples are **literate programs** — the code *is* the documentation. A reader should be
able to open one `.py` file and understand what the example does, why it matters, and how
every piece works, without switching to a separate file.

When a scenario has multiple executable scripts, each script must explain its role,
prerequisites, and resource lifecycle without requiring the reader to inspect another
file. Support modules and READMEs may provide additional context, but must not carry
information required to understand an entry point's purpose or behavior.

Give each example one clear teaching goal. Choose implementation details that make that
behavior easy for a customer to understand and apply.

An example should be:

- understandable without access to a private repository or Chalk environment;
- directly executable when it has a runnable entry point;
- explicit about credentials, external services, destructive behavior, scale, and cost;
- safe to run concurrently unless shared state is essential to its purpose; and
- clear about every resource it creates and whether that resource is deleted or retained.

Prefer deleting an obsolete, duplicate, misleading, or private-only example over keeping
it as historical code.

## Layout

Each scenario lives under `examples/<slug>/`. Use the smallest structure that makes the
scenario coherent:

- A single script is preferred for a small, independent workflow.
- Multiple executable entry points are appropriate for deploy/call workflows.
- Support modules, assets, Dockerfiles, and locked projects are appropriate when the
  scenario genuinely needs them.
- Add a scenario README only for supplementary multi-file context, extended safety
  guidance, or sample output. It must not substitute for literate entry points.
- Keep `examples/README.md` as an inventory-free customer quick-start. It should explain
  setup, direct execution, expected behavior, and general safety/cost considerations
  without exposing maintainer terminology or requiring updates when individual examples
  are added, renamed, or removed.

Do not add an otherwise unnecessary directory layer.

## Executable entry points

Every runnable Python script must be executable and start with a PEP 723 `uv` shebang:

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute>=<minimum-supported-version>"]
# ///
```

Declare every local runtime dependency in the script metadata, using public published
packages and an honest minimum version. A locked project may coexist with executable
scripts for dependency review, but an entry point must not depend on the caller being in
a particular working directory for dependency discovery.

Helper modules are not executable and do not need a shebang. Keep executable mode and
shebang presence consistent.

Do not put commands in source comments or docstrings explaining how to invoke an
executable script. Its path and `--help` interface are the invocation documentation.
Document prerequisites and sequencing constraints without prescribing a working
directory or wrapper command.

## Literate source

The code is the primary documentation. Explain the use case, important decisions, and
resource lifecycle close to the relevant code. Use Markdown headings in `#` comments for
longer examples when that materially improves navigation.

Scale the prose to the example:

- A small deployer may need only a precise module docstring and a persistence note.
- A multi-step workflow should give the reader the full arc before the implementation,
  then explain each non-obvious step in execution order.
- Inline comments should explain why a choice matters, not restate the next line.
- Keep inline programs passed to `sandbox.exec` short, self-contained, and named as
  module-level constants when reused or substantial.

Assume the reader knows Python and containers but not this SDK. Be concrete and remove
filler.

## Public SDK usage

Use public, published APIs and dependencies. Do not rely on private imports, private
attributes, unpublished packages, sibling repositories, personal filesystem paths,
cluster port-forwards, or staging-only service addresses.

Prefer `Sandbox` or `SandboxClient` for new sandbox workflows and call `terminate()` for
cleanup. `Container` is deprecated. Use it only when `Sandbox` lacks a public capability
required by the example, and explain that exception next to the usage so it can be
removed when the capability becomes available.

When a concept such as environment variables, secrets, volumes, compute resources,
images, or network policy exists on multiple SDK surfaces, use the public shape supported
by the surface being demonstrated. Do not copy internal normalization or implementation
details into examples.

## Names and concurrency

Avoid explicit names unless an example needs cross-process lookup, demonstrates naming,
or deliberately reuses a resource across runs.

When an example owns a named ephemeral resource, add a UUID suffix. This prevents
concurrent runs from replacing one another and ensures cleanup cannot delete an existing
caller-owned resource.

Treat a resource supplied by ID, name, path, or environment variable as caller-owned
unless the interface explicitly says otherwise. Do not delete caller-owned resources.

## Resource lifecycle

Classify every created resource as one of the following:

- **Ephemeral:** owned by this run and deleted before exit.
- **Intentionally persistent:** retained because later calls, schedules, inspection, or
  the example's output require it.
- **Caller-owned:** attached to or updated by the example but not owned by it.
- **Platform-managed:** reusable image caches, call history, and similar state without a
  script-level deletion lifecycle.

Ephemeral resources must be cleaned up in `finally` blocks on success, failure, and
interrupt. Construct the local handle before starting or deploying the resource so a
partially successful operation can still be cleaned up. Guard cleanup when the remote ID
was never assigned. When several resources are created:

- delete them in reverse dependency order;
- attempt every cleanup even if one deletion fails; and
- use unique names so cleanup affects only this run.

Use a bounded server-side lifetime as a fallback for costly ephemeral compute when the
local process can be killed before `finally` runs. Use protobuf duration strings such as
`"600s"`, not informal values such as `"10m"`.

Intentional persistence is valid. State it prominently in the entry point, including what
remains, why it remains, and whether it continues to consume billable capacity. Repeat it
in a scenario README when one exists. Deploy-only functions, scheduled functions,
services intended for a separate client, and destination volumes are common persistent
cases.

## Credentials, privacy, and safety

- Never commit credential-shaped sample values or read a personal `.env` path.
- Use standard environment discovery and public `Secret` APIs.
- Prefer consumer-scoped secrets for deployed workloads.
- Do not interpolate secrets into command lines, URLs, logs, or model-visible text.
- Warn before copying local provider configuration or sending file contents, prompts, or
  command output to an external model or service.
- State when an example executes model-generated commands, probes security boundaries,
  performs destructive operations inside a sandbox, starts many resources, uses GPUs, or
  creates persistent storage.

## Tests and verification

Follow the repository's `AGENTS.md`: do not add tests unless explicitly requested.

When test coverage is requested or already exists:

- put integration coverage in the existing `e2etests/` structure rather than requiring a
  colocated test directory for every example;
- use the shared credential markers, resource naming, deployment helpers, and cleanup
  fixtures;
- exercise stable, externally observable behavior rather than implementation details;
- reuse the narrowest existing harness; and
- keep billable or environment-specific tests explicit in Buildkite.

An example change should at least receive non-billable static verification appropriate to
the files touched: syntax parsing, Ruff, executable-mode/shebang consistency, dependency
and path searches, and `git diff --check`. Do not launch remote resources merely to claim
the example is current; say when verification was static only.

## Review checklist

- The example's intent is still useful and technically honest.
- Every runnable entry point is executable and self-declares its dependencies.
- The example uses current public SDK surfaces and published packages.
- It contains no personal path, private API, staging-only assumption, or credential.
- Names are absent, stable by necessity, or unique for resources owned by the run.
- Every created resource has an explicit lifecycle classification.
- Ephemeral cleanup covers startup failures and does not stop after the first cleanup
  error.
- Persistent resources, cost, privacy, and destructive behavior are conspicuous.
- Every entry point explains its own intent, behavior, prerequisites, decisions, and
  lifecycle without relying on another file or adding invocation boilerplate.
- Static verification is clean; any unperformed runtime verification is stated honestly.
