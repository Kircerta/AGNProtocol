# Known Issues

## Local Runtime State

Some integration tests require initialized runtime state. Missing
`system_mode.json` puts the runtime into fail-closed mode.

## Provider Availability

Provider-backed commands require configured credentials or a local compatible
endpoint.

## Desktop Automation

Desktop and vision actions depend on local GUI permissions and installed helper
tools.

## Test Suite

Use targeted checks for documentation-only changes. Run the full test suite for
runtime, dispatch, policy, provider, memory, or desktop changes.
