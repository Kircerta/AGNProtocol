# System Layout

```text
Operator
  |
Control Plane
  |
Governance Layer
  |
Runtime Layer
  |
Adapters and Providers
```

## Paths

| Path | Purpose |
|---|---|
| `agn2/governance/` | Constitution and policy configuration |
| `agn2/control_plane/` | Tauri control-plane source |
| `src/agn/governance/` | Lifecycle and command implementation |
| `src/agn/dispatch/` | Dispatcher, bus, and event store |
| `scripts/` | CLI entrypoints |
| `runtime/` | Generated runtime state |
