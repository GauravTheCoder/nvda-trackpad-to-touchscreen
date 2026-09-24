# touchExplore NVDA add-on

Global plugin that patches `screenExplorer.ScreenExplorer.moveTo` (touch
explore-by-touch) to fix three stock NVDA annoyances: container chatter
("19 items" repeating between icons), redundant role announcements
("Recycle Bin, list item"), and selection state that doesn't match reality
("not selected" on every item because touch never actually selects
anything). It also adds a VoiceOver-style split-tap gesture (hold one
finger on an item, tap elsewhere with a second finger to activate it,
silently — same as a regular double-tap), and fixes multi-finger taps
sometimes being detected with fewer fingers than actually used (e.g. a
3-finger tap registering as 1 or 2 fingers). It also adds a
trackpad-as-touchscreen mode (NVDA+Ctrl+Shift+T, see `trackpadTouch.py`)
that reads a laptop trackpad's own raw multi-touch HID contacts and feeds
them into NVDA's real touch pipeline, so every touch gesture works from a
trackpad on hardware with no touchscreen. See README.md for user-facing
behavior; this file is implementation notes and NVDA internals that were
expensive to (re)discover.

## Source of truth

There is no NVDA Python source on this machine — NVDA ships as compiled
`.pyc` inside `library.zip`. All NVDA internals below were confirmed by
fetching the real source from GitHub. Don't guess at NVDA API
behavior/signatures from memory — fetch the actual source and quote it
verbatim; a paraphrased summary from a fetch has already been wrong once
here, and fetching from a stale tag has *also* already been wrong once here
(see history below) — **fetch from `https://raw.githubusercontent.com/
nvaccess/nvda/master/source/...`, not a pinned old release tag**, unless you
have first confirmed the tag actually matches the installed NVDA version
(check `lastTestedNVDAVersion` in `touchExplore/manifest.ini` as a hint, but
that's this add-on's own claim, not authoritative for what's actually
installed - if in doubt, ask the user to check NVDA's "About" dialog for the
exact running version). `touchHandler.py` gained a real, gesture-ID-breaking
API change (`TouchMode` enum replacing plain mode strings) between the
`release-2025.3` tag this project was first developed against and the
version actually running on this machine - see "Trackpad-as-touchscreen
mode" below. Useful files when revisiting this:

- `source/speech/speech.py` — `speakObject`, `getObjectSpeech`,
  `getObjectPropertiesSpeech`, `getPropertiesSpeech`.
- `source/controlTypes/role.py` — `silentRolesOnFocus`.
- `source/controlTypes/processAndLabelStates.py` — `_processNegativeStates`
  (the "not selected" logic).
- `source/NVDAObjects/__init__.py` — base `setFocus()`,
  `getSelectedItemsCount()`.
- `source/NVDAObjects/IAccessible/__init__.py` — MSAA `setFocus()` (uses
  `accSelect(SELFLAG_TAKEFOCUS, childID)` — **focus only, not selection**).
- `source/NVDAObjects/UIA/__init__.py` — UIA `setFocus()` (raw
  `IUIAutomationElement::SetFocus`, focus only) and
  `UIASelectionItemPattern` (`_get_UIASelectionItemPattern`, used by
  `doAction()` for `select()`).
- `source/oleacc.py` — `SELFLAG_*` constants.
- `source/touchHandler.py` — `TouchHandler` (module singleton `handler`,
  owns `.screenExplorer`), `TouchInputGesture` (`_get_identifiers`,
  `notifyInteraction`).
- `source/touchTracker.py` — `MultiTouchTracker`/`TrackerManager`, the
  `preheldTracker` concept (one finger held while another group
  touches/taps — this is what a split-tap gesture is built from), and
  `maxAccidentalDrift`/`SingleTouchTracker.update()` (tap vs. hold/hover
  classification, and where multi-finger tap misdetection actually lives —
  see below).
- `source/globalCommands.py` — `script_review_activate` (`ts:double_tap`),
  the reference implementation for "activate the current object" (doAction
  + parent walk + notifyInteraction + ui.message). Our split-tap script
  mirrors the doAction/parent-walk/notifyInteraction parts but
  deliberately drops the ui.message() feedback — a regular double-tap is
  silent in this setup, so split-tap activation matches that rather than
  announcing "Activate"/"No action".

For the trackpad-as-touchscreen feature specifically, the relevant NVDA
internals are the same `touchHandler.py`/`touchTracker.py` files above (this
feature drives them from a different input source, it doesn't change their
logic), plus Microsoft's own (non-NVDA) documentation — fetched and
independently re-verified against real hardware via ctypes probe scripts,
not trusted from docs prose alone (see "Trackpad-as-touchscreen mode"
below for what was actually confirmed and how):
`learn.microsoft.com/en-us/windows-hardware/design/component-guidelines/touchpad-windows-precision-touchpad-collection`
(the HID usage table for touchpad input reports) and
`learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-touchpad_parameters_v1`
(the `SPI_*TOUCHPADPARAMETERS` struct).

## Key NVDA facts learned the hard way

- **`speech.speakObject(obj, reason=...)` role suppression is automatic**,
  not something you pass a flag for. `reason=OutputReason.FOCUS` makes NVDA
  consult `controlTypes.silentRolesOnFocus` (includes `LISTITEM`,
  `TREEVIEWITEM`, `MENUITEM`, `TABLECELL`, etc.) and drop the role when a
  name is already present. There's no `role=False`/`_role=False` kwarg on
  `speakObject` — I guessed that once and it doesn't exist.
- **Focus and selection are separate concerns**, and which API couples them
  depends on the accessibility backend:
  - MSAA/IAccessible (e.g. the Windows desktop, classic Explorer
    `SysListView32`): `obj.setFocus()` calls `accSelect(SELFLAG_TAKEFOCUS,
    childID)` — this moves focus but does **not** select. Actually
    selecting (and deselecting whatever else was selected, for a
    single-select container) requires `accSelect(SELFLAG_TAKEFOCUS |
    SELFLAG_TAKESELECTION, childID)` — flag value `3` — called directly on
    `obj.IAccessibleObject` with `obj.IAccessibleChildID`.
  - UIA: `obj.setFocus()` calls raw `SetFocus()`, focus only. Selection
    needs `obj.UIASelectionItemPattern.select()` (a cached property, `None`
    if the pattern isn't supported), which also happens to move focus as
    part of selecting.
  - There's no unified "select me" method on the base `NVDAObject` — always
    branch on which pattern/interface is actually present
    (`getattr(obj, "UIASelectionItemPattern", None)` first, then
    `getattr(obj, "IAccessibleObject", None)`).
- **`_processNegativeStates`** only speaks "not selected" for roles
  `LISTITEM, TREEVIEWITEM, TABLEROW, TABLECELL, TABLECOLUMNHEADER,
  TABLEROWHEADER, CHECKBOX` when `SELECTABLE + FOCUSABLE` are both present
  and `reason == FOCUS` (or `CHANGE` while focused) — it does **not**
  condition on how many siblings are selected. So "not selected" fires on
  every single-selection browse regardless of context; the only real fix is
  making the touched item genuinely selected (matches how it reads), not
  fighting the speech layer.
- **Calling the real OS focus/selection API is genuinely async from NVDA's
  perspective**: `obj.setFocus()` / `accSelect()` / `ISelectionItemProvider
  ::Select()` change real OS state, which NVDA's own event hooks
  (winEvent/UIA) pick up independently and announce through the normal
  focus pipeline. Don't also call `speech.speakObject()` yourself right
  after — that double-announces every item. Let NVDA's own pipeline speak
  it; it already gets role suppression and selection-state right once the
  touched item is genuinely selected.
- Desktop icons and Explorer icons on this machine are `role=15`
  (`controlTypes.Role.LISTITEM`), MSAA-backed (`UIASelectionItemPattern` is
  `None`), even on Windows 11 ARM64 — don't assume the shell is UIA.
- **NVDA's touch gesture IDs already have a "held finger(s) + second
  action" concept** (`touchTracker.TrackerManager`/`preheldTracker`) — no
  need to hand-roll multi-touch state tracking for gestures like
  VoiceOver's split-tap. One finger held (hover) + a second finger's single
  tap produces the standard, bindable gesture ID
  `ts(object):1finger_hold+tap` (also `ts:1finger_hold+tap` without the
  finger count, and `ts:hold+tap` — see `_get_identifiers` for the exact
  format: `ts(<mode>):<preheldCount>finger_hold+<tapAction>`). Bind it like
  any other gesture via `@script(gestures=(...))` from `scriptHandler`.
  `mode` is `touchHandler.handler._curTouchMode`, `"object"` by default (as
  opposed to browse-mode "text" touch typing).
- The currently touch-explored object is reachable globally as
  `touchHandler.handler.screenExplorer._obj` — useful for a script that
  needs "whatever item is under the held finger right now" without relying
  on `api.getNavigatorObject()`/review position being in sync (they may not
  be, depending on `updateReview`/timing).
- `TouchInputGesture` is dispatched through the completely standard
  `inputCore.manager.executeGesture()` path — nothing special-cased. Any
  `ts(...)`/`ts:...` ID is bindable from a global plugin exactly like a
  keyboard gesture.
- **Multi-finger taps misdetecting with fewer fingers than actually used is
  a drift-threshold problem, not a timing/merge-window problem.**
  `SingleTouchTracker.update()` only classifies a completed touch as
  `action_tap` if it stayed within `touchTracker.maxAccidentalDrift` (10px
  default) of its start point for its *entire* duration; a finger that
  drifts further is left as `action_unknown` forever (there's no
  reclassification once the touch completes). Critically,
  `TrackerManager.update()` only forwards a tracker to
  `processAndQueueMultiTouchTracker`/`makeMergedTrackerIfPossible` when its
  action becomes non-unknown (`if newAction != oldAction and newAction !=
  action_unknown`) — so a finger stuck at `unknown` never even reaches the
  merge logic, silently dropping out of the gesture instead of erroring.
  With 2-3 simultaneous fingers it's normal for at least one to drift more
  than a single careful finger would (observed up to ~19px in testing),
  so this fires often. The merge logic itself (pure
  `[startTime, endTime]` interval overlap, no grace padding) is *not* the
  problem — multi-finger flicks, which have no drift ceiling at all, always
  merged correctly in the same test session. Fix: raise
  `touchTracker.maxAccidentalDrift` (this add-on uses 25). It's a plain
  module-level global read by name at call time, so reassigning
  `touchTracker.maxAccidentalDrift` from outside the module after import
  is sufficient — no need to monkeypatch any function.
  - **This description of `SingleTouchTracker.update()` matches the
    `release-2025.3` snapshot this add-on was first developed against, but
    NOT the version actually running when the trackpad-as-touchscreen
    feature was developed/debugged later** — re-fetch and re-read the
    current method body verbatim before relying on this description again;
    don't assume it still matches. Confirmed differences on the later
    version: flick detection is velocity-based (a rolling sample window,
    `getVelocity()`, `minFlickVelocity`) rather than purely
    distance/direction-based; `TouchAction`/`TouchEdge` are enums, not
    plain string constants; and — the one that actually mattered for a
    later bug (see "Trackpad-as-touchscreen mode" below) —
    `self.action = TouchAction.HOVER` fires unconditionally on **any**
    `update()` call, not just on lift, the instant
    `multitouchTimeout` has elapsed, which is a materially different
    failure mode than "only misclassifies at completion."

## Trackpad-as-touchscreen mode (`trackpadTouch.py`, `touchpadOsSettings.py`)

Everything below was confirmed against real Windows Precision Touchpad
hardware (a Microsoft-driver PTP, Vendor 0x045E Product 0x0C77, Windows 11
24H2 build 26200 ARM64) via standalone ctypes probe scripts before being
used in the add-on — several points below directly contradict what the
official Microsoft docs alone would lead you to implement; do not skip
re-verifying against real hardware if revisiting this on different hardware
or a different Windows version.

- **`touchHandler.handler` does not exist on a machine with no real
  touchscreen digitizer.** `touchHandler.touchSupported()` requires
  `GetSystemMetrics(SM_MAXIMUMTOUCHES) > 0`, which is 0 with only a
  trackpad present, so `touchHandler.setTouchSupport()`/`initialize()` never
  construct the module singleton. There is nothing to piggyback on — the
  entire touch pipeline (`TrackerManager`, `ScreenExplorer`,
  `TouchInputGesture`, `inputCore.manager.executeGesture`) has to be driven
  independently, using the same classes but instances this add-on owns.
- **NVDA's own built-in touch scripts hardcode
  `touchHandler.handler.screenExplorer.moveTo(...)`** (see
  `globalCommands.py`'s `script_touch_newExplore`/`script_touch_explore`/
  `script_touch_changeMode`) — they reference the module-level singleton
  directly, not whatever object a gesture happened to come from. This means
  making trackpad-driven touch-explore narration work isn't just "feed
  gestures into `inputCore`" — **the add-on's own `TrackpadTouchScreen`
  instance must actually be installed as `touchHandler.handler` itself**
  while active (and the previous value, normally `None`, restored on
  disable), or those stock scripts crash on `None.screenExplorer`. Once
  installed, they work completely unmodified — no need to reimplement
  explore-by-touch narration.
- **A Windows trackpad exposed as a mouse only ever reports one cursor
  position at a time** — Windows' precision-touchpad driver consumes
  multi-finger gestures (2/3/4-finger swipes, pinch, etc) itself before they
  reach an application as distinct points, so there is no way to get real
  multi-touch data through mouse/cursor APIs. Genuine multi-finger detection
  requires reading the touchpad's own HID digitizer top-level collection
  (Usage Page `0x0D`, Usage `0x05`) directly via the Raw Input API
  (`RegisterRawInputDevices`/`WM_INPUT`/`GetRawInputData`), which runs in
  parallel to (not instead of) whatever the OS does with the same physical
  contacts.
- **The touchpad enumerates as *two* separate raw-input HID devices with
  the same UsagePage/Usage/VendorId/ProductId**, confirmed via
  `GetRawInputDeviceList` + `GetRawInputDeviceInfo(RIDI_DEVICEINFO)`: a real
  PnP device node (name like `\\?\HID#MSHW0238&Col06#...`, openable via
  `CreateFileW`) and a synthetic device (name like
  `\\?\Microsoft HID RID\000D_0005\2`) that `WM_INPUT` messages actually
  arrive from. `RAWINPUTHEADER.hDevice` in a `WM_INPUT` message is always
  the *synthetic* device's handle. `CreateFileW` on the synthetic device's
  own name fails with `ERROR_PATH_NOT_FOUND` (error 3) — it must never be
  opened directly.
  - The synthetic device has **its own, separate preparsed HID data**,
    fetchable directly from its raw-input handle via
    `GetRawInputDeviceInfo(RIDI_PREPARSEDDATA)` — no `CreateFileW`/
    `HidD_GetPreparsedData` needed for it at all. On the test hardware this
    device's reports are 182 bytes with Report ID `1`.
  - Its PnP sibling has a **different** report layout entirely (confirmed:
    50-byte reports, Report ID `129`/`0x81`) — the two are independent HID
    report descriptors that happen to share a UsagePage/Usage/VID/PID, not
    two views of the same report. Calling `HidP_GetUsageValue` against the
    wrong one's preparsed data fails with `HIDP_STATUS_INCOMPATIBLE_REPORT_ID`
    (`0xC0110003`) — confirmed by direct instrumentation after initially
    (wrongly) assuming they were interchangeable.
  - `RIDI_PREPARSEDDATA` on the raw-input handle is therefore the correct
    primary path; opening the PnP sibling via `CreateFileW` +
    `HidD_GetPreparsedData` is kept in the add-on only as a fallback for
    hardware where `RIDI_PREPARSEDDATA` isn't available on the synthetic
    device (not observed on the test hardware, but not guaranteed
    elsewhere) — and if that fallback path is ever actually exercised, its
    report layout must be re-verified independently; don't assume it
    matches the synthetic device's.
  - `CreateFileW` on the PnP sibling needs `dwDesiredAccess=0`
    (capability-query-only, no read/write) — `GENERIC_READ|GENERIC_WRITE`
    fails with `ERROR_SHARING_VIOLATION` (error 32) because the OS's own
    touchpad driver already holds the device open for read/write.
- **CORRECTION (see "Hardware-universality pass" below): this bullet is
  wrong.** Tip Switch (`0x42`) and Confidence (`0x47`) are 1-bit *button*
  usages, so they never appear in `HidP_GetValueCaps` output - the dump this
  conclusion was drawn from. A `HidP_GetButtonCaps` probe shows this pad
  declares Tip, Confidence and In Range (`0x32`) on every contact link
  collection. Original (incorrect) text kept below for history:
- **This hardware's HID report does not include a Tip Switch usage
  (`0x0D`/`0x42`) at all**, despite Tip Switch being documented as
  *mandatory* for a spec-compliant Windows Precision Touchpad (see
  `HidP_GetValueCaps` output: only Contact ID `0x51`, X `0x01/0x30`, Y
  `0x01/0x31`, Width `0x48`, Height `0x49`, Azimuth `0x3f`, and Pressure
  `0x0D/0x30` are present per contact link collection). Gating "is this
  contact live" on Tip Switch presence therefore silently discards every
  contact on this hardware. The correct, working approach: use the
  device-level Contact Count usage (`0x0D`/`0x54`, link collection `0`) —
  it's authoritative per the Windows Precision Touchpad spec, and the first
  N link collections (by ascending `LinkCollection` number = reported slot
  order) are the live contacts; higher-numbered slots keep reporting their
  *last real* X/Y (not zeros) once a finger lifts, so treating "has an X/Y
  value" as "is live" also produces stale phantom contacts if not gated by
  contact count.
- **Don't confuse Pressure and Tip Switch** — Pressure is `0x0D`/`0x30`;
  Tip Switch is `0x0D`/`0x42`. Easy to mix up since 0x30 is also the X usage
  on a *different* page (`0x01`/`0x30`), and both are plausible-looking
  small hex numbers near each other in the Windows Precision Touchpad
  Collection reference. Confirmed against
  `touchpad-windows-precision-touchpad-collection` on Microsoft Learn, not
  memory.
- Raw input device handles are **not stable** across process/thread
  restart — always re-resolve by UsagePage/Usage (and VendorId/ProductId
  for the PnP-sibling fallback path) via a fresh `GetRawInputDeviceList`
  call each time, never cache a handle value across a stop/start cycle.
- **ctypes' default (untyped) argument/return marshaling silently breaks on
  64-bit pointer/handle values** — confirmed directly, twice, while
  developing this: `kernel32.GetModuleHandleW` with no declared `restype`
  returns a 32-bit-truncated garbage value for a >2GB module base address
  (no exception — it just silently returns the wrong number); separately,
  passing that same class of large handle value into `user32.CreateWindowExW`
  with no declared `argtypes` raises `ctypes.ArgumentError: ... OverflowError:
  int too long to convert`. Every Win32 function this module calls has
  explicit `argtypes`/`restype` set for exactly this reason, including ones
  `touchHandler.py` itself also calls untyped (`CreateWindowExW`,
  `RegisterClassExW`, `GetMessageW`, `DefWindowProcW`, `GetModuleHandleW`,
  etc) — since ctypes `argtypes`/`restype` assignments live on the function
  object and are process-global, but a *correct*, fully-general type
  declaration only accepts values a legitimate caller would pass anyway, so
  this cannot break `touchHandler.py`'s own calls to the same functions.
- `RAWINPUTHEADER.hDevice` **can be `0` for input from a precision
  touchpad** per Microsoft's own docs remarks — not hit in this add-on's
  testing (a real nonzero synthetic-device handle was always present), but
  worth remembering if a future device behaves differently; code that
  assumes `hDevice` is always truthy could misbehave on such hardware.
- `TOUCHPAD_PARAMETERS_V1`'s exact bit-field layout (which named boolean
  lands at which bit, across its two `BOOL:1` bitfield words) is documented
  by Microsoft only as field *order*, not bit position, and
  `TOUCHPAD_PARAMETERS_VERSION_1`'s numeric value isn't documented at all —
  both were taken from a real, tested community C#/PInvoke reference
  (`flcdrg/reinstall-windows`'s `Set-Touchpad.ps1`, using .NET
  `BitVector32` section masks) and independently re-verified by running a
  read-only `SPI_GETTOUCHPADPARAMETERS` probe against this machine and
  confirming it read back this machine's actual current touchpad settings
  correctly (`touchpadPresent=True`, live `tapEnabled`/`panEnabled`/etc
  values matching Windows Settings) before being trusted for the
  minimize/restore write path. `SPI_SETTOUCHPADPARAMETERS` requires Windows
  11 24H2 (build ≥ 26100) — `touchpadOsSettings.isSupported()` detects this
  by checking whether `SPI_GETTOUCHPADPARAMETERS` succeeds and reports a
  touchpad present at all, and every public function in that module is a
  silent no-op if it doesn't.
- `SystemParametersInfoW`'s `pvParam` argument must be typed `c_void_p`
  (or another pointer-ish ctypes type), not `c_int` — passing a struct via
  `byref()` into a `c_int`-typed parameter raises `ArgumentError: wrong
  type` immediately.

### Post-release bug: missing `pump()` broke NVDA speech entirely, needed a restart

First real-world use after shipping (not caught by the standalone
pre-release testing above, because that testing exercised the HID
capture/decode pipeline directly and never ran under an actual NVDA
process) surfaced two compounding bugs, both now fixed:

1. **`TrackpadTouchScreen` had a private `_pump()` method but no public
   `pump()`.** `core.py`'s `CorePump.Notify()` — NVDA's own central
   "check the queues and execute functions" timer callback, which runs
   continuously - unconditionally calls `touchHandler.handler.pump()` every
   single cycle whenever `touchHandler.handler` is truthy:
   ```python
   if touchHandler.handler:
       touchHandler.handler.pump()
   JABHandler.pumpAll()
   IAccessibleHandler.pumpAll()
   queueHandler.pumpAll()   # <-- drains NVDA's speech queue
   mouseHandler.pumpAll()
   braille.pumpAll()
   ...
   ```
   Since this add-on installs itself as `touchHandler.handler` while
   trackpad-as-touchscreen mode is active (see above), and that object had
   no `pump()`, **every core pump cycle raised `AttributeError:
   'TrackpadTouchScreen' object has no attribute 'pump'`**. `Notify()`
   wraps the whole block in a bare `except Exception: log.exception(...)`,
   so NVDA didn't crash - but the exception aborted every step *after* the
   failed call within that `try`, including `queueHandler.pumpAll()`. That
   is the function that actually plays/drains NVDA's speech queue - so
   speech silently stopped working the moment trackpad-as-touchscreen mode
   was turned on, while everything queuing speech kept running normally
   (hence no visible error to the user, just silence, needing a full NVDA
   restart to recover since the core pump keeps hitting the same
   AttributeError indefinitely). Confirmed from the actual NVDA log
   (`%TEMP%\nvda.log`, or `nvda-old.log` for the previous run before a
   restart - **not** written to the NVDA profile folder under
   `%APPDATA%\nvda`, and not something this add-on's own `log.debug` calls
   alone would have caught since the exception was in NVDA's own core, not
   this add-on's code): `grep "core.CorePump.Notify\|AttributeError" ` on
   the log showed the exact `AttributeError` repeating at roughly 4-9ms
   intervals for as long as the trackpad kept reporting contacts - matching
   the "very fast beeps" symptom exactly (every contact update still
   triggered a tone/gesture dispatch fine on its own; it was everything
   *downstream* in the same pump cycle, especially speech, that never ran).
   Fix: added a `pump()` method matching `touchHandler.TouchHandler.pump()`'s
   contract exactly (drain `trackerManager.emitTrackers()`, dispatch each as
   a `TouchInputGesture` via `inputCore.manager.executeGesture()`, manage
   `pendingEmitsTimer` for delayed multi-touch merge windows) - the old
   `_pump()` logic was moved here rather than rewritten, since it was
   already correct as far as it went; it just needed to be reachable by the
   name `core.py` actually calls.
2. **Gesture dispatch must happen on NVDA's main thread, not the background
   HID capture thread.** The HID capture thread's job is only to update
   `trackerManager` and call `core.requestPump()` - exactly mirroring how
   real `touchHandler.TouchHandler.inputTouchWndProc` (which also runs on
   its own dedicated thread) only calls `self.trackerManager.update(...)`
   then `core.requestPump()`, never `executeGesture()` directly. The actual
   dispatch happens later, when `core.py`'s main-thread `CorePump.Notify()`
   calls `pump()`. This add-on's `_handleRawInput()` (on the background
   thread) was fixed to stop calling gesture-dispatch logic directly and
   instead only update `trackerManager` + call `core.requestPump()`; `pump()`
   (called externally, on the main thread, by `core.py`) does the actual
   `executeGesture()` calls. This also matters because `pendingEmitsTimer`
   is a `gui.NonReEntrantTimer` (a `wx.Timer` subclass) - wx timers must be
   constructed and started/stopped on the wx GUI/main thread, which is safe
   for `pump()` (main-thread-only, by construction) but would not have been
   safe to do from the background capture thread.

Separately, **the specific NVDA version installed on this machine (newer
than the `release-2025.3` source this add-on was first developed against -
see the "Source of truth" section above) has replaced `touchHandler.py`'s
plain string touch-mode values (`"object"`, `"text"`) with a
`touchHandler.TouchMode` enum** (`TouchMode.OBJECT`, etc; also added
`TouchMode.BROWSE` for the browse-mode-tracking case, and the
`TouchAction`/`TouchEdge` enums in `touchTracker.py` replacing former
`action_tap`-style string constants, plus `TouchHandler._processGestures()`
replacing the old `pump()`-inline dispatch loop with the same emit logic
plus new sequential-flick-combining). `TouchHandler.__init__` now sets
`self._curTouchMode = TouchMode.OBJECT` (the enum member, not the string
`"object"`). Since `TouchInputGesture._get_identifiers()` builds gesture ID
strings with plain `"%s" % mode` string formatting, passing the enum
through unconverted produces malformed gesture IDs like
`ts(TouchMode.OBJECT):hover` instead of `ts(object):hover` - which then
never matches any `@script(gestures=("ts(object):...",))` binding (visible
directly in the NVDA log's `IO -
inputCore.InputManager.executeGesture` lines during the incident above).
Real, current `TouchHandler._processGestures()` normalizes this immediately
before constructing each gesture
(`modeStr = self._curTouchMode.value if isinstance(self._curTouchMode,
TouchMode) else self._curTouchMode`); this add-on's `pump()` does the same
thing more defensively (`getattr(self._curTouchMode, "value",
self._curTouchMode)`, which doesn't need to import/reference `TouchMode` at
all - works whether that enum exists on the running NVDA version or not,
since `minimumNVDAVersion` for this add-on is older than when `TouchMode`
was introduced).

### Post-release bug: window class never unregistered, second enable failed

Enabling trackpad-as-touchscreen mode, disabling it, then enabling it again
in the *same NVDA session* failed outright
(`OSError: RegisterClassExW failed for trackpad touch window`, surfaced to
the user as "Could not enable trackpad touchscreen mode"). Cause:
`TrackpadTouchScreen._run()` calls `RegisterClassExW` with a fixed class
name (`"touchExploreTrackpadTouchWindowClass"`) every time it starts, but
never called `UnregisterClassW` on the way out - per Microsoft's own docs,
"an application must destroy all windows created with the specified class"
before unregistering (already done, via `DestroyWindow`, in the existing
cleanup) and the class then stays registered for the life of the *process*
if never explicitly unregistered ("All window classes that an application
registers are unregistered when it terminates" - NVDA the process doesn't
terminate on an NVDA+Ctrl+Shift+T toggle, so the class genuinely leaked
across toggles). Fix: call `UnregisterClassW(classAtom, hInstance)` in the
`finally` block, after `DestroyWindow`. The class atom (not the class name
string) must be passed as `UnregisterClassW`'s `lpClassName` parameter with
"the atom ... in the low-order word ... the high-order word must be zero" -
satisfied by declaring that parameter `c_void_p` and passing the plain
integer atom value directly (matches the exact pattern real, current
`touchHandler.py` itself uses for this: `user32.UnregisterClass(cast(c_void_p(self._wca),
LPCWSTR), self._appInstance)`).

### Post-release bug: "Desktop" spoken between icons - not this add-on's own code

First reported as "the container-silencing fix doesn't work with trackpad
mode" - it looked exactly like a `_patchedMoveTo`/`_CONTAINER_ROLES`
regression, but temporary `log.debug(f"obj.name={obj.name!r}
obj.role={obj.role!r} containerHit={containerHit}")` instrumentation added
at the top of `_patchedMoveTo` proved otherwise: **every single "Desktop"
occurrence in the log had `containerHit=True`, i.e. the container-silencing
logic was working correctly and `_patchedMoveTo` itself never spoke it.**
The "Desktop" speech events in the log had no `_patchedMoveTo` debug line
immediately before them at all - conclusive that a different code path was
speaking it.

Root cause: a trackpad, however this add-on reads its raw HID digitizer
reports, is *also still, simultaneously, an ordinary mouse-class HID
device* - it keeps generating completely normal Windows `WM_MOUSEMOVE`
events in parallel, moving the real OS mouse cursor along with whatever
finger is being tracked via raw input. This machine has
`config.conf["mouse"]["enableMouseTracking"]` (NVDA's "report object under
mouse pointer" setting) on, so `mouseHandler.internal_mouseEvent()` (a
`winInputHook` callback, entirely independent of touch/raw-input) fires on
every one of those real mouse moves, and `mouseHandler.executeMouseMoveEvent()`
calls `eventHandler.executeEvent("mouseMove", mouseObject, ...)` - NVDA's
*own*, separate, `event_mouseMove` announcement pipeline, which has no
notion of this add-on's container-role suppression (that logic lives
entirely inside the patched `screenExplorer.ScreenExplorer.moveTo`, which
`event_mouseMove` never calls). So the real cursor drifting across the
desktop's icon-view container between icons - via ordinary mouse movement,
not touch-explore - got announced by NVDA's mouse-tracking feature exactly
as it would for a normal mouse user gliding over the same container.

This is a case of confirming the actual call site rather than assuming the
bug is in the code most recently touched: the instrumentation is what
turned "the container fix is broken" into "the container fix works fine,
something else entirely is speaking" - see the "Debugging workflow" entry
below for the general lesson.

Fix: `GlobalPlugin._enableTrackpadTouchScreen()` now saves
`config.conf["mouse"]["enableMouseTracking"]` and sets it `False` for the
duration of trackpad-as-touchscreen mode (mirroring exactly how
`touchpadOsSettings.minimizeOsGestures()`/`restoreOsGestures()` already
handles the OS's own touchpad-gesture settings), restoring the user's real
preference in `_disableTrackpadTouchScreen()`. This is a blunt fix (mouse
tracking is off for the whole duration trackpad mode is on, not just while
a finger is actually down) but matches the existing OS-gesture-minimization
design and needs no new synchronization with the HID capture thread.

### Known, accepted limitation: real touchscreen goes silent while trackpad mode is on

On a machine that has *both* a real touchscreen and a trackpad (confirmed:
`touchHandler.initialize()` logs "Touchscreen detected, maximum touch
inputs: 10" at NVDA startup, meaning `touchHandler.handler` is a real,
already-running `TouchHandler` instance *before* trackpad mode is ever
toggled - this add-on was originally developed assuming a trackpad-only
machine with no real touch hardware, where `touchHandler.handler` starts as
`None`), enabling trackpad-as-touchscreen mode replaces
`touchHandler.handler` with this add-on's `TrackpadTouchScreen` instance.
The real `TouchHandler`'s own background thread keeps running and keeps
calling `core.requestPump()` on genuine touchscreen input, but
`core.py`'s `CorePump.Notify()` now calls `touchHandler.handler.pump()` on
*this add-on's* object instead of the real handler's - so real touchscreen
contacts are tracked but never dispatched as gestures, and the touchscreen
goes silent, for as long as trackpad mode stays on.

This is accepted, current behavior, not a bug to fix - the user was asked
directly (real touchscreen + trackpad coexisting simultaneously vs. a
simple, reliable single-active-handler swap) and chose the simpler
behavior: only one of {real touchscreen, trackpad} is the active touch
input source at a time, matching a normal user's usage pattern (you're
either using one or the other in a given moment, not both at once). What
*is* fixed and confirmed working: `TrackpadTouchScreen.stop()` (called by
`_disableTrackpadTouchScreen()`) correctly restores `touchHandler.handler`
back to the real `TouchHandler` instance it saved on enable
(`self._previousHandler`), so the real touchscreen resumes working
immediately on toggling trackpad mode off, no NVDA restart needed - traced
directly in the log: mouse-tracking announcements (`config.conf["mouse"]`
restored) and the real touchscreen's own `event_mouseMove`/`ts(...)`
activity both resumed normally within seconds of "Trackpad touchscreen mode
off" being spoken, with the `TrackpadTouchScreen` thread's own
`RAWINPUTDEVICE`/window-class cleanup (see previous entry) completing
cleanly in between.

If coexistence (both touch sources active at once) is ever wanted later,
the design would need two separate `TrackerManager`/pump paths dispatched
from the same `touchHandler.handler.pump()` call (or some other way to let
`core.py`'s single `touchHandler.handler.pump()` call fan out to both the
real `TouchHandler.pump()` and this add-on's own), plus resolving the
contact-ID collision risk between two independent hardware sources feeding
gestures through the same `ts(...)` gesture-ID namespace.

### Design decision: freezing the real mouse cursor during trackpad mode

Silencing NVDA's mouse-tracking *announcements* (previous entry) doesn't
stop the real OS cursor from visibly moving (and, in principle, clicking
whatever it lands on) in parallel with a touch-explore swipe - the user
explicitly wanted the cursor to not move at all during a gesture, not just
to stop being announced. This needed a second, independent mechanism.

**What doesn't work**: `RIDEV_NOLEGACY` registered on this add-on's own
message-only window (the same window already used for the digitizer's
`RIDEV_INPUTSINK` registration) had **no effect at all** when tested in
isolation - confirmed empirically (98 distinct cursor positions recorded
during a 5-second window it should have frozen). Per Microsoft's own docs
remarks, `RIDEV_NOLEGACY` alone only suppresses legacy mouse messages
*while the registering window is in the foreground* - and a message-only
window (`HWND_MESSAGE` parent, the same kind `touchHandler.TouchHandler`
itself uses) is never the foreground window. **`RIDEV_NOLEGACY` must be
combined with `RIDEV_INPUTSINK`** to take effect from a background/
message-only window - confirmed empirically after adding it: 1 distinct
cursor position (frozen) during the same 5-second test.

**Scope**: `RIDEV_NOLEGACY`/`RIDEV_INPUTSINK` register against a *HID usage
class* (`usUsagePage=0x01, usUsage=0x02`, Generic Desktop/Mouse), not one
specific physical device - Microsoft's docs don't offer a way to scope this
to a single physical mouse. Registering it therefore freezes **every**
mouse device on the system, not just the trackpad, for as long as it's
registered (a real USB/Bluetooth mouse plugged in at the same time would
also freeze). This was surfaced to the user explicitly as a tradeoff (no
per-device scoping exists) before implementing, and accepted.

**Cleanup risk and its resolution**: this is the single riskiest piece of
Win32 code in this add-on - registering it and then failing to unregister
it (a crash, an unhandled exception before cleanup, NVDA being killed) would
leave the cursor frozen system-wide. Two things were verified directly,
empirically, rather than assumed, given the severity of getting this wrong:
1. **Normal explicit unregistration** (`RegisterRawInputDevices` with
   `RIDEV_REMOVE`, `hwndTarget=None`, same usage page/usage) reliably
   restores normal mouse behavior - confirmed via `GetCursorPos()` polling
   before/during/after a full register-freeze-unregister cycle.
2. **Abnormal termination** (the registering process hard-killed via
   `os._exit()`, deliberately skipping all Python `finally`/cleanup code,
   to simulate an actual crash) - the OS released the registration
   automatically the moment the process died; normal mouse movement resumed
   immediately with no sign-out/restart needed. This matches the general
   Windows pattern of per-process window-station resources (windows,
   window classes, message queues, hooks) being reclaimed on process exit,
   but was not assumed - it was tested directly before relying on it, given
   what "wrong" would mean for the user (a system-wide stuck cursor).
   **This means even an NVDA crash while trackpad mode is on and mouse
   suppression is active cannot leave the cursor stuck long-term** - it's
   released the moment the NVDA process itself actually terminates.

**Implementation**: the digitizer (`RIDEV_INPUTSINK` only) and mouse
(`RIDEV_NOLEGACY | RIDEV_INPUTSINK`) registrations are both done in a
single `RegisterRawInputDevices` call with a 2-element `RAWINPUTDEVICE`
array (required anyway per the docs: "Only one window per raw input device
class may be registered ... within a process"). Since
`RegisterRawInputDevices`'s atomicity across multiple array entries in one
call isn't documented (no statement either way on whether a failure partway
leaves some entries registered), the `finally` cleanup block
unconditionally attempts `RIDEV_REMOVE` for *both* usage classes regardless
of which register call(s) actually succeeded - removing something that was
never registered is a harmless no-op/failure, so there's no cost to being
unconditional here, only benefit (never skipping the mouse-unfreeze step
because some unrelated earlier step's state made a conditional check
evaluate false).

### Post-release bug: multi-finger taps and flicks never registered, only exploration worked

Reported as "multi-finger and flick gestures don't work, only touch
exploration does." Single-finger tap already worked, which ruled out a
`TrackerManager`/`TouchInputGesture` wiring problem outright and pointed at
something specific to fast/multi-finger timing.

**Diagnosis**: temporary `log.debug` instrumentation was added at two
points - in `_handleRawInput`, logging every contact-ID/position change (not
every report, to avoid flooding single-finger drag logs); and in `pump()`,
logging every emitted tracker's `action`/`numFingers`/`preheld`. Reading
the log back showed two distinct, separate root causes, not one:

1. **A slow, deliberate 2-finger drag never merges into a "2-finger"
   gesture, by design** - not a bug. `TrackerManager.emitTrackers()`
   maintains a `curHoverStack` and only ever emits `action_hover` for the
   single *most recently added* hovering contact
   (`singleTouchTracker = self.curHoverStack[-1]`); every other
   concurrently-hovering contact becomes the `preheldTracker` instead (the
   same "held finger(s) + second action" concept this add-on's own
   split-tap gesture is built on - see the top-level "Key NVDA facts"
   section). A continuous 2-finger *hover/drag* was never meant to produce
   a combined "2-finger hover" gesture at all, on real touchscreen hardware
   either - confirmed by re-reading `emitTrackers()`, not assumed.
2. **The real bug**: this hardware's touchpad HID driver stops sending
   reports *entirely* for a contact that isn't actively moving - confirmed
   directly from the log: a genuine, fast 2-finger tap attempt showed
   `_handleRawInput` receiving zero `WM_INPUT` messages for over a full
   second mid-gesture (both contacts present and nearly stationary, then a
   ~1.2s gap, then one contact reported as lifted). Meanwhile,
   `touchTracker.SingleTouchTracker.update()` on the currently-running
   NVDA version (a further API drift from the `release-2025.3` snapshot -
   see the top of this file) locks a contact's `action` to
   `TouchAction.HOVER` **unconditionally, on any `update()` call**, the
   moment `time.time() - self.startTime >= multitouchTimeout` (250ms
   default) - not just on lift, and this add-on's code only ever called
   `TrackerManager.update()` reactively, when a new HID report arrived. So
   during that ~1.2s silent gap, nothing re-evaluated the contact's state
   at all, and by the time the next real report (the lift) arrived, more
   than 250ms had already elapsed and the action was permanently `HOVER` -
   the tap/flick classification branch (`if complete: ...`) was never even
   reached, because the `else: self.action = HOVER` branch had already run
   on an earlier, ordinary (non-`complete`) `update()` call once the
   silent gap alone exceeded the timeout. This also explains why a
   single-finger *flick* worked in earlier testing: a flick involves
   continuous fast movement the whole time, so the touchpad never goes
   quiet mid-gesture the way it does for a comparatively stationary tap or
   held multi-finger contact.

**Fix, two parts, both needed**:
1. **`trackpadTouch.py`**: a `SetTimer`/`WM_TIMER` poll
   (`CONTACT_POLL_INTERVAL_MS = 20`) on the same message-only window
   already used for raw input, re-feeding every currently-down contact's
   *last known position* into `TrackerManager.update()` on a short fixed
   interval, independent of whether a new HID report has actually arrived.
   This closes the "hardware goes silent, nothing re-evaluates the
   time-based state" gap - `_handlePollTimer()` does not decode any new
   HID data, it purely re-asserts already-known contact positions so
   `update()` gets called often enough for its own internal
   `time.time()`-based logic to be evaluated promptly. `KillTimer` in the
   cleanup `finally` block (though `DestroyWindow` alone would also
   implicitly clean up any timers owned by that window).
2. **`__init__.py`**: `touchTracker.multitouchTimeout` (0.25s default)
   raised to 0.4s, patched/restored the same module-global way as the
   pre-existing `maxAccidentalDrift` fix (same rationale: a real human
   gesture, especially multi-finger, does not reliably complete within a
   quarter-second machine-tight window, and no amount of prompt polling
   changes that once the *real* gesture duration itself exceeds the
   window - the timer-poll fix above only helps when reports are missing
   during a gesture that itself would have finished in time; it can't
   rescue a gesture that is genuinely, humanly slower than the timeout
   allows). Deliberately scoped module-globally rather than only while
   trackpad mode is active (mirroring the drift-fix precedent) after
   confirming with the user that a more forgiving timeout for real
   touchscreen gestures too was an acceptable, even likely beneficial,
   side effect - not something to avoid.

### Post-release bug: flicks still didn't register even after the timeout/poll fix

The timer-poll and `multitouchTimeout` fixes above fixed multi-finger taps
and held-hover cases, but a subsequent report showed flicks specifically
still never registered - confirmed the user's finger genuinely left the
trackpad surface each time (ruled out by direct confirmation, and by the
fact the identical motion worked correctly on the real touchscreen).

**Diagnosis**: unconditional (every single report, not just on ID-set
change) `log.debug` logging of contact positions plus explicit
`log.debug` calls at every early-return branch in `_handleRawInput` (to
rule out a silently-swallowed report) showed the actual mechanism: a
genuine, fast, correctly-executed flick (measured ~5000px/sec, ~110ms
total, comfortably over both `minFlickDistance` and `minFlickVelocity`)
produced a clean sequence of real HID reports tracking the finger's
motion - then **nothing**. No further `WM_INPUT` at all, not even one
matching any of the added early-return diagnostics, for the following
~325ms, until this add-on's own timer-poll (not a new real report) finally
triggered a `pump()` that classified the stalled contact as `hoverdown`
instead of a flick.

The touchpad's HID driver does not send an explicit lift/contact-count-drop
report when a finger actually leaves the surface, if the finger was already
stationary (post-flick, at the end of the swipe motion) when it lifted -
the same "stops reporting once nothing is changing" behavior identified
for the earlier stationary-multi-finger-tap bug, but this time manifesting
on the lift itself, not just during a hold. Since
`touchTracker.SingleTouchTracker.update()` only ever classifies a contact
as a tap or flick at the moment it's told `complete=True` (see the
`update()` method excerpt above), and this add-on's own timer-poll
(`_handlePollTimer`, introduced for the earlier fix) was re-feeding the
stale position with `complete=False` indefinitely, a lifted contact was
being kept alive as "still touching" forever - permanently preventing the
one call that could have classified it correctly.

**The two failure modes (stationary hold, and lift-after-flick) are
indistinguishable from raw HID silence alone** - both produce zero reports
for an extended period, and only one of them should be treated as a lift.
Asked the user directly which to prioritize given that ambiguity: chosen to
treat a stalled contact as lifted quickly, favoring tap/flick gestures
working reliably over supporting an arbitrarily long, perfectly motionless
intentional hold (which was not the reported-broken behavior; a stationary
hold that happens to pause for longer than the timeout mid-gesture is the
accepted tradeoff).

**Fix**: `TrackpadTouchScreen` now tracks a separate
`_lastRealReportTime` timestamp per contact ID, updated only by genuine
HID reports in `_handleRawInput` - never by `_handlePollTimer`'s own
re-feeds, which would otherwise reset it and defeat the whole mechanism.
`_handlePollTimer` (renamed conceptually to also do lift-inference, same
method) checks each tracked contact's `_lastRealReportTime` against
`LIFT_INFERENCE_TIMEOUT_S` (0.08s, chosen per the tradeoff above) on every
poll tick; any contact stale past that is fed one final
`trackerManager.update(id, lastPos, lastPos, True)` (the lift NVDA needs to
see) and removed from tracking, rather than being re-fed again with
`complete=False`. Verified with standalone unit tests (isolating the
timing logic from real hardware) before live-testing on the actual
touchpad, since this touches core tracking state and a live-only test
alone wouldn't distinguish "fixed" from "coincidentally worked this one
time."

### Investigated and abandoned: disabling OS 3/4-finger touchpad gestures at the application level

A separate, known limitation (documented since the feature was first
built): Windows' own 3/4-finger touchpad gestures (3-finger tap opens
Start menu, 3-finger swipe switches virtual desktops) are not covered by
`touchpadOsSettings.py`'s existing `SPI_SETTOUCHPADPARAMETERS`-based
minimization, because `TOUCHPAD_PARAMETERS_V1` (the officially documented,
already-used struct - see above) has no field for them at all; they're
governed by an entirely separate Windows setting ("Three- and four-finger
touch gestures" in Settings > Bluetooth & devices > Touchpad).

Investigated whether that separate setting could be toggled
programmatically and live, the same way the existing OS-gesture
minimization already works: it's stored at
`HKCU\Control Panel\Desktop\TouchGestureSetting` (`REG_DWORD`, `1` =
system owns 3/4-finger gestures - the default and what this machine had -
`0` = handed to applications instead), and per Microsoft Q&A community
reports (no official Microsoft Learn documentation page exists for this
specific setting or a `SPI_SETGESTURE` action constant - one community
report even disputes the constant's own value, `0x009B` vs `0x2031` on
Windows 11 24H2) it can supposedly be applied live via
`SystemParametersInfo`/`WM_SETTINGCHANGE` without a sign-out.

**Tested directly rather than trusted**: wrote a throwaway script that set
the registry value to `0`, broadcast `WM_SETTINGCHANGE` for
`"Control Panel\\Desktop"`, and left it in that state (no fixed countdown -
self-paced, since a prior fixed-window test caught the user off guard and
wasted a cycle) for the user to test a real 3-finger tap/swipe against.
**Confirmed: the OS gestures still fired exactly as before** - Start menu
and desktop-switch still happened - matching the "may not work reliably"
caveat from the community report, not the "works live" claim. Restored the
registry value to its original `1` and re-verified via
`Get-ItemProperty` that the restore itself took effect.

**Conclusion**: this is not implementable as a live, reliable
per-toggle feature the way the existing OS-gesture minimization is -
building it into the add-on would add registry-write complexity and risk
for a change that (on this machine, and per the one third-party report
found) silently doesn't do anything without a sign-out/restart, which
defeats the point of toggling it alongside NVDA+Ctrl+Shift+T. Left as a
documented, known limitation (already in README.md) rather than
implemented; the user can still turn it off manually via Windows Settings
> Bluetooth & devices > Touchpad if they want to accept a restart to get
it, but this add-on does not attempt to automate it. Revisit only if
Microsoft ever documents an official, confirmed-live API for this
specific setting - don't retry the same undocumented registry+broadcast
approach expecting a different result on a future NVDA/Windows version
without testing it fresh, the same way every other claim in this
investigation was tested rather than assumed.

### Diagnosed and clarified: "Activate" vs "Double Click", split-tap looked broken

Reported as three symptoms together: flick navigation still speaking
"selected"/"not selected", split-tap saying "Double Click" instead of
being silent, and same-spot double-tap saying "Double Click" on trackpad
but "Activate" on the real touchscreen. Only the first turned out to be a
real, fixable issue (see next entry, resolved separately, before this
one) - the other two were mostly correct understanding of already-correct
NVDA behavior, reached by reading the actual log evidence rather than
assuming the report matched the apparent symptom:

- **The "double tapping says double click" report was NOT about this
  add-on's split-tap gesture** (`ts(object):1finger_hold+tap`) at all,
  despite that being what "double tapping" sounded like it meant. The log
  showed only a *single* HID contact ID present the whole time, classified
  as `action='tap' actionCount=2` -> `ts(object):double_tap` (a real,
  same-spot double-tap, i.e. one finger tapping twice quickly) - never
  `1finger_hold+tap`. `ts(object):double_tap`/`ts:double_tap` is NVDA's own
  **stock** gesture (`globalCommands.script_review_activate`, also bound to
  `kb:NVDA+numpadEnter`), and it is not silent by design - it always
  speaks either the fixed string `"Activate"` (when `pos.activate()`, a
  `TextInfo`-level activation from the *review position*, succeeds) or the
  touched object's own real, OS-reported MSAA `accDefaultAction` string
  (`obj.getActionName()` -> `IAccessibleObject.accDefaultAction()`) as a
  fallback when `pos.activate()` raises `NotImplementedError`. Confirmed
  directly: "Double Click" is a completely genuine, literal default-action
  label some real desktop/taskbar icons report via MSAA - not something
  NVDA, this add-on, or the input method (touch vs trackpad) fabricates or
  controls. The user confirmed afterward they'd tested *different* icons
  each time (touchscreen test vs trackpad test), which alone fully explains
  seeing different wording - not a discrepancy needing a fix. Lesson: when
  a user says "X does Y", check the log for which actual gesture ID fired
  before assuming X is the feature you think they mean - `double_tap` and
  `1finger_hold+tap` are gesture-level *and* conceptually different things
  that both loosely fit a plain-English description of "double tapping."
- Despite that, **the user did want a request they'd actually made
  implicitly along the way honored**: an audible click cue for activation
  (both split-tap and the stock same-spot double-tap), which became a
  follow-up feature request rather than a bug fix - see the sound-effects
  entry below.

### Fix: flick-based object navigation now moves real focus/selection too

The "not selected" complaint about flick navigation, unlike the two above,
was a real, confirmed, fixable gap. Verified directly from the log: a
flick (`ts(object):flickleft`/`2finger_flickright`/etc) dispatches to
`globalCommands.py`'s own `script_navigatorObject_next`/`_previous`/
`_parent`/`_firstChild`/`_nextInFlow`/`_previousInFlow` - all six of which
only ever call `api.setNavigatorObject(newObj)` +
`speech.speakObject(newObj, reason=OutputReason.FOCUS)`, **never**
`newObj.setFocus()` or any real selection API. This is genuinely stock
NVDA behavior (identical if you use the keyboard equivalents,
`NVDA+numpad6` etc, with no add-on installed at all) - not a regression or
something touch/trackpad-specific - confirmed by finding "not selected"
firing identically right after a plain `kb(laptop):enter` press elsewhere
in the same log, with no touch/trackpad gesture involved.

Fixed by binding this add-on's own scripts to the same six `ts(object):...`
gesture IDs (`flickup`/`flickdown`/`flickright`/`flickleft`/
`2finger_flickright`/`2finger_flickleft`), each replicating its
corresponding stock script's exact movement algorithm (same
`simpleNext`/`simplePrevious`/`simpleParent`/`simpleFirstChild`/
`simpleReviewMode`-respecting logic, read verbatim from the fetched
current `globalCommands.py` rather than guessed) but calling a new shared
`_navigateAndAnnounce(newObj)` helper instead of the stock
`setNavigatorObject`+`speakObject` pair: it still calls
`api.setNavigatorObject(newObj)`, then calls `_touchSelect(newObj)` (the
same helper `_patchedMoveTo` already uses for touch-explore) to move real
focus/selection when `newObj` is focusable, letting NVDA's own async
focus-event pipeline announce it correctly (role suppression, accurate
selection state) exactly like touch-explore already does - falling back to
the stock `speech.speakObject()` announcement only when `_touchSelect` was
a no-op (not focusable, or already focused), matching the stock scripts'
own behavior for non-selectable navigable content (plain text, etc).

This relies on NVDA's script-resolution order giving global-plugin-bound
gestures priority over `globalCommands.GlobalCommands`'s own bindings for
the exact same gesture ID (`scriptHandler.findScript`, called from
`InputGesture._get_script`) - the identical mechanism this add-on's own
pre-existing split-tap gesture already depends on to add a *new* `ts(...)`
binding, just now also used to *override* six gestures `globalCommands.py`
already claims. Keyboard equivalents (`NVDA+numpad6`, etc) are completely
untouched, since gesture identifiers are per-source strings (`kb:...` vs
`ts:...`/`ts(object):...` share no binding relationship) - only actual
touch/trackpad flicks get the real-selection treatment.

### Fix: flick navigation no longer steals real focus into a hidden-desktop window

Separate bug report, same underlying cause category as the previous entry
but a genuinely different mechanism: creating/switching virtual desktops
(`Ctrl+Win+D`, `Ctrl+Win+←/→`) does not destroy or move windows, it only
hides them - so an app's window (e.g. WhatsApp) that was the navigator
object right before a desktop switch stays fully alive in NVDA's
accessibility tree. A subsequent flick then keeps walking that hidden
window's object tree via `simpleNext`/`simplePrevious`/`simpleParent`/
`simpleFirstChild`, and `_navigateAndAnnounce`'s call to `_touchSelect()`
can move **real OS focus/selection** into that hidden window - not just
stale narration, an actual focus-steal into an app the user can't see and
isn't using. This is stock NVDA behavior too (identical with keyboard
`NVDA+numpad6` etc, no touch involved), since plain UIA/MSAA tree walking
has no concept of virtual desktops at all - not a regression, but this
add-on's flick scripts are the ones calling `_touchSelect()`, so they're
where the guard belongs.

Fixed via a new `virtualDesktop.py` module wrapping the public, documented
`IVirtualDesktopManager` COM interface (`IsWindowOnCurrentVirtualDesktop`) -
not to be confused with the various undocumented `IVirtualDesktop`/
`IVirtualDesktopManagerInternal`/`IApplicationView` shell-private interfaces
most community virtual-desktop tools use instead. GUIDs
(`CLSID_VirtualDesktopManager={AA509086-5CA9-4C25-8F95-589D3C07B48A}`,
`IID_IVirtualDesktopManager={A5CD92FF-29BE-454C-8D04-D82879FB3F1B}`)
confirmed against three independent, mutually-consistent sources (pyvda's
`com_defns.py`, `MScholtes/VirtualDesktop.cs`, a community AutoHotkey
sample) since Microsoft Learn's own pages don't print the raw GUID
literals. `_navigateAndAnnounce` now checks
`virtualDesktop.isOnCurrentVirtualDesktop(newObj.windowHandle)` before
calling `_touchSelect()`; if it's confirmed `False`, falls back to plain
narration (`speech.speakObject`) instead of moving real focus/selection
into the hidden window. Fails safe on `None` (COM failure, no virtual
desktop support, etc) by *not* skipping - worst case reproduces the
original bug, never wrongly blocks a legitimate same-desktop flick.

**Known limitation, deliberately not chased further: this fix (and
`IsWindowOnCurrentVirtualDesktop` in general) does not reliably work for
apps that host their UI in a separate helper-process window** - see the
next entry for the full investigation. This module was deliberately kept
in its simple form (`GetAncestor(hwnd, GA_ROOT)` + one COM call, no
process-tree walking) since flick navigation's `newObj` is always an
ordinary NVDAObject from a normal single-process app in every case tested
so far - the MSIX/WebView2-split-process failure mode below was only ever
observed via touch-explore hitting WhatsApp directly, not via flicking
onto it. If flick navigation is ever reported to have the same problem,
see the next entry before re-implementing anything - the fix attempted
there is already written up in detail, just not kept in the shipped code.

### Investigated and reverted: touch-explore narrating a hidden-desktop app's content (WhatsApp/WebView2)

Bug report: after opening WhatsApp Desktop (Microsoft Store version) on one
virtual desktop, switching to a different, empty desktop, and touch-
exploring/swiping there, NVDA still spoke WhatsApp's content (e.g. a
contact/community name) - confirmed genuinely different from the
flick-navigation bug above (this was a continuous touch-explore swipe, not
a flick), and confirmed to still happen even with the flick-navigation fix
in place, since `_patchedMoveTo` is a completely separate code path
(`api.getDesktopObject().objectFromPoint(x, y)`, a fresh point-based
hit-test every call - no navigator-object staleness involved at all).

**Initial hypothesis, confirmed WRONG by live instrumentation**: guessed
this matched a known WebView2 bug
(`MicrosoftEdge/WebView2Feedback#5668`) where a WebView2 host window fades
to alpha=0/`WS_EX_LAYERED` instead of actually hiding. Live
`log.debug` dumps of the actual hit-tested hwnd during a real repro showed
`layered=False alpha=None visible=True exStyle=0x20` - a perfectly
ordinary, fully-opaque, non-layered window. That specific bug's mechanism
does not apply here; don't retry it without re-confirming against a fresh
log first.

**Actual root cause, confirmed via live Win32 enumeration during a real
repro**: WhatsApp Desktop is MSIX-packaged and hosts its UI in a WebView2
control. The window touch-explore actually hits
(`Chrome_RenderWidgetHostHWND`, whose top-level ancestor via
`GetAncestor(hwnd, GA_ROOT)` is a window titled `"(99) WhatsApp"`) is owned
by the **WebView2 helper process** (`msedgewebview2.exe`), a completely
separate top-level window from WhatsApp's **real** application window
(titled plain `"WhatsApp"`, owned by `WhatsApp.Root.exe`, the MSIX app's
root process) - confirmed via `EnumWindows` + `GetWindowThreadProcessId`
that these are two distinct, ownerless top-level windows with **no Win32
window-level relationship** between them at all (`GetParent`/
`GetWindow(GW_OWNER)` both return `NULL` both ways) - the only relationship
is that `msedgewebview2.exe` is a **child process** of `WhatsApp.Root.exe`.

`IVirtualDesktopManager::IsWindowOnCurrentVirtualDesktop` gives the
**correct** answer (`False`, with a resolvable `GetWindowDesktopId`) for
WhatsApp's real app window, but an **incorrect** answer (`True`) for its
WebView2 helper window - confirmed reproducibly with a fresh COM manager
instance each time (ruling out a stale/cached COM object) and via an
atomic single-function-call snapshot comparing both windows together
(ruling out a timing race between separate calls). `DwmGetWindowAttribute`
`DWMWA_CLOAKED` has the identical split: `0` (not cloaked) on the WebView2
window, `2` (`DWM_CLOAKED_SHELL`, correctly indicating "hidden by the
shell") on the real app window - so this isn't specific to one API, both
of Windows' own mechanisms for this get the WebView2 window wrong in the
same way. This is corroborated by the same open, unresolved
WebView2Feedback#5668 issue (independently confirming WhatsApp's WebView2
window does something unusual with its window state/visibility across
virtual desktop switches - a different manifestation of the same
underlying "this app's helper window isn't tracked like a normal window"
category of problem, not the specific alpha-fade mechanism guessed at
first). Chromium's own documentation
(`chromium/src/docs/windows_virtual_desktop_handling.md`) states plainly
that Windows gives no notification when a window changes virtual desktops,
and that Chromium itself only re-derives this state at safe checkpoints
(focus events) rather than trusting it live - i.e. even Chromium's own
engineers don't treat this OS state as reliable ground truth on every
query, which matches what was found here.

**Fix attempted**: walk the process tree from the hit window's owning
process (via `CreateToolhelp32Snapshot`/`Process32FirstW`/`Process32NextW`)
to find its **parent** process, then find that parent's own visible,
ownerless top-level window via `EnumWindows`, and query
`IsWindowOnCurrentVirtualDesktop`/`DWMWA_CLOAKED` against *that* window
instead (treating either signal indicating "not current" as authoritative,
combining both since neither alone was trusted after the above findings).
This resolved the WebView2 window (`msedgewebview2.exe`) to WhatsApp's real
window (`WhatsApp.Root.exe`'s own top-level window) correctly and gave the
right answer (`False`) when tested standalone, outside NVDA, against the
exact hwnd from a real repro.

**Why this was reverted anyway**: after shipping this fix and asking the
user to re-test inside NVDA, the bug still reproduced. Root-caused this
specific regression to the diagnostic process itself, not the fix's own
logic: repeated ad-hoc standalone script probes of
`IVirtualDesktopManager` run minutes apart (to compare the same hwnd's
answer at different points) gave **inconsistent, contradictory results
across separate runs and fresh COM manager instances** - e.g. the exact
same WhatsApp hwnd reported both matching and non-matching desktop GUIDs
relative to the foreground window depending on which desktop happened to
be active *at the moment each separate script was invoked*, which the
investigator did not control for and could not observe from outside NVDA.
This made it impossible to tell, from outside a live NVDA session, whether
the fix's logic was actually correct in the moment the bug reproduced, or
whether the API's behavior itself had shifted between the standalone
verification and the in-NVDA retest. Diagnosing this properly would have
needed the atomic, single-call, in-process instrumentation approach (like
`describeVirtualDesktopState` in the fix's now-reverted code) captured
during an actual live repro inside NVDA, cross-referencing the real NVDA
log - the same discipline documented in "Debugging workflow" below - rather
than separate ad-hoc script invocations at different, uncontrolled points
in time. This diagnostic loop (rebuild → reinstall → restart NVDA →
reproduce → fetch and read the log → interpret → rebuild again) is also
inherently slow and resource-intensive per iteration, and after several
rounds without a fully confirmed fix, continuing was judged not worth the
cost for what is a narrow edge case (one specific MSIX/WebView2 app's
touch-explore narration after a virtual desktop switch, not a general
usability or safety problem) - explicitly decided with the user to stop
and document rather than keep iterating.

**Current state**: this fix was fully reverted (`ghostWindow.py` deleted;
`virtualDesktop.py` reverted back to its simple pre-existing form used only
by the flick-navigation fix above; `_patchedMoveTo` reverted back to plain
`api.getDesktopObject().objectFromPoint(x, y)` with no skip/re-resolve
logic). The bug is real, understood in detail, and documented as a known
limitation in README.md rather than fixed. If revisiting this: the process-
tree-walk approach and the combined `IsWindowOnCurrentVirtualDesktop` +
`DWMWA_CLOAKED` signal were both independently confirmed correct against
the exact real hwnd from a repro when tested standalone - the open question
is only whether that result holds up when captured atomically *during* a
live, in-NVDA repro rather than via separate later script runs. Re-add the
atomic `describeVirtualDesktopState`-style diagnostic (log everything in
one `log.debug` call per touch-explore hit, from inside `_patchedMoveTo`
itself) and get a fresh log from a live repro before trusting any of this
again - don't re-verify via standalone scripts run afterward, they cannot
be trusted to reflect the state at the moment of the actual bug.

### Feature: explore/click sound effects, replacing the tone beep

**Superseded by "Earcon sound packs, extra gestures, diagnostics" below.**
The files described here now live, shortened, in `sounds/classic/`, and
`nvwave.playWaveFile` is no longer used. The notes are kept for their
format/conversion findings.

User-supplied `explore.mp3`/`click.mp3` (in the repo's own `resources/`
folder, unrelated to NVDA - a separate UI sound-effects asset library
already present in the working directory before this add-on touched it)
replace the previous single 1000Hz/30ms `tones.beep()` explore cue and add
a new click cue for activation.

**NVDA cannot play MP3 directly** - `nvwave.playWaveFile()` opens the file
via the stdlib `wave` module (`wave.open(fileName, "r")`), which only
reads WAV; there is no MP3 decoding anywhere in NVDA itself, confirmed by
reading `nvwave.py`'s actual source rather than assuming a `.mp3` path
would just work. Converted both files to WAV with `ffmpeg`, matching NVDA's
own bundled UI sound format exactly (22050 Hz, mono, 16-bit PCM) rather
than guessing a plausible format - confirmed by inspecting a real NVDA
sound file (`<NVDA install dir>/waves/browseMode.wav`) directly rather than
assuming. Bundled at
`touchExplore/globalPlugins/touchExplore/sounds/{explore,click}.wav`
(picked up automatically by `build.py`'s recursive `os.walk`, no build
script changes needed).

- **explore.wav** replaces the tone in `_patchedMoveTo`, played whenever a
  new real item is landed on. It's noticeably longer than the tone it
  replaced (~1.03s vs 30ms) - deliberately kept at full length rather than
  trimmed, per explicit user preference, accepting that fast exploration
  will audibly cut it short each time a new item is landed on before the
  previous sound finishes (`nvwave.playWaveFile` stops any in-progress
  sound when a new one starts, same behavior category as speech
  interruption elsewhere in this add-on).
- **click.wav** plays on activation: added to `_activateObject()` (this
  add-on's own split-tap, right after `obj.doAction()` succeeds) and to a
  new `script_touchExploreDoubleTapActivate` override of NVDA's stock
  `ts:double_tap` gesture (see previous entry).
- **Follow-up fix, same session**: the user asked why the click sound
  seemed tied to the "Double Click" wording - it isn't; they're
  independent (the sound plays on any successful activation regardless of
  what `ui.message()` says afterward), but talking through it surfaced
  that the wording itself (stock's "Activate"/per-icon `accDefaultAction`
  announcement) was unwanted noise, not useful confirmation. Fixed by
  making `script_touchExploreDoubleTapActivate` drop its `ui.message()`
  calls on the success paths entirely, matching `_activateObject`
  (split-tap) exactly: click sound only, no speech. Also fixed a real bug
  caught during this same edit - the click sound in
  `script_touchExploreDoubleTapActivate` was being played
  unconditionally, including for the gesture's keyboard-sourced bindings
  (`ts:double_tap` is also `kb:NVDA+numpadEnter`/`kb(laptop):NVDA+enter`);
  `notifyInteraction()` was already correctly gated on
  `isinstance(gesture, touchHandler.TouchInputGesture)` but `_playSound`
  was not - now both are gated together, so keyboard activation via this
  script stays fully silent (matching its pre-existing, correct
  `ui.message()`-only stock behavior before this session's changes).
- Verified end-to-end before considering this done: converted files
  round-tripped correctly through Python's `wave` module (the exact
  mechanism `nvwave.playWaveFile` itself uses) with the expected
  channel/rate/width/duration values, and were played back for the user to
  confirm audibly (via `Media.SoundPlayer` in a probe script) rather than
  just trusting that a successful `ffmpeg` exit code meant a correct,
  audible result.

### Hardware-universality pass (trackpad mode on non-development hardware)

Goal: make trackpad mode correct on Precision Touchpads other than the
development one, without changing behavior on it. What changed and why:

- **Tip/Confidence read as buttons** (`HidP_GetButtonCaps`/`HidP_GetUsages`),
  and honored only for slots that declare them: Tip clear = the spec's
  explicit lift report (lift at last tip-down position); Confidence clear =
  palm, ignored until that contact ID stops being reported (the spec says
  confidence stays cleared for the rest of the contact's life). Without the
  usages (or if reading them fails) liveness is Contact Count alone, as
  before. **Not yet confirmed from a live capture on this pad** - a 90s
  capture attempt recorded 0 reports (almost certainly no touch during the
  window, per the handoff warning above). The decoder *is* verified against
  this pad's real preparsed data, using reports built with
  `HidP_SetUsageValue`/`HidP_SetUsages` - a useful technique for testing HID
  decode without anyone touching the hardware.
- **Hybrid reporting mode** (`_FrameAssembler`): a frame split across
  reports (first has the real Contact Count, the rest have 0 and the same
  Scan Time) is reassembled. Previously the Contact Count 0 follow-up read
  as "every finger lifted". The dev pad has 5 slots per report (parallel
  mode), so it never takes this path.
- **Unreadable Contact Count = ignore the report**, not "0 contacts" (a
  report with a different Report ID used to lift every finger).
- **Contacts keyed by (hDevice, contactId)**, mapped to fresh integer
  tracker IDs - two touchpads no longer collide.
- **Lift-inference timeout** = max(80ms, 8 × the device's smoothed
  real-report interval; gaps over 250ms excluded from the average). At this
  pad's report rate the 80ms floor wins, so behavior here is unchanged.
- **Maps onto the foreground window's monitor**, fixed per gesture. Falls
  back to primary when `config.conf["touch"]["edgeGestures"]` is on, because
  `touchHandler._getEdge` tests against primary-screen size only.
- **`RIDEV_DEVNOTIFY` + `WM_INPUT_DEVICE_CHANGE`**: on removal, lift that
  device's contacts and drop cached state. `hDevice == 0` is attributed to
  the sole touchpad if there's exactly one. `NoTouchpadFoundError` gives a
  specific spoken message.
- **`pump()` borrows `TouchHandler._processGestures`** (plus
  `_tryBuildSequentialGesture`) when present, so trackpad input gets NVDA's
  sequential flicks. On any exception it falls back to the legacy loop for the
  rest of the session. **The installed NVDA 2026.2 does NOT have
  `_processGestures`, sequential flicks or edge gestures** - checked by
  grepping names out of `touchHandler.pyc` in `C:\Program Files\NVDA\library.zip`
  (it does have `TouchMode` and pinch). Those are newer than 2026.2 on
  `master`, so here the legacy loop runs. The "Source of truth" section's
  mention of `_processGestures` on the installed version was a `master`
  fact, not an installed-version fact. Grepping `.pyc` names in
  `library.zip` is a quick, reliable way to check what the installed NVDA
  actually has.
- `build.py` now skips `__pycache__` (a stale `ghostWindow.pyc` from the
  reverted module had been shipping inside the package).

Tests: standalone, NVDA modules stubbed, not tracked in the repo (same as
the earlier lift-inference tests). **User confirmed live afterwards: works
well on the development hardware.**

### Touch thresholds in millimetres + calibration (`touchSettings.py`, `calibration.py`, `settingsUI.py`)

**What the installed NVDA 2026.2 actually does** (read from the
`release-2026.2` tag, which matches the installed version per
`_buildVersion.pyc` - fetch that tag, not `master`, while 2026.2 is what's
installed). This corrects claims elsewhere in this file that came from
`master`:
- `SingleTouchTracker.update()` is purely distance-based: tap if both
  per-axis max deltas `< maxAccidentalDrift`, flick if the dominant axis
  `>= minFlickDistance`, both only `if deltaTime < multitouchTimeout`,
  otherwise `action_hover`. **No `minFlickVelocity` / velocity window in
  2026.2**, and it uses the `action_*` string constants, not a
  `TouchAction` enum (`touchHandler.TouchMode` does exist).
- `multitouchTimeout` has three jobs: the maximum tap/flick duration; the
  double-tap window (`pluralTimeout = startTime + multitouchTimeout`, so the
  second tap must *start* within it of the first tap's *start*); and the
  wait before a single tap is emitted.
- `minPinchDistance` exists (pixels).

**Design**:
- The settings are per input source (touchscreen / trackpad), stored flat
  in `config.conf["touchExplore"]` (so they're profile-aware), and converted
  to px into the same `touchTracker` globals. The touchscreen uses
  `screenPxPerMm()`: `DESKTOPHORZRES/HORZSIZE` averaged with the vertical
  equivalent. Not `HORZRES`: that one is scaled for a non-DPI-aware thread
  (it read 1536 instead of 2304 at 150%). The trackpad uses mapped-monitor px
  / pad mm, from the HID Physical/Unit/UnitExp of X/Y (this pad: 120 × 80 mm),
  recomputed in `_applyThresholds` whenever a gesture's map rect is chosen.
- The defaults reproduce the old fixed 25px / 50px / 0.4s exactly on the
  development hardware (touchscreen 7.92 px/mm → 3.2/6.3 mm; trackpad
  19.2 px/mm → 1.3/2.6 mm). Other hardware gets the same physical feel
  instead of the same pixel count.
- Calibration records by wrapping the active source's
  `trackerManager.update` on the *instance* (`del` restores it), and blocks
  touch gestures during capture via `inputCore.decide_executeGesture`. Both
  are undone in a `finally` after `ShowModal()`, because wx's C++
  Escape/Cancel path never calls a Python `EndModal` override. Leaving the
  decider registered would kill all touch input until NVDA restarts.
- For speech, set focus first, then `wx.CallLater(300, ui.message, ...)`:
  NVDA's own focus announcement cancels speech that's already in progress.
- `validate()` compares with a 1e-6 tolerance, because 0.8 × 1.5 is
  1.2000000000000002; a test sweeping every drift value caught this.
- **The GUI (panel + dialog) can't run outside NVDA** and hasn't been
  exercised live yet. The logic under it is covered by the standalone tests.

### Earcon sound packs, extra gestures, diagnostics (`audioCues.py`, `monitors.py`, `diagnostics.py`)

- **Sounds**: `sounds/earcons/` holds Kenney "Interface Sounds" (CC0;
  downloaded from kenney.nl, licence copied alongside), and `sounds/classic/`
  the original explore/click. Both are built by
  `resources/sounds/build_sounds.py` (checked to rebuild byte-identically).
  Cues were chosen by *measured* duration/brightness, since no one could
  listen during the build: frequent cues are under about 100ms, and all
  are under 300ms. `resources/sounds/earcon-audition.wav` (gitignored)
  speaks each cue's name, via Windows SAPI TTS, before playing it, so the
  user can judge the set by ear. The old explore sound's *audible* part
  was only about 65ms; most of its ~1s length was a quiet tail.
- **Why not `nvwave.playWaveFile`**: panning. `WavePlayer.stop()`/`open()`
  call `_setVolumeFromConfig()`, which resets every channel's volume, so
  `setVolume(left=, right=)` can't hold a pan. Instead the pan is baked into
  stereo sample data (linear gains; centre = both channels at full level),
  cached per quantised pan step, and fed to one persistent
  `WavePlayer(purpose=AudioPurpose.SOUNDS)`. That purpose is what applies
  NVDA's sound volume. `feed()` doesn't block for clips this short, so no
  thread is needed. `nvwave.decide_playWaveFile` is still consulted.
- **Height as pitch** (user request, after trying the panning): y within
  the touched monitor maps to ±`PITCH_RANGE_SEMITONES` (6), quantised to
  whole semitones, with the top edge highest. It's done by linear-interpolation
  resampling of the samples (`_pitched`), not by changing the player's rate,
  so one `WavePlayer` serves every pitch. Duration scales with pitch, which
  is fine for these short cues. Verified musically with an FFT on a 440Hz
  sine: +12 → 880, −12 → 220, +6 → 622. The first render costs 1.4ms for the
  item cue and 8ms for the longest (activate); cached renders are microseconds.
  The render cache is keyed (path, panStep, semitones) and capped at 256
  entries, dropping the oldest first. The audition file demos height, pan and
  both, rendered by the add-on's own `_renderedData` so it's exactly what
  NVDA plays.
- **`monitors.py` exists because ctypes `argtypes` are process-global.**
  Two modules each declaring `GetMonitorInfoW` with their *own*
  `MONITORINFO` class would break whichever loaded first
  (`ArgumentError`). Any Win32 function taking a Structure should be
  declared in exactly one module.
- **Gestures** use IDs that 2026.2's `globalCommands` leaves unbound
  (checked against `release-2026.2`): `ts:2finger_tap`,
  `ts:3finger_double_tap`, `ts(object):3finger_flickup/down` (text mode's
  `3finger_flickDown` is stock say-all), `ts:2finger_triple_tap`
  (`counterNames` = single/double/triple/quadruple), and
  `ts:2finger_pinchin/out`. Pinch trackers are always `numFingers=2`, hence
  the `2finger_` prefix. Rate changes are stored exactly as
  `synthSettingsRing` does it (`setattr(synth, ...)` plus
  `config.conf["speech"][synth.name][...]`). Keys are sent with
  `KeyboardInputGesture.fromName("pageDown"/"pageUp"/"mediaPlayPause")`,
  whose names were checked in `vkCodes.py`.
- **Diagnostics**: `diagnostics.collect()` reports versions, touch hardware,
  px/mm, the `touchTracker` values in effect, every setting, and each
  touchpad's VID/PID/slots/Tip/Confidence/size/report interval. It was run
  against the real hardware in the tests. Config sections are read key by
  key: an `AggregatedSection` isn't guaranteed to support `.get()`/`.items()`.
- **Not exercised live yet**: the panel's sound controls, the preview, and
  the new gestures inside NVDA. Covered by the standalone tests: role→cue
  mapping, pan data, fallback pack, decider, failure safety, and every
  bundled file loading and being short.

## Debugging workflow that actually worked

Guessing at NVDA internals from memory/paraphrase burned an iteration early
on. What works, and has been used twice successfully since: add
`log.debug(...)` calls at each decision point (or monkeypatch a
diagnostics-only wrapper around the suspect method that logs before
delegating to the original, unchanged, behavior), have the user set NVDA's
logging level to Debug (NVDA Settings > General > Logging level), reproduce,
and read `grep "touchExplore:"` out of the log (Tools > View Log in NVDA, or
the log file directly).

**Where the log actually lives**: NVDA writes `nvda.log` (and `nvda-old.log`
for the previous run, e.g. before a crash/restart) to `%TEMP%\nvda.log` -
**not** under `%APPDATA%\nvda`, which only holds config/profile data, no
logs. If NVDA had to be restarted to recover from something (like the
`pump()` incident below), the *previous* run's log is `nvda-old.log`, not
`nvda.log` - check both. When speech itself has broken (so the user can't
be walked through NVDA's own Tools > View Log by voice), reading the file
directly is the only way in; grep it for the add-on's own log lines
(`touchExplore:`) but also, critically, for bare `ERROR`/`AttributeError`/
`Traceback` - a bug in this add-on can break NVDA's *own* internals (see
below), and those errors won't be prefixed with anything from this add-on
at all, they'll appear as NVDA's own `core.py`/etc log lines.

- First use: conclusively showed `UIASelectionItemPattern=None` and
  `obj.states` unchanged after `setFocus()` — pinned down that
  `SELFLAG_TAKEFOCUS` alone doesn't select.
- Second use: temporarily wrapped `touchTracker.TrackerManager.update` and
  `.makeMergedTrackerIfPossible` to log each tracker's action/drift/timing
  and every merge attempt's outcome. The log showed, across three separate
  failed multi-finger taps, the same pattern every time: 1-2 of the
  fingers exceeded 10px drift and were stuck at `action=unknown`, so they
  never reached the merge step at all - it wasn't a timing/overlap issue
  (the merge logic itself was never even invoked for the dropped fingers).
  This is why the fix ended up being a one-line constant change rather
  than anything touching the merge/timing logic that was the original
  suspicion.
- Third use, developing trackpad-as-touchscreen mode: raw Win32/HID ctypes
  code (struct layouts, argtypes, the two-raw-input-devices-per-touchpad
  situation, the missing Tip Switch usage) is *not* reliably guessable or
  even reliably gettable from docs prose alone — every load-bearing detail
  in the "Trackpad-as-touchscreen mode" section above was pinned down by
  writing small standalone Python/ctypes probe scripts (outside NVDA
  entirely - just `python probe.py` in a terminal) that print exactly what
  a real Win32/HID call returns on this machine, and iterating on those
  until the real behavior was fully understood, before writing a single
  line of the actual add-on code. This was substantially faster and more
  reliable than reasoning from documentation or memory about e.g. exact
  struct byte layouts or which of two same-Usage-Page HID devices raw input
  messages actually arrive from — both were things no amount of docs
  reading alone would have surfaced correctly. When a live human is present
  to physically touch the trackpad during a probe run, be explicit and
  patient about the handoff (announce you're starting, give a real window,
  confirm they actually touched it if a run comes back empty) rather than
  guessing from a timeout whether the touch happened or the code is broken
  - several apparent "bugs" during this session were actually just the
  probe running before anyone touched the trackpad.
- Fourth use, diagnosing the post-release "trackpad mode makes speech stop,
  need to restart NVDA" report: standalone probe scripts (as in the third
  use above) are great for validating a mechanism in isolation, but they
  cannot catch a bug that only manifests when the code runs *inside* NVDA's
  own process and interacts with NVDA's own scheduling/threading
  assumptions (here: `core.py`'s pump loop unconditionally calling
  `touchHandler.handler.pump()`). No amount of standalone HID-capture
  testing would have surfaced the missing-`pump()`/wrong-thread bugs, since
  those only exist at the seam between this add-on's code and NVDA's core -
  reading the actual NVDA log from the user's real run (see above) was the
  only way to find it, and it took one `grep` to go straight to the exact
  cause (`AttributeError: 'TrackpadTouchScreen' object has no attribute
  'pump'`, repeating every ~4-9ms). Lesson: for a feature that installs
  itself into an NVDA singleton/extension point (here,
  `touchHandler.handler`), standalone testing proves the feature's own
  logic works but cannot prove it satisfies every contract the singleton's
  real callers (elsewhere in NVDA core, not just this add-on) expect of it
  - re-read the actual call site(s) of anything being impersonated (found by
  grepping the fetched NVDA source for the attribute/method name, e.g.
  `handler\.pump\(\)` or `handler\._curTouchMode`, not just the class
  definition being impersonated) before considering the impersonation
  complete.
- Fifth use, diagnosing "Desktop spoken between icons, container fix
  broken": the user's bug report matched a previous, already-fixed bug
  class closely enough (touch-explore chatter) that the obvious first guess
  was a regression in `_patchedMoveTo`/`_CONTAINER_ROLES`. Adding one
  `log.debug(f"obj.name=... obj.role=... containerHit=...")` line at the
  top of the suspect function and reading it back immediately disproved
  that guess (`containerHit=True` every time, correctly) rather than
  confirming it - which redirected the investigation to the *actual* cause
  (NVDA's separate mouse-tracking announcement pipeline) in one step,
  instead of burning time trying to fix code that was already correct.
  Lesson: when a report sounds like a known bug pattern, add the
  single cheapest instrumentation point that would prove or disprove the
  obvious hypothesis *before* changing any code - it's faster than
  "fixing" working code and re-testing to discover the report still
  reproduces.
- Sixth use, diagnosing "multi-finger and flick gestures don't register":
  two rounds of instrumentation, not one, were needed because the first
  round's data was ambiguous on its own. The first captured session showed
  a 2-finger episode that never emitted a merged gesture - consistent with
  *either* a real bug *or* correct-by-design hover-stack behavior for a
  slow drag (see above), and the raw contact-ID log alone couldn't
  distinguish the two. Rather than guess, the user was asked to
  specifically retry with a quick, deliberate tap/flick (not a drag), and
  the *second* instrumented capture - now with per-report timestamps
  visible in the log - showed the real signature: a ~1.2s gap with zero
  `WM_INPUT` messages, mid-gesture, on hardware that was assumed (never
  verified) to report continuously like the earlier single-finger testing
  suggested. Lesson: when instrumentation output is consistent with more
  than one explanation, don't pick the more interesting-looking one and
  start fixing it - get the user to reproduce the *specific* narrower case
  that would tell the explanations apart, before writing any fix. This also
  surfaced a second, independent NVDA source-drift issue in the same
  investigation (`SingleTouchTracker.update()`'s hover-lock-on-any-call
  behavior, not just its classification-on-lift behavior, differs from what
  the drift-threshold history in this file describes for the
  `release-2025.3` snapshot) - re-fetching and re-reading the actual
  current method body end-to-end, not just skimming for the previously-known
  drift/timeout constant names, is what caught it.
- Seventh use, diagnosing "flicks still don't work" after the sixth use's
  fix already shipped: the earlier fix (timer-poll + raised
  `multitouchTimeout`) was real and necessary but incomplete - it fixed the
  reported symptom it was built for (stalled multi-finger taps) without
  fixing flicks, which looked superficially like the same class of bug but
  had a different, more specific cause (silent lift, not silent hold).
  Confirming the fix with a narrow, isolated live test ("do just one flick,
  nothing else") before declaring it done, rather than trusting that a fix
  for one symptom in a bug report covered every symptom in that same
  report, is what caught this before it shipped as "fixed" when it wasn't.
  Also: this round's diagnosis needed logging at EVERY early-return branch
  in the suspect function, not just the success path - the previous round's
  instrumentation only logged on successful decode, which meant a silently
  swallowed or malformed report would have looked identical to "no report
  arrived at all" in the log; ruling out every intermediate failure point
  explicitly (not just inferring "probably fine" from their absence) is
  what made "the hardware sent zero WM_INPUT, full stop" a confirmed fact
  rather than a remaining assumption.

Reach for this before iterating blindly on the next API guess - all seven
times the actual cause was more specific (and the fix simpler) than the
initial hypothesis.

## Build/install loop

```
python build.py            # -> touchExplore.nvda-addon
```

Install: double-click the `.nvda-addon` with NVDA running, or NVDA Add-on
Store > "Install from external source". Restart NVDA (or NVDA+Ctrl+F3 to
reload plugins, though a full restart is more reliable after editing a
patched-class module like this one) for changes to take effect.
