# AGNProtocol

AGNProtocol is a local-first governed agent runtime for coordinating AI
providers, task execution, reviews, memory records, desktop actions, and audit
trails.

The runtime keeps execution behind explicit lifecycle, policy, emergency-stop,
and review gates. The project is experimental and intended for local research,
prototype orchestration, and agent-safety work.

## Requirements

- Python 3.11 or newer.
- `uv` for environment setup.
- At least one configured provider for model-backed execution.

## Install

```bash
git clone https://github.com/Kircerta/AGNProtocol.git
cd AGNProtocol
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

## Configure

Provider credentials belong in environment variables or a local secret manager.
Do not commit `.env` files, runtime state, provider tokens, logs, archives, or
machine-specific files.

Common variables:

```bash
export DEEPSEEK_API_KEY="..."
export QWEN_LOCAL_BASE_URL="http://127.0.0.1:8000/v1"
export QWEN_LOCAL_MODEL="qwen-model-name"
export TELEGRAM_BOT_TOKEN="..."
export ALLOWED_CHAT_IDS="123456"
```

Local operator files such as `HOST_INFO.md`, `.local/`, `runtime/`,
`reports/`, `results/`, `verdicts/`, and `.agn_workspace/` are ignored by git.

## Core Commands

```bash
python3 scripts/agn_bootstrap_check.py check
python3 scripts/agn2_system.py validate
python3 scripts/agn2_system.py status
python3 scripts/agn2_system.py capabilities
python3 scripts/agn2_system.py start
python3 scripts/agn2_system.py emergency-stop --reason "operator stop"
python3 scripts/agn2_system.py release-stop --reason "operator release"
```

## Task Workflow

```bash
python3 scripts/agn2_execution_workflow.py preflight --task-summary "Describe the task"
python3 scripts/agn_task_start_kernel.py build --task-summary "Describe the task"
python3 scripts/agn_operator_brief.py build --task-summary "Describe the task"
python3 scripts/agn_governed_execution.py show
python3 scripts/agn_infrastructure_map.py show
```

## API Backend

```bash
uvicorn agn_api.main:app --reload --host 127.0.0.1 --port 8000
```

## Tests

```bash
python3 -m py_compile scripts/agn_tool_reality_cards.py scripts/agn_mcp_server.py scripts/awakening_daemon.py scripts/agn_host_state_probe.py
python3 -m pytest -q
```

Some integration tests require initialized local runtime state and provider
availability.

## Documentation

- [Runbook](RUNBOOK.md)
- [Security Policy](SECURITY.md)
- [Documentation Index](documentation/README.md)
- [Third-Party Notices](THIRD_PARTY_NOTICES.md)

## License

MIT. See [LICENSE](LICENSE).
