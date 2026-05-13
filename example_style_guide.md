# Example Style Guide

How to write examples for the Chalk Sandbox SDK.

## Philosophy

Examples are **literate programs** — the code *is* the documentation. A reader should be able to open one `.py` file and understand what the example does, why it matters, and how every piece works, without switching to a separate file.

## File structure

Each example lives in `examples/<slug>/` and contains:

| File | Purpose |
|------|---------|
| `<slug>.py` | The literate example — a runnable script with Markdown prose in comments, interspersed with code |
| `test/test_<slug>.py` | Pytest suite that proves the example works end-to-end |

The `.py` file serves both the copy-paste user (it runs) and the learner (the prose explains it). No separate `.md` or `README.md` is needed.

## Writing the literate script (`<slug>.py`)

### Prose lives in `#` comments with Markdown formatting

```python
# ## Step 1 — Write the database
#
# The writer container runs an inline Python script that creates a SQLite
# database at `/data/app.db` and inserts three rows.

WRITE_SCRIPT = """..."""
```

This keeps prose visually interleaved with code in the raw `.py`, and doc renderers can extract it into rich HTML.

### Structure

1. **Title and hook** — A `# #` heading followed by 2–3 sentences: what the example does and why you'd want this pattern. Lead with the use case, not the API calls.

2. **Overview** — A numbered list of the concrete steps. The reader should know the full arc before seeing any code.

3. **Sections** — Each logical step gets a `# ##` heading. A short paragraph explains the *why* before the code shows the *how*. Sections follow the order things actually happen at runtime.

4. **Putting it all together** — A `main()` that wires the pieces together with brief inline comments for orientation.

### Tone

- **Technical but accessible.** Assume the reader knows Python and containers, but not the Chalk SDK.
- **Concrete.** Name specific paths, tables, row counts — not "some data."
- **Decisions, not just steps.** If you made a choice (e.g., stopping the writer before starting readers), say why ("so we know data persists independently of the originating container").
- **No filler.** Every sentence should teach something or orient the reader. Cut "In this section we will…" preamble.

### Code style

- Use the `uv run --script` shebang so examples are directly executable:
  ```python
  #!/usr/bin/env -S uv run --script
  #
  # /// script
  # requires-python = ">=3.12"
  # dependencies = ["requests", "python-dotenv"]
  # ///
  ```
- Keep inline scripts (passed to `container.exec`) short and self-contained. Define them as module-level string constants with a prose comment above.
- Avoid explicit `name=` arguments unless the example needs a stable lookup
  across processes, demonstrates custom naming, or names a resource that has
  to be reused later. Functions should usually rely on the Python function
  name, and containers should usually let the SDK generate a name.
- Use `uuid` suffixes when a resource must be named, especially volumes, so
  examples are safe to run concurrently.
- Always clean up in a `finally` block — stop containers, delete volumes.

## Writing the test (`test/test_<slug>.py`)

The test proves the example actually works. It should be runnable both locally (`uv run python -m pytest ...`) and in Buildkite CI.

### Conventions

- Exercise the same operations as the literate script, structured as discrete pytest functions.
- Use a `@pytest.fixture` for shared resources (volumes, images) with cleanup in teardown.
- Skip when credentials are missing:
  ```python
  needs_staging = pytest.mark.skipif(
      not CHALK_CLIENT_ID or not CHALK_API_SERVER,
      reason="CHALK_CLIENT_ID/CHALK_API_SERVER not set",
  )
  ```
- Test names describe what's being proved: `test_two_readers_see_same_data`, not `test_example`.
- Assert on specific, greppable output (`"read 3 rows"`) not vague success checks.
- The test should be self-contained — create and tear down all resources, don't depend on state from the literate script.

### What to test

At minimum, two tests:

1. **The happy path works** — the core operation succeeds (e.g., writer creates the DB).
2. **The interesting property holds** — the thing the example is *about* (e.g., two independent readers see identical data).

Don't test SDK internals or error paths — that belongs in the SDK's own test suite.
