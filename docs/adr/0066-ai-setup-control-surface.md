# 0066. AI setup is the AI control surface

- Status: Accepted
- Date: 2026-10-20
- Relates to: [0012](0012-runtime-ai-features.md),
  [0063](0063-fleet-service-availability-in-tui.md),
  [0065](0065-ai-capability-labels-and-requirements.md)

## Context

ADR-0065 moved the fleet panel into a feature-centric requirements view and put
the AI toggles in the settings modal. That left two problems:

- the settings modal carried six AI rows, ten output/audio rows and a hint
  line; it ignored its `height` argument and had no scroll, so short terminals
  clipped the title or the bottom;
- the `a` view was read-only. It could explain what a pass needs, but the user
  had to switch to `s` to turn it on, then back to `a` to see the effect;
- descriptions existed in `config.FEATURES` and `/api/fleet` but were never
  rendered. Fixed `30`/`34`/`62`-cell columns and a duplicated title also made
  the `a` view fragile on narrow terminals.

## Decision

1. **`a` owns the AI capabilities.** It lists each pass with its full
   description, toggle state, resolved models, live readiness and the on-demand
   start command. `space`/`enter` toggles the selected pass; `r` refreshes;
   `s` opens settings; `esc` closes.
2. **`s` owns output and audio preferences only.** It is sectioned
   (**Output**, **Audio transcription**) and no longer contains AI rows or the
   old AI setup hint.
3. **Overlays are bounded and scrollable.** Both views use an exact height
   budget (title, header, footer, borders). Settings uses `windowBounds` to keep
   the selected row visible; the AI body starts at the selected pass block.
   Settings rows are a `header`/`setting` model; headers are not selectable.
4. **The AI view is width-aware.** Descriptions wrap at the box width, model
   names are truncated to their column, and the state/glyph is never truncated.
   The divider is sized to the box; the duplicated title is removed.
5. **Toggles mirror the engine's `implies` rule locally** (classifier enables
   vision; disabling vision disables the classifier), then refresh the fleet
   after the `/api/config` round-trip.
6. **No model lifecycle action.** The view still only reports the `start`
   command; it never starts or stops a model (ADR-0063).
7. **The short footer is compact** so `a AI` and `?` remain visible at 80
   columns; the full binding list stays under `?`.

## Consequences

- **+** One screen answers “what does this pass do, what does it need, is it
  on, and what do I start?” without switching between settings and details.
- **+** Settings fits comfortably and scales to short terminals via scrolling.
- **+** Descriptions have room and are rendered instead of existing only in the
  API.
- **+** The AI screen and the requirements projection can evolve together.
- **−** Users who expect AI toggles under `s` must learn `a`; the footer and
  help point at it.
- **−** `a` is no longer read-only; the engine contract is unchanged, but the
  TUI now writes `/api/config` from this view.
- **−** Two overlay renderers share layout helpers and need render tests at
  small sizes.

## Alternatives considered

- **Keep toggles in `s` and only add a scrollable sectioned list** — still
  splits “what it does” (a) from “turn it on” (s), and leaves `s` dense.
  Rejected.
- **Put the description under the selected settings row** — less disruptive,
  but the AI list remains crowded and the detail is hidden until the cursor
  moves. Rejected.
- **Full-screen settings instead of overlays** — more space, but changes the
  fixed-frame shell and the job/watch surface; not needed for this fix.
- **Let `a` start models** — blocking lifecycle action; explicitly deferred by
  ADR-0063. Rejected.
