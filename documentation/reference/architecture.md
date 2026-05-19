# Architecture

AGNProtocol has four runtime layers.

1. Operator commands enter through lifecycle and command CLIs.
2. Governance checks policy, emergency-stop state, and review requirements.
3. Dispatch routes provider, reviewer, memory, vision, and desktop requests.
4. Runtime adapters perform bounded local work and record evidence.

## Main Surfaces

| Surface | Entry |
|---|---|
| Lifecycle | `scripts/agn2_system.py` |
| Governed execution | `scripts/agn_governed_execution.py` |
| Dispatcher | `scripts/dispatcher_runtime.py` |
| Event store | `src/agn/dispatch/event_store.py` |
| Runtime bus | `src/agn/dispatch/bus.py` |
| Policy gate | `src/agn/core/policy_gate.py` |
| Emergency stop | `src/agn/core/emergency_stop.py` |
| API backend | `agn_api/main.py` |

## Local State

Generated state belongs in ignored directories such as `runtime/`, `reports/`,
`results/`, `verdicts/`, `dispatch/`, `ssot/`, and `.agn_workspace/`.
