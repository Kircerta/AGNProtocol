# Control Plane

Local Rust + Tauri control console for AGNProtocol.

## Role

- reads `runtime/admin_control/read_models/`
- writes command envelopes under `runtime/admin_control/commands/pending/`
- leaves privileged actions to the control daemon

## Structure

- `src-tauri/`: Rust + Tauri shell
- `ui/`: static frontend

## Development

```bash
cargo tauri dev
```
