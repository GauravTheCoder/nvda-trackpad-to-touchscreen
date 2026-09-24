# Touch Explore Sounds (NVDA add-on)

Improves NVDA's touch explore-by-touch feedback:

- Plays a short sound (earcon) when your finger lands on a real, actionable
  item (icon, list item, button, link, cell, etc), in addition to NVDA's
  normal spoken announcement of that item - a different one for buttons,
  links, edit fields and check boxes. The sound is also placed where the
  item is on screen: panned left or right, and higher or lower in pitch the
  higher or lower on the screen it is. Flicking to an item plays its sound too. See "Sounds"
  below.
- Plays an activation sound (no extra speech) whenever you activate an item -
  split-tap (see below) or a same-spot double-tap - confirming the action
  happened without adding to what NVDA already says.
- Adds touch gestures for stopping speech, turning speech off and on,
  scrolling, media play/pause and speech rate. See "Extra touch gestures"
  below.
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
  instead of firing on every item regardless of context. This also applies
  when flicking through items (stock NVDA's own flick gestures only move
  NVDA's review/navigator position, not real focus or selection, which is
  why "not selected" would otherwise fire on every flicked-to item too).
- Adds a split-tap activation gesture: hold one finger on a touch-explored
  item, then tap anywhere else on the screen with a second finger to
  activate that item (its default action - e.g. open it), equivalent to
  double-tapping the item itself without needing to lift and re-tap the
  same exact spot.
- Fixes multi-finger taps sometimes being detected with fewer fingers than
  actually used (e.g. a 3-finger tap registering as a 1- or 2-finger tap).
- Adds a trackpad-as-touchscreen mode (**NVDA+Ctrl+Shift+T** to toggle): on a
  laptop with no touchscreen, reads your trackpad's own multi-touch surface
  directly and maps it onto the whole monitor you're working on, so every touch gesture above -
  plus NVDA's own built-in touch gestures (explore, tap, flick, multi-finger
  taps, mode-cycling, etc) - work from the trackpad exactly as they would on
  real touchscreen hardware. While this mode is on, the add-on also turns off
  the OS's own trackpad gestures (tap-to-click, two-finger tap, pinch/pan,
  the corner right-click zone) that would otherwise fire at the same time
  from the same fingers, and restores your original trackpad settings when
  you turn the mode back off. See "Trackpad-as-touchscreen mode" below for
  requirements and limitations.
- Lets you adjust how touch gestures are recognised, measured in
  millimetres so they feel the same on any screen or trackpad, and includes
  a calibration that measures your own taps and flicks. See "Touch settings
  and calibration" below.

## Sounds

Sounds are set in NVDA Settings > **Touch Explore** > Sounds:

- **Play sounds**: turn all of this add-on's sounds on or off.
- **Different sounds for buttons, links, edit fields and check boxes**: when
  off, every item uses the same sound.
- **Sound when your finger moves off an item into empty space**: a faint
  tick, played once as your finger leaves an item for empty space (such as
  the gap between desktop icons), so you can tell empty space from "still on
  the same item". Inside lists it can also tick briefly between rows.
- **Play sounds left or right by where they are on the screen**.
- **Play sounds higher or lower by how high up the screen they are**: at the
  top edge a sound plays 6 semitones (half an octave) higher, at the
  bottom edge 6 lower, and in the middle unchanged. Together with the left/
  right setting, you can hear roughly where on the screen an item is.
- **Sound pack**: "Earcons" (default) or "Classic" (this add-on's original
  explore and click sounds, shortened).
- **Preview sounds** plays the item, button, link, edit field, check box and
  activation sounds of the selected pack, moving from the top left of the
  screen to the bottom right (so you hear the panning and pitch settings if
  they're ticked).

Other sounds: a low "bong" when a flick reaches the first or last item ("No
next"/"No previous"); a sound for scrolling; rising/falling sounds when
speech or trackpad touchscreen mode is turned on or off. Sounds follow
NVDA's own sound volume setting.

The Earcons pack is made from Kenney's "Interface Sounds"
(https://kenney.nl/assets/interface-sounds), released as CC0 (public
domain), so the add-on can include and redistribute them. To hear all the
sounds with their names spoken, followed by demonstrations of the height
(pitch) and left/right placement, play `resources/sounds/earcon-audition.wav`
(not included in the add-on package). `resources/sounds/build_sounds.py`
rebuilds the packs from the original downloads.

## Extra touch gestures

| Gesture | Action |
|---|---|
| Two-finger tap | Stop speech |
| Three-finger double tap | Turn speech off, or back on |
| Three-finger flick up / down | Page Down / Page Up (like VoiceOver: flicking up shows what's further down) |
| Two-finger triple tap | Media play/pause |
| Pinch out / in | Faster / slower speech (5 steps each) |

All of these can be changed in NVDA's Input Gestures dialog, under Touch
Explore Sounds. That category also lists two commands with no gesture by
default:

- **Touch calibration** (see below).
- **Copy touch diagnostics to the clipboard**: details of your touch
  hardware and settings, to paste into a bug report.

In trackpad touchscreen mode, Windows may take three-finger gestures for
itself; see "Notes / limitations".

## Touch settings and calibration

NVDA Settings > **Touch Explore** has separate settings for the touchscreen
and the trackpad:

- **Tap movement tolerance (mm)**: how far a finger may move and still count
  as a tap. Raise it if taps (especially two- and three-finger taps) are
  missed or come out as the wrong number of fingers.
- **Minimum flick distance (mm)**: how far a finger must travel for a flick.
  Lower it if short flicks are ignored. It must be at least 1.5 times the
  tap tolerance.
- **Minimum pinch distance (mm)**: how much two fingers must spread or close
  for a pinch.
- **Gesture time (ms)**: the longest a tap or flick may take, and also how
  quickly the second tap of a double tap must start. Raise it if your
  gestures are slow or double taps come out as two single taps. A single
  tap waits this long before it acts, so lower values make single taps
  respond faster.

The defaults match this add-on's earlier fixed behaviour.

**Calibrate touch** (a button in the same panel; the command can also be
assigned a gesture in Input Gestures, under Touch Explore Sounds) measures
how you actually touch, then offers new settings:

1. It calibrates whichever touch input is active: the touchscreen, or the
   trackpad if trackpad touchscreen mode is on.
2. It speaks each step: 8 single taps, 5 two-finger taps, 5 double taps and
   8 flicks, done the way you normally would. A high beep and a count mean
   the gesture was counted; a low beep and "Try again" mean it didn't match
   the step. You can skip any step; the settings that step would have
   measured stay as they are.
3. During these steps, touch gestures don't do anything, so tapping can't
   activate anything by accident. Press Escape to cancel at any time.
4. At the end it reads out the old and new values. Press Save (focused, so
   Enter works) to use them, or Cancel to keep your old settings.

## System requirements

- NVDA 2023.1 or later (see `minimumNVDAVersion` in
  `touchExplore/manifest.ini`), which in turn means any Windows version NVDA
  2023.1 itself supports (Windows 8.1 or later) for the core add-on - the
  container/role/selection fixes, split-tap, and the real-touchscreen
  multi-finger fix all use only standard, version-independent MSAA/UIA/touch
  APIs.
- Trackpad-as-touchscreen mode's raw-input HID digitizer reading works on
  any Windows version with a Precision Touchpad, but its OS-gesture
  minimization step specifically requires Windows 11 24H2 (build 26100+) -
  see "Trackpad-as-touchscreen mode" below.

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

With more than one monitor, the trackpad maps onto whichever monitor holds
the window you're currently working in, chosen each time a new gesture
starts (it doesn't switch monitors mid-gesture). Exception: if NVDA's own
"edge gestures" touch setting is on (NVDA versions that have it), the
trackpad always maps onto the primary monitor, because NVDA only detects
edges on the primary monitor.

Palm touches that the trackpad itself flags as accidental are ignored. If
more than one Precision Touchpad is connected (e.g. built-in plus external),
both work, and one can be plugged in or removed while this mode is on. If
your trackpad isn't a Precision Touchpad, NVDA says "No Precision Touchpad
found" when you try to turn this mode on.

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
- **Known limitation: touch-explore can still narrate an app's content after
  you've switched away to a different virtual desktop, for certain apps.**
  Confirmed with WhatsApp Desktop (the Microsoft Store version): after
  opening it on one virtual desktop and switching to a different, empty
  desktop, swiping/touch-exploring on that other desktop can still speak
  WhatsApp's content. Root cause: WhatsApp Desktop is packaged as an MSIX
  app that hosts its UI in a WebView2 control - the window touch-explore
  actually hits belongs to a separate helper process (`msedgewebview2.exe`)
  from WhatsApp's own real application window (`WhatsApp.Root.exe`), with no
  Win32 parent/owner relationship between the two. Windows' own
  `IVirtualDesktopManager` COM API (the standard way to check which virtual
  desktop a window belongs to) gives the correct answer for WhatsApp's real
  window but an incorrect one for its WebView2 helper window - which is the
  window actually hit-tested here - so this add-on cannot reliably tell that
  window apart from one that's genuinely on the current desktop. This
  appears to be a genuine Windows/WebView2 defect, not specific to this
  add-on: a similar WebView2 window-state bug for this same app is
  independently reported and still open upstream
  (`MicrosoftEdge/WebView2Feedback#5668`), and Chromium's own documentation
  states plainly that Windows gives no notification when a window changes
  virtual desktops, so even Chromium re-derives this state defensively
  rather than trusting it live. A fix was attempted (walking the process
  tree from the helper process to find WhatsApp's real window, then
  querying that instead) and did work for one confirmed hwnd during
  testing, but was not reliably re-verified across repeated repro attempts
  before this line of investigation was stopped as too deep/costly relative
  to how narrow the impact is - the real focus-stealing side effect of this
  same bug for *flick*-based navigation (as opposed to plain touch-explore
  narration) is separately fixed, see `virtualDesktop.py`. If you hit this,
  the workaround is to close the affected app rather than just switching
  virtual desktops away from it.
