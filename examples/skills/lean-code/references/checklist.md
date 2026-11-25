# Lean code review checklist

- Each changed file supports an acceptance criterion or a required test.
- Shared logic reuses an established implementation where it fits.
- Each dependency has a stated job and a pinned version.
- Helpers clarify behavior for their callers.
- Tests cover a meaningful failure case and the normal entry point.
