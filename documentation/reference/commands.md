# Commands

## Setup

```bash
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

## Lifecycle

```bash
python3 scripts/agn2_system.py validate
python3 scripts/agn2_system.py status
python3 scripts/agn2_system.py capabilities
python3 scripts/agn2_system.py start
python3 scripts/agn2_system.py emergency-stop --reason "operator stop"
python3 scripts/agn2_system.py release-stop --reason "operator release"
```

## Task Start

```bash
python3 scripts/agn2_execution_workflow.py preflight --task-summary "Describe the task"
python3 scripts/agn_task_start_kernel.py build --task-summary "Describe the task"
python3 scripts/agn_operator_brief.py build --task-summary "Describe the task"
```

## Discovery

```bash
python3 scripts/agn_infrastructure_map.py show
python3 scripts/agn_governed_execution.py show
```

## API

```bash
uvicorn agn_api.main:app --reload --host 127.0.0.1 --port 8000
```
