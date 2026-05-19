# Coding Criticality Overlay

Use this overlay when the task involves implementation, refactoring, debugging, testing, or code review.

## What This Overlay Adds

- do not jump from request to code
- surface assumptions before execution
- force a disconfirming-evidence pass
- prefer minimal, testable change over broad speculative edits
- separate worker labor from final judgment

## Default Questions

1. What problem is actually being solved?
2. What is explicitly out of scope?
3. What would make this change unsafe or misleading even if it "works" once?
4. What evidence would prove the current idea wrong?
5. What is the smallest useful change that still satisfies the task?

## Implementation Rhythm

1. Restate the target behavior and non-goals.
2. Identify hidden constraints, failure modes, and likely regressions.
3. Define validation before editing.
4. Prefer narrow, reversible changes.
5. Ask for review or run stronger validation when ambiguity remains.

## Review Lens

- spec compliance before style preference
- evidence before confidence
- residual risk must be named
- passing tests are useful, but not a substitute for contract thinking

## External Reference Fit

- `superpowers` for workflow discipline, TDD posture, and review rhythm
- `promptfoo` for agentic-system evaluation
