import json
import sys
from pathlib import Path

import chalkcompute

# Add examples dir to path so we can import test_utils
sys.path.insert(0, str(Path(__file__).parent.parent))
from test_utils import make_function_name

agent = chalkcompute.RemoteFunction.from_name(name=make_function_name("fraud-agent"))
agent.wait_ready()

transaction_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
verdict_json = agent(transaction_id)
print(json.dumps(json.loads(verdict_json), indent=2))
