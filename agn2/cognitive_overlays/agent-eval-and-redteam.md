# Agent Eval And Red-Team Overlay

Use this overlay when the task involves agent evaluation, MCP surfaces, prompt security, memory poisoning, or operational red teaming.

## What This Overlay Adds

- test the system, not only the final answer
- inspect trajectory, permissions, and side effects
- assume prompt injection and data exfiltration attempts are part of the problem
- verify capability boundaries with negative cases, not only happy paths

## Default Questions

1. What can the agent do, not just what can it say?
2. Which tools, APIs, or memory surfaces could be abused?
3. What would privilege escalation look like here?
4. What would memory poisoning or context poisoning look like here?
5. Which failure would be most expensive to discover late?

## Validation Rhythm

1. Define target behavior and forbidden behavior.
2. Add adversarial cases and out-of-bounds prompts.
3. Check whether the agent actually used tools or execution paths.
4. Record residual risk after the test pass, not only raw scores.

## External Reference Fit

- `promptfoo` for evals, trajectory assertions, red teaming, and MCP security testing
