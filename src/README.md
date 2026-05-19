# AGN Python Package

`src/agn/` contains the importable Python package for AGNProtocol.

## Layout

```text
src/agn/
├── architecture/  # infrastructure and capability maps
├── core/          # constitution, emergency stop, policy gate, guarded I/O
├── dispatch/      # dispatcher, bus, event store
├── governance/    # lifecycle, commands, control daemon, execution gateway
├── handlers/      # desktop, provider, review, memory, vision adapters
├── runtime/       # host and runtime fact surfaces
├── tools/         # package utilities
└── integrations/  # external integration helpers
```

## Common Imports

```python
from agn.core.admin_control import repo_root, atomic_write_json
from agn.core.emergency_stop import is_emergency_stop_active
from agn.core.policy_gate import evaluate_dispatch_request
from agn.core.role_guard import require_write_access
from agn.dispatch.dispatcher import dispatch_request
from agn.dispatch.bus import publish_message
from agn.dispatch.event_store import append_event
from agn.governance.system import refresh_system
from agn.governance.execution_gateway import dispatch_provider_task
from agn.runtime.host_info import build_host_info
from agn.architecture.infrastructure_map import build_infrastructure_map
```

## Development

```bash
PYTHONPATH=src:scripts python3 -m pytest tests/ -q
```
