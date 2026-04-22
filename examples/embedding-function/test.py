import sys
from pathlib import Path

import chalkcompute

# Add examples dir to path so we can import test_utils
sys.path.insert(0, str(Path(__file__).parent.parent))
from test_utils import make_function_name

orchestrate = chalkcompute.RemoteFunction.from_name(name=make_function_name("orchestrate"))
orchestrate.wait_ready()
embeddings = orchestrate(["Hello", "world!"])
print(embeddings)
