# Third-Party Notices

The top-level MIT license covers AGN-owned source in this repository. It does
not relicense third-party dependencies, registry packages, optional tools, or
upstream projects referenced by AGN documentation.

## Vendoring Boundary

This repository does not vendor external source repositories, runtime archives,
generated reports, or local tool caches.

Lockfiles are included to make dependency resolution inspectable:

- `uv.lock`
- `agn2/control_plane/src-tauri/Cargo.lock`
- `agn2/conversation_monitor/src-tauri/Cargo.lock`
- `tools/agn-health/Cargo.lock`

Transitive dependency licenses should be checked from package metadata before
redistributing binary builds.

## Direct Python Dependencies

- FastAPI: https://github.com/fastapi/fastapi
- Uvicorn: https://github.com/encode/uvicorn
- PyJWT: https://github.com/jpadilla/pyjwt
- HTTPX: https://github.com/encode/httpx
- pytest: https://github.com/pytest-dev/pytest
- MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk
- Chroma: https://github.com/chroma-core/chroma
- uv: https://github.com/astral-sh/uv

## Direct Rust / Desktop Dependencies

- Tauri: https://github.com/tauri-apps/tauri
- Serde: https://github.com/serde-rs/serde
- serde_json: https://github.com/serde-rs/json
- time: https://github.com/time-rs/time
- crates.io registry: https://github.com/rust-lang/crates.io-index

## Optional External Tools

AGN documentation and wrapper code reference several optional tools. They are
not vendored here.

- browser-use: https://github.com/browser-use/browser-use
- promptfoo: https://github.com/promptfoo/promptfoo
- Deep Agents: https://github.com/langchain-ai/deepagents
- Hindsight: https://github.com/vectorize-io/hindsight
- beads: https://github.com/steveyegge/beads
- InsForge: https://github.com/InsForge/InsForge
- ChatGPT Prompts for Academic Writing: https://github.com/ahmetbersoz/chatgpt-prompts-for-academic-writing
- Superpowers: https://github.com/obra/superpowers

If a downstream distribution vendors any of these projects, include that
project's license and notices next to the vendored copy.
