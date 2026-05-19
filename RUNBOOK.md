# Runbook

## Setup

```bash
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

## Health Checks

```bash
python3 scripts/agn_bootstrap_check.py check
python3 scripts/agn2_system.py validate
python3 scripts/agn2_system.py status
python3 scripts/agn2_system.py capabilities
```

## Lifecycle

```bash
python3 scripts/agn2_system.py start
python3 scripts/agn2_system.py refresh
python3 scripts/agn2_system.py emergency-stop --reason "operator stop"
python3 scripts/agn2_system.py release-stop --reason "operator release"
```

## Task Start

```bash
python3 scripts/agn2_execution_workflow.py preflight --task-summary "Describe the task"
python3 scripts/agn_task_start_kernel.py build --task-summary "Describe the task"
python3 scripts/agn_operator_brief.py build --task-summary "Describe the task"
```

## Governed Execution

```bash
python3 scripts/agn_governed_execution.py show
python3 scripts/dispatcher_runtime.py --help
python3 scripts/control_daemon.py --help
python3 scripts/admin_command_protocol.py --help
```

## Discovery

```bash
python3 scripts/agn_infrastructure_map.py show
python3 scripts/agn_evolution_pipeline.py show
python3 scripts/agn_reconstruction_status.py show
```

## API

```bash
uvicorn agn_api.main:app --reload --host 127.0.0.1 --port 8000
```

## Tests

```bash
python3 -m py_compile scripts/agn_tool_reality_cards.py scripts/agn_mcp_server.py scripts/awakening_daemon.py scripts/agn_host_state_probe.py
python3 -m pytest -q
git diff --check
```

## Release Check

```bash
git status -sb
python3 scripts/maintenance/check_portability.py
rg --hidden --glob '!.git/**' -n -I 'sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_-]{20,}|BEGIN (RSA|OPENSSH|PRIVATE) KEY' .
```
