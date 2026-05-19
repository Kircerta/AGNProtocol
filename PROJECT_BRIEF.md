# Project Brief

AGNProtocol is a governed local agent runtime. It coordinates provider routing,
task dispatch, reviews, memory records, desktop actions, and audit surfaces
through explicit lifecycle and policy gates.

## Core Surfaces

| Surface | Path |
|---|---|
| Lifecycle CLI | `scripts/agn2_system.py` |
| Governed execution | `scripts/agn_governed_execution.py` |
| Task workflow | `scripts/agn2_execution_workflow.py` |
| Task-start kernel | `scripts/agn_task_start_kernel.py` |
| Operator brief | `scripts/agn_operator_brief.py` |
| Dispatcher | `scripts/dispatcher_runtime.py` |
| Python package | `src/agn/` |
| API backend | `agn_api/main.py` |
| Control plane source | `agn2/control_plane/` |

## Runtime Boundaries

- Lifecycle state is controlled through `scripts/agn2_system.py`.
- Policy decisions flow through the governance layer.
- Generated state lives in ignored runtime directories.
- Provider secrets live in environment variables or local secret stores.
- Desktop and vision actions require explicit task intent.

## State Directories

| Path | Role |
|---|---|
| `.agn_workspace/` | Local workspace state |
| `runtime/` | Runtime transport and read models |
| `reports/` | Generated reports |
| `results/` | Task outputs |
| `verdicts/` | Review outputs |
| `memory/records/` | Append-only memory records |

These paths are local state surfaces and are excluded from normal commits.
