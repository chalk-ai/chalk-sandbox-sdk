# Local Remote Function Dev

This note describes the recommended developer workflow for testing local
changes in:

- `chalkcompute` in this repo

inside a deployed remote function, especially for nested remote calls such as:

```python
@chalkcompute.function()
def call_defer() -> int:
    import chalkcompute

    return chalkcompute.RemoteFunction.from_name("add").defer(2, 3).get()
```

## Recommended Modes

There are two developer-facing modes that are worth supporting well:

1. Local `chalkcompute` Python source overlay
2. Local `chalkcompute` Linux wheel override

The current recommended deploy env for testing both together is:

```bash
just local-fn-deploy \
  examples/scalar/deploy_sync.py \
  /abs/path/to/chalkcompute-...linux_x86_64.whl
```

Important: all env vars must be part of the same shell command. If the wheel
assignment is on its own line without a trailing `\`, the deploy process will
not receive it.

Use:

```bash
just local-fn-env /abs/path/to/chalkcompute-...linux_x86_64.whl
```

to print the raw env block,

```bash
just local-fn-deploy examples/scalar/deploy_sync.py /abs/path/to/chalkcompute-...linux_x86_64.whl
```

to run a deploy script with the recommended local-dev env, and:

```bash
just sdk-wheel-build
```

to build a Linux wheel from macOS or Linux via Docker.

## Why Strip Mode Is Confusing

When deploying from a source checkout with version `0.0.0`, the SDK defaults
to strip mode. In strip mode, the deployed pod does not mount the local SDK
source copy, so the function imports the packaged `chalkcompute` from the base
image:

```text
/usr/local/lib/python*/site-packages/chalkcompute
```

That makes it easy to accidentally test an old packaged SDK instead of local
changes.

## Local Python Source Overlay (`CHALK_FUNCTION_LOCAL_SDK_COPY=1`)

To force the deployed function to use the local SDK source, deploy with:

```bash
CHALK_FUNCTION_STRIP_SOURCE=0 \
CHALK_FUNCTION_LOCAL_SDK_COPY=1 \
.venv/bin/python test.py
```

Expected pod entrypoint shape:

```sh
cat /tmp/sdk/_archive.part.* > /tmp/_archive.tar.gz
tar xzf /tmp/_archive.tar.gz -C /tmp/chalkcompute-sdk-src
export PYTHONPATH=/tmp/chalkcompute-sdk-src:$PYTHONPATH
exec chalk-remote-call --handler handler.handler
```

Expected import path in the pod:

```text
/tmp/chalkcompute-sdk-src/chalkcompute/__init__.py
```

## Local SDK Wheel Override (`CHALK_FUNCTION_LOCAL_SDK_WHEEL`)

The local `chalkcompute` wheel should be built for Linux and mounted into the
pod as a separate lazy-file volume. At startup the wrapper should:

1. `pip install` the mounted wheel
2. `exec chalk-remote-call --handler ...`

Expected pod signals:

- extra chalkfs volume container for `/app`
- extra chalkfs volume container for `/tmp/sdk`
- wrapped main command like:

```sh
sh -c 'set -eux; python -m pip install /tmp/sdk/chalkcompute-...whl; exec chalk-remote-call --handler handler.handler'
```

- startup logs showing:

```text
Successfully installed chalkcompute-0.0.0
```

When the local wheel is enabled:

- function source + generated handler under `/app`
- local `chalkcompute` wheel under `/tmp/sdk`

the pod should have 3 containers total:

1. the main function container
2. the `/app` chalkfs mount container
3. the `/tmp/sdk` chalkfs mount container

## Native Client Extension

Nested `.defer()` calls require the shared native client extension:

```text
chalkcompute/_native*.so
```

The local SDK source archive contains Python files, but it does not contain a
Linux native extension built for the pod's Python version. If the local source
overlay shadows the installed `chalkcompute` package, importing
`chalkcompute._native` can fail.

For testing, the runtime image should still install a packaged `chalkcompute`
wheel so the native extension exists in site-packages, while the local source
overlay provides the Python code under test:

```python
runtime_image = Image.debian_slim().pip_install(["chalkcompute==0.0.0"])


@chalkcompute.function(image=runtime_image)
def call_defer() -> int:
    import chalkcompute

    return chalkcompute.RemoteFunction.from_name("add").defer(2, 3).get()
```

The source-overlay path should continue to support using the installed wheel's
native extension even when `/tmp/chalkcompute-sdk-src` is first on `PYTHONPATH`.

## SDK Gap To Close

The source-overlay support needs a fallback in `chalkcompute/_remote_call_client.py`.
The normal import:

```python
importlib.import_module("chalkcompute._native")
```

fails when `/tmp/chalkcompute-sdk-src/chalkcompute` shadows the installed
`chalkcompute` package, because the source archive does not contain the Linux
extension file.

The fallback should:

1. Try the normal `chalkcompute._native` import first.
2. If that fails, scan `sys.path` entries other than the local source package for
   `chalkcompute/_native{EXTENSION_SUFFIX}`.
3. Load the first matching extension via `importlib.util.spec_from_file_location`.
4. Keep using that module for `RemoteCallClient` and
   `resolve_service_auth_context`.

In the verified pod, this meant Python source came from:

```text
/tmp/chalkcompute-sdk-src/chalkcompute
```

while the native extension came from:

```text
/usr/local/lib/python*/site-packages/chalkcompute/_native*.so
```

## Build Helper For `chalkcompute_native`

Most developers build local wheels on macOS, but the deploy needs a Linux
`chalkcompute` wheel containing `chalkcompute._native`. The helper script in
this repo wraps Docker so the same command works from macOS or Linux:

```bash
just sdk-wheel-build
```

That builds the wheel in a Linux container and copies the result into:

```text
dist/linux-local/
```

The helper defaults `--chalk-private` to `../chalk-private` and rewrites the
private git dependency URL inside the container so Rust dependencies can resolve
from your local checkout instead of requiring GitHub auth in Docker.

## Useful Pod Checks

Check whether the pod is using the local source overlay:

```bash
python -c 'import chalkcompute; print(chalkcompute.__file__)'
```

Check whether the old `CHALK_GRPC_ENGINE` guard is still present:

```bash
python -c 'import inspect, chalkcompute._function as f; src = inspect.getsource(f.RemoteFunction._ensure_remote_call_client); print("CHALK_GRPC_ENGINE must be set" in src)'
```

Check whether the native client extension can be imported:

```bash
python -c 'import chalkcompute._native as n; print(n.__file__)'
```

For the source-overlay case, run those checks under the same `PYTHONPATH` that
the entrypoint uses:

```bash
PYTHONPATH=/tmp/chalkcompute-sdk-src:$PYTHONPATH python -c '...'
```

Check whether the local SDK wheel was actually installed:

```bash
python -c 'import importlib.metadata as m; print(m.version("chalkcompute"))'
```

Check the pod command shape:

```bash
kubectl get pod <pod> -o yaml | rg "pip install|_archive.part|chalk-remote-call --handler"
```
