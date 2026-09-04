#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["chalkcompute>=2.11.1"]
# ///

"""Call the deployed embedding orchestration function.

Deploy ``embed.py`` and ``orchestrate.py`` first.
"""

import chalkcompute

orchestrate = chalkcompute.RemoteFunction.from_name("ex-orchestrate")
orchestrate.wait_ready()
print(orchestrate.remote(["a", "b", "c"]))
