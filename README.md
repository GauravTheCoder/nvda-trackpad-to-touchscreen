# Touch Explore Sounds (NVDA add-on)

Improves NVDA's touch explore-by-touch feedback:

- Plays a short tone (1000 Hz, 30 ms) when your finger lands on a real,
  actionable item (icon, list item, button, link, cell, etc), in addition to
  NVDA's normal spoken announcement of that item.
- Stays completely silent (no speech, no tone) whenever the touch hit
  resolves to a generic container (pane, list, window, tree, panel, etc)
  rather than a real item — whether that's because the finger is over
  genuinely empty space (e.g. gaps between desktop icons), or because the
  container itself is briefly hit as a stepping-stone while sliding between
  real rows (e.g. Explorer's "Items View" list re-announcing "19 rows and 4
  columns" between every file name).
- Collapses exact duplicate announcements that happen back-to-back for the
  same item (NVDA sometimes speaks an item once via its object description
  and once via its text/cell content, which can otherwise sound like an
  echo).
- Moves real focus/selection to whatever item your finger lands on
  (VoiceOver-style), so "selected"/"not selected" speech reflects reality
  instead of firing on every item regardless of context.
- Adds a split-tap activation gesture: hold one finger on a touch-explored
  item, then tap anywhere else on the screen with a second finger to
  activate that item (its default action - e.g. open it), equivalent to
  double-tapping the item itself without needing to lift and re-tap the
  same exact spot.

## Why this happens in stock NVDA

NVDA's touch explore logic (`screenExplorer.ScreenExplorer.moveTo`) hit-tests
whatever is under your fingertip and speaks whenever the resolved object
differs from the previous one. Two things cause the "constant chatter"
problem:

1. When there's no icon/item exactly at that pixel, the hit-test resolves to
   the surrounding container (the desktop's icon view, an Explorer list,
   etc). Since that's "a new object" compared to the last icon touched, NVDA
   speaks it — hence the container description repeating in the gaps
   between icons.
2. Even while moving in a fairly straight line across real rows/icons, the
   hit-test can transiently resolve to the container itself between two
   items (not just in the empty margins), so the container's own line (e.g.
   "Items View list, read only, with 19 rows and 4 columns") gets interposed
   between every item you touch.

This add-on patches `moveTo` to suppress speech entirely whenever the
resolved object's role is a generic container role (pane/list/window/tree/
panel/etc), regardless of whether that hit came from empty space or a
transient waypoint between items. Only real items get spoken, and get a
tone cue as well.

## Build

Requires Python 3 (no external dependencies).

```
python build.py
```

Produces `touchExplore.nvda-addon` in the project root.

## Install

Double-click `touchExplore.nvda-addon` with NVDA running, or open it via
NVDA's Add-on Store > "Install from external source", and restart NVDA when
prompted.

## Notes / limitations

- Scope is global (all apps), matching how touch explore-by-touch works in
  NVDA generally.
- The set of "container" roles treated as silence-worthy is defined in
  `_CONTAINER_ROLES` in `touchExplore/globalPlugins/touchExplore/__init__.py`;
  extend it there if you find another app/control whose empty space still
  chatters.
- Tested against the NVDA `screenExplorer.py` logic as of the 2026.1 source.
  If a future NVDA version changes `moveTo`'s internals substantially, the
  patched copy in this add-on may need to be resynced with core.
