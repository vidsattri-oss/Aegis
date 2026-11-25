---
name: studio-ui-ux
description: Design, implement, and review polished product interfaces from user requirements. Use for new screens, app redesigns, and changes to navigation, forms, or interactive flows that need a coherent visual direction and a testable UX handoff.
---

# Studio UI/UX

Use this skill when the request changes the interface. Start with the brief, approved acceptance criteria, existing product, and available assets. Match the depth of the handoff to the change.

## Design the experience

1. **Define the job.** Identify who is using the screen, what brought them there, the decision or action they need to make, and the visible proof of success. Map the primary path and relevant recovery paths to acceptance criteria. Keep the navigation, terminology, and controls that already serve the task. Record consequential assumptions in `decisions`.
2. **Set a visual direction.** Choose a composition that expresses the product’s purpose. A monitoring view might give the live status and current work the strongest visual weight. A discovery view might lead with imagery that helps users compare choices. Describe what the eye should land on first, second, and third. Carry that hierarchy through type, spacing, contrast, alignment, and the density of real content.
3. **Specify the system.** Reuse established tokens and components when they fit. For new work, specify concrete typography, spacing, surface and accent roles, control variants, and layout widths. Show how long labels, sparse or dense data, and narrow viewports change the layout. Use cards for meaningful grouping, icons with a clear purpose, and imagery that carries content or supports the approved visual direction. Record any asset, crop, and fallback the implementation needs.
4. **Write the interface.** Use the user’s vocabulary and action-specific labels. Place required formats and instructions beside the controls they apply to. Keep the primary action visible or easy to reach through normal scrolling. For relevant loading, empty, validation, failure, and success states, specify what the user sees, what input is retained, and the next action. Provide a clear way out of blocked or interrupted flows.
5. **Design for access.** Specify semantic names and order, keyboard operation, visible focus that stays clear of overlays, status feedback, and the narrow-screen behavior of dense content. Communicate important state through text or another non-color cue. Apply relevant contrast, reflow, and target-size criteria to web work. [WCAG 2.2](https://www.w3.org/TR/WCAG22/)

## Hand off and verify

- **Design** maps the work into the existing JSON contract. Use `flows` for entry, action, branches, and outcome. Use `wireframes` for region order, representative copy, and wide and narrow layouts. Use `components` for reusable patterns and their states. Use `accessibility` for observable checks and `decisions` for visual rationale and assumptions. Use the wireframe tool when a structural preview would help check the order or legibility of the handoff.
- **Development** converts approved decisions into the real screens, states, and interactions using the existing runtime. Verify against realistic content and keep styling shared where the patterns repeat. Use local or approved assets and make the interface usable while optional assets load or are unavailable.
- **QA and review** trace UI requirements to visible outcomes. Exercise the main path and a relevant recovery path through the real entry point. Check keyboard focus, long content, and narrow layouts wherever the available tools permit. Tie findings to a screen, state, and action. Record any checks that still need human or visual verification and distinguish the wireframe preview from evidence produced by the implemented interface.
