"""Exercise the installed agent and reviewed MCP producers outside the checkout."""

import json
import os
import subprocess
import sys
import tempfile


CODE = '''
import json
from importlib.metadata import version
from plat_agent.lifecycle.versioned_adapters import COSTMODEL_V2, UNDERWRITING_V5
from plat_agent.costmodel_client import CostModelClient
from plat_agent.underwriting_client import UnderwritingClient
from importlib.resources import files

COSTMODEL_V2.verify()
UNDERWRITING_V5.verify()
cost = CostModelClient().call_tool('estimate', {'unit_sqft':750, 'bedrooms':1, 'bathrooms':1})
assert cost and 'error_type' not in cost and 'error' not in cost
inputs = json.loads(files('plat_agent.lifecycle').joinpath('fixtures/test001_underwriting_inputs.json').read_text())
client = UnderwritingClient()
try:
    checked = client.validate(inputs)
    assert checked['adapter_contract'] == 'plat.underwriting.mcp/1'
    assert checked['status'] != 'error'
finally:
    client.close()
print(json.dumps({'status':'passed', 'agent_version':version('plat-agent'),
                  'mcp_version':version('mcp'), 'checks':['producer_digests','costmodel_stdio',
                  'underwriting_stdio','packaged_synthetic_input']}))
'''


if __name__ == "__main__":
    environment = {k: v for k, v in os.environ.items() if not k.startswith((
        "PYTHON", "PLAT_", "UNDERWRITING_", "COSTMODEL_"))}
    with tempfile.TemporaryDirectory(prefix="plat-agent-install-") as temporary:
        subprocess.run([sys.executable, "-I", "-c", CODE], cwd=temporary,
                       env=environment, check=True, timeout=120)
