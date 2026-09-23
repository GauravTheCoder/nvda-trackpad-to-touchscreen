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
- Fixes multi-finger taps sometimes being detected with fewer fingers than
  actually used (e.g. a 3-finger tap registering as a 1- or 2-finger tap).
- Adds a trackpad-as-touchscreen mode (**NVDA+Ctrl+Shift+T** to toggle): on a
  laptop with no touchscreen, reads your trackpad's own multi-touch surface
  directly and maps it onto the whole screen, so every touch gesture above -
  plus NVDA's own built-in touch gestures (explore, tap, flick, multi-finger
  taps, mode-cycling, etc) - work from the trackpad exactly as they would on
  real touchscreen hardware. While this mode is on, the add-on also turns off
  the OS's own trackpad gestures (tap-to-click, two-finger tap, pinch/pan,
  the corner right-click zone) that would otherwise fire at the same time
  from the same fingers, and restores your original trackpad settings when
  you turn the mode back off. See "Trackpad-as-touchscreen mode" below for
  requirements and limitations.

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

## Trackpad-as-touchscreen mode

Press **NVDA+Ctrl+Shift+T** to turn this on or off; NVDA announces the new
state. While on, dragging a finger on your trackpad explores the screen the
same way dragging a finger on a real touchscreen would (proportionally - the
top-left of your trackpad maps to the top-left of your screen, and so on),
and all touch gestures (tap, flick, hold, multi-finger taps, this add-on's
split-tap activation, etc) work from it.

Requirements:

- A Windows Precision Touchpad (the standard type on virtually all modern
  Windows laptops; older "legacy" trackpads that only report themselves to
  Windows as a plain mouse are not supported, since they don't expose real
  multi-touch contact data to any application).
- Works on any Windows version for the touch input itself. The best-effort
  minimization of the OS's own trackpad gestures while this mode is on
  additionally requires Windows 11 version 24H2 or later; on earlier Windows
  versions that part is silently skipped and trackpad-as-touchscreen mode
  still works, just with more potential interference from the OS's own
  gesture recognition running on the same physical trackpad at the same
  time.

Limitations:

- While trackpad-as-touchscreen mode is on, the ordinary mouse cursor is
  frozen in place - it won't move or click, from the trackpad or from any
  other mouse connected to your PC at the time, since Windows only lets an
  application suppress normal mouse behavior for a whole class of device,
  not one specific physical mouse. Your mouse (all mice) work normally
  again the instant you turn trackpad mode back off. This is deliberate: it
  stops a swipe/tap gesture from also moving the real cursor or triggering
  a real click somewhere on screen.
- Windows does not provide any documented way for an application to become
  the *exclusive* consumer of a trackpad's input for its OS-level gestures
  specifically (separate from the mouse-cursor freeze above). 3/4-finger
  gestures in particular (3-finger tap opening the Start menu, 3/4-finger
  swipes switching virtual desktops or opening Task View) will still fire
  from the OS while trackpad-as-touchscreen mode is on - this was
  specifically investigated and confirmed not fixable live: Windows' only
  setting for this ("Three- and four-finger touch gestures" in
  Settings > Bluetooth & devices > Touchpad) does not take effect without a
  sign-out or restart, even when toggled through the same
  `SystemParametersInfo`-style mechanism this add-on already uses
  successfully for other touchpad settings, so this add-on does not attempt
  to toggle it automatically. If this bothers you, turn it off yourself in
  Windows Settings (accepting the restart) - it isn't undone when you turn
  trackpad-as-touchscreen mode off, since this add-on never touches it.
- This add-on also temporarily turns off NVDA's "report object under mouse
  pointer" setting (NVDA Settings > Mouse) while trackpad-as-touchscreen
  mode is on, as a second layer of protection alongside the cursor freeze
  above. Your normal setting is restored exactly as it was when you turn
  trackpad mode back off.
- If your laptop has a real touchscreen *in addition to* a trackpad,
  turning on trackpad-as-touchscreen mode temporarily takes over as the
  active touch input source - the real touchscreen won't respond to touch
  while trackpad mode is on. Turning trackpad mode back off immediately
  restores the real touchscreen, no restart needed.
- Multi-finger contact tracking depends on your trackpad's own hardware/
  driver correctly reporting simultaneous contacts and an accurate contact
  count; this varies somewhat by manufacturer.

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
- The multi-finger tap fix raises `touchTracker.maxAccidentalDrift` (how far
  a finger may move during a tap before NVDA stops considering it a tap) from
  its default of 10px to 25px. If taps still misdetect on your hardware, or
  if genuine small drags start being misread as taps, adjust the value in
  `touchExplore/globalPlugins/touchExplore/__init__.py`.
