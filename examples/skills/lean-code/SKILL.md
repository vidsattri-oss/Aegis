---
name: lean-code
description: Keep architecture and implementation changes proportional to the request, reuse established code, and review the resulting diff for avoidable complexity.
---

# Keep changes lean

For an existing project, study the supplied source and preserve its entry points and conventions. Reuse existing behavior before introducing a library, abstraction, or service. Prefer the standard library when it covers the requirement clearly.

Treat the approved acceptance criteria as the boundary of the change. Preserve files and behavior outside that boundary. Add a dependency only when the design explains its role and there is a concrete gap in the existing code or runtime.

When reviewing a candidate, apply the supplied checklist against its source and execution evidence. Ask for concrete revisions when a change adds avoidable complexity. Verify behavior through the normal entry point.
