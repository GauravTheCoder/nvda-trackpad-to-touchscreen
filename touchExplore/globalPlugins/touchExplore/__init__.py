# Touch Explore Sounds
# A global plugin for NVDA that improves touch-explore feedback:
# - Plays a short tone when the finger lands on a real, actionable item
#   (icon, list item, button, link, etc), and moves real focus/selection to
#   it (VoiceOver-style), letting NVDA's own focus-speech pipeline announce
#   it correctly (role suppression, selection state, etc) instead of us
#   re-implementing that logic and risking double speech.
# - Stays silent when the finger is over empty space inside a container
#   (e.g. the gap between desktop icons), instead of repeating the
#   container's "N items" description on every micro-movement.
# - Adds a VoiceOver-style split-tap gesture: hold one finger on a
#   touch-explored item, tap anywhere else on the screen with a second
#   finger, and that item is activated (its default action performed) -
#   equivalent to double-tapping the item itself, without needing to lift
#   and re-tap the same exact spot.
# - Adds a trackpad-as-touchscreen mode (NVDA+Ctrl+Shift+T to toggle): reads
#   the trackpad's own raw multi-touch HID contacts and feeds them into
#   NVDA's real touch pipeline, so every touch gesture above - and NVDA's
#   own stock touch gestures - work from a trackpad on a machine with no
#   touchscreen. See trackpadTouch.py for the implementation and
#   CLAUDE.md for the hardware-verified details behind it.
#
# Touch explore-by-touch reporting lives in
# screenExplorer.ScreenExplorer.moveTo(), called directly by touchHandler
# as the finger moves. There's no extension point for it, and touchHandler
# can recreate ScreenExplorer instances at any time (e.g. when touch
# support is toggled or a config profile switch occurs), so rather than
# patching one instance, we patch the moveTo method on the class itself.

import api
import config
import controlTypes
import globalPluginHandler
import locationHelper
import screenExplorer
import speech
import textInfos
import tones
import touchHandler
import touchTracker
import ui
from comtypes import COMError
from logHandler import log
from scriptHandler import script
from utils.security import objectBelowLockScreenAndWindowsIsLocked

from . import touchpadOsSettings
from .trackpadTouch import TrackpadTouchScreen

# Roles that are "generic containers": their own announcement (name, role,
# row/column counts) is a waypoint, not content. This is true whether the
# finger is over genuinely empty space (e.g. the desktop) or briefly
# resolves to the list/pane itself while sliding between real rows/icons
# inside it (e.g. Explorer's "Items View" list). Either way, only the real
# items inside are worth speaking.
_CONTAINER_ROLES = frozenset(
	{
		controlTypes.Role.WINDOW,
		controlTypes.Role.PANE,
		controlTypes.Role.DIALOG,
		controlTypes.Role.LIST,
		controlTypes.Role.TREEVIEW,
		controlTypes.Role.FRAME,
		controlTypes.Role.APPLICATION,
		controlTypes.Role.GROUPING,
		controlTypes.Role.DIRECTORYPANE,
		controlTypes.Role.GLASSPANE,
		controlTypes.Role.LAYEREDPANE,
		controlTypes.Role.ROOTPANE,
		controlTypes.Role.SCROLLPANE,
		controlTypes.Role.SPLITPANE,
		controlTypes.Role.DESKTOPPANE,
		controlTypes.Role.OPTIONPANE,
		controlTypes.Role.PANEL,
		controlTypes.Role.INTERNALFRAME,
	},
)

# Tone played when landing on a new, real item.
ITEM_TONE_HZ = 1000
ITEM_TONE_MS = 30


# MSAA SELFLAG_TAKEFOCUS | SELFLAG_TAKESELECTION. NVDA's own
# IAccessible.setFocus() only passes SELFLAG_TAKEFOCUS (1): that moves focus
# but does *not* change selection, which is why calling it left every
# desktop icon reporting "not selected" regardless of which one was
# touched - focus moved, but the previously-selected icon (if any) stayed
# selected and the touched one never became selected. Passing both flags
# together is the standard "click this item, exclusively selecting it and
# deselecting everything else" operation for single-select MSAA controls.
_SELFLAG_TAKEFOCUS_AND_SELECTION = 1 | 2


def _touchSelect(obj) -> None:
	"""Move real focus and selection to obj, mirroring VoiceOver: touch-explore
	doesn't just narrate items, it makes the touched item the
	actually-focused/selected one (and, for single-select controls,
	unselects whatever was selected before). This is what makes
	"selected"/"not selected" state speech meaningful rather than noise -
	the touched item is expected to read as selected, so NVDA's own "don't
	announce selected for the sole selected item, but do announce unselected
	siblings" logic does the right thing without us fighting speech
	internals across multiple code paths (object speech, text info speech,
	etc).

	Classic MSAA/IAccessible controls (e.g. the desktop and Explorer's
	SysListView32) need an explicit accSelect() call with both
	SELFLAG_TAKEFOCUS and SELFLAG_TAKESELECTION - NVDA's own setFocus() on
	these objects only passes TAKEFOCUS. Modern UIA-backed controls handle
	focus and selection as separate concerns too - UIA's SetFocus() only
	moves focus - so there selection needs the SelectionItemPattern's
	select() explicitly, which NVDA's own doAction() uses the same way and
	which also moves focus as part of selecting.
	"""
	states = obj.states
	if controlTypes.State.FOCUSABLE not in states or controlTypes.State.FOCUSED in states:
		return
	selectionItemPattern = getattr(obj, "UIASelectionItemPattern", None)
	if selectionItemPattern is not None:
		try:
			selectionItemPattern.select()
		except COMError:
			log.debugWarning("touchExplore: UIASelectionItemPattern.select() failed", exc_info=True)
		return
	iaObj = getattr(obj, "IAccessibleObject", None)
	iaChildId = getattr(obj, "IAccessibleChildID", None)
	if iaObj is not None:
		try:
			iaObj.accSelect(_SELFLAG_TAKEFOCUS_AND_SELECTION, iaChildId)
			return
		except COMError:
			log.debugWarning("touchExplore: accSelect(TAKEFOCUS|TAKESELECTION) failed", exc_info=True)
	obj.setFocus()


def _activateObject(obj, gesture) -> None:
	"""Perform the default action on obj (and, failing that, walk up its
	parents), mirroring VoiceOver's split-tap gesture: hold one finger on an
	item to select it via touch-explore, then tap anywhere else on the
	screen with a second finger to activate it - equivalent to double-tapping
	the item itself, without needing to lift and re-tap the same spot.

	This intentionally mirrors globalCommands.script_review_activate's
	object-activation fallback (doAction() walking up obj.parent on
	NotImplementedError, notifyInteraction() so the OS knows this was a
	touch interaction) rather than going through api.getNavigatorObject()/the
	review position: obj here is already the exact item _patchedMoveTo
	tracked under the held finger, so using it directly avoids any
	dependency on navigator object/review position being in sync at the
	moment the second finger taps. Deliberately silent, like a regular
	double-tap.
	"""
	while obj and not objectBelowLockScreenAndWindowsIsLocked(obj):
		try:
			obj.doAction()
			if isinstance(gesture, touchHandler.TouchInputGesture):
				touchHandler.handler.notifyInteraction(obj)
			return
		except NotImplementedError:
			obj = obj.parent


# --- Multi-finger tap misdetection fix --------------------------------
# NVDA sometimes recognizes a 3-finger (or 2-finger) tap as having fewer
# fingers than were actually used. Diagnosed via log.debug instrumentation
# on touchTracker.TrackerManager: SingleTouchTracker.update() only
# classifies a completed touch as action_tap if it stayed within
# touchTracker.maxAccidentalDrift (10px) of its start point for its entire
# duration; if it drifts further it's left as action_unknown and never
# reaches processAndQueueMultiTouchTracker's merge logic at all (only
# non-unknown actions get queued/merged there). With multiple simultaneous
# fingers, it's normal for at least one to drift more than a single
# practiced finger tap would - observed drift on failed 3-finger taps was
# up to ~19px, comfortably exceeding the 10px default and causing 1-2 of
# the 3 fingers to silently drop out, so NVDA reports a 1 or 2 finger tap
# instead. Raising the threshold fixes this at the source (touch tracking
# is otherwise unmodified) without touching the merge logic itself, which
# behaved correctly (pure time-interval overlap; not the actual culprit -
# multi-finger flicks, which don't have a drift ceiling, always merged
# correctly in the same test session).
_originalMaxAccidentalDrift = touchTracker.maxAccidentalDrift
_PATCHED_MAX_ACCIDENTAL_DRIFT = 25
_driftPatched = False
# --- end multi-finger tap fix -------------------------------------------


# --- Tap/flick classification timeout fix -------------------------------
# touchTracker.SingleTouchTracker.update() locks a touch's action to HOVER
# the instant touchTracker.multitouchTimeout (0.25s default) elapses since
# the touch started, on ANY update() call (not just on lift) - regardless of
# what the finger does afterward. Diagnosed via log.debug instrumentation on
# trackpadTouch.TrackpadTouchScreen while investigating multi-finger taps
# and flicks not registering in trackpad-as-touchscreen mode: a deliberate,
# genuine 2-finger tap attempt was observed taking longer than 250ms
# door-to-door (coordinating two fingers to touch and lift together takes
# real, measurable time), so by the time the fingers lifted, both had
# already been irreversibly locked to HOVER and could never become
# action_tap/action_flick* at all - not a merge-logic problem, and not
# fixable by any amount of prompt update() calling once the 250ms window has
# actually elapsed relative to real time. Raising the timeout gives real
# human multi-finger gestures (and flicks in general) more realistic budget
# to complete before being written off as a hover, at the cost of also
# giving a genuinely slow hover/drag slightly longer before NVDA starts
# treating its own continued movement as touch-exploring rather than a
# potential tap-in-progress - same class of "the machine-tight default
# doesn't match real human timing" fix as the drift patch above, and
# extended module-globally the same way, applying to real touchscreen
# gestures too, not just trackpad mode (accepted; see CLAUDE.md).
_originalMultitouchTimeout = touchTracker.multitouchTimeout
_PATCHED_MULTITOUCH_TIMEOUT = 0.4
_timeoutPatched = False
# --- end tap/flick classification timeout fix ---------------------------


_originalMoveTo = screenExplorer.ScreenExplorer.moveTo
_patched = False

# Tracks the last object/position _patchedMoveTo acted on, so we can collapse
# exact back-to-back repeats that arise from an item being reachable via both
# the object path (focus/selection) and the text/cell path (speakTextInfo).
_lastSpokenKey = None


def _isContainerHit(obj) -> bool:
	"""Decide whether obj is a generic container whose own announcement
	should be suppressed (as opposed to a real item worth speaking).
	"""
	return obj.role in _CONTAINER_ROLES


def _patchedMoveTo(self, x, y, new=False, unit=textInfos.UNIT_LINE):
	obj = api.getDesktopObject().objectFromPoint(x, y)
	prevObj = None
	while obj and obj.beTransparentToMouse:
		prevObj = obj
		obj = obj.parent
	if not obj or (
		obj.presentationType != obj.presType_content and obj.role != controlTypes.Role.PARAGRAPH
	):
		obj = prevObj
	if not obj:
		return

	containerHit = _isContainerHit(obj)

	hasNewObj = False
	if obj != self._obj:
		self._obj = obj
		hasNewObj = True
		if self.updateReview:
			if not api.setNavigatorObject(obj):
				return
	else:
		obj = self._obj

	pos = None
	if obj.treeInterceptor:
		try:
			pos = obj.treeInterceptor.makeTextInfo(obj)
		except LookupError:
			pos = None
		if pos:
			obj = obj.treeInterceptor.rootNVDAObject
			if hasNewObj and self._obj and obj.treeInterceptor is self._obj.treeInterceptor:
				hasNewObj = False
	if not pos:
		try:
			pos = obj.makeTextInfo(locationHelper.Point(x, y))
		except (NotImplementedError, LookupError):
			pass
		if pos:
			pos.expand(unit)
	if pos and self.updateReview:
		api.setReviewPosition(pos)

	global _lastSpokenKey

	if containerHit:
		# A generic container hit: either genuinely empty space, or the
		# list/pane itself resolved as a stepping-stone between real rows.
		# No speech, no repeated "N items"/"Items View" chatter. Position
		# tracking is still updated (silently) so we don't misfire once the
		# finger reaches a real item.
		if pos:
			self._pos = pos
		return

	posChanged = bool(
		pos
		and (
			new
			or not self._pos
			or pos.__class__ != self._pos.__class__
			or pos.compareEndPoints(self._pos, "startToStart") != 0
			or pos.compareEndPoints(self._pos, "endToEnd") != 0
		)
		and not objectBelowLockScreenAndWindowsIsLocked(pos.obj)
	)

	# Build a de-duplication key describing what we're about to say. If it
	# exactly matches the last thing we actually spoke, skip re-speaking it
	# (this happens when the object path and text/cell path both resolve to
	# the same visible content, e.g. a list item vs. its "Name" edit cell).
	objKey = (obj, obj.name, obj.role) if hasNewObj else None
	posKey = None
	if posChanged:
		try:
			posKey = (pos.obj, pos.text)
		except Exception:
			posKey = (pos.obj, None)
	newKey = (objKey, posKey)
	if newKey == _lastSpokenKey and (objKey is not None or posKey is not None):
		return
	_lastSpokenKey = newKey

	speechCanceled = False
	if hasNewObj and objKey is not None and not objectBelowLockScreenAndWindowsIsLocked(obj):
		speech.cancelSpeech()
		speechCanceled = True
		tones.beep(ITEM_TONE_HZ, ITEM_TONE_MS)
		# Actually move focus and selection to obj (VoiceOver-style
		# touch-explore) rather than speaking it ourselves. This triggers a
		# real OS focus/selection change, which NVDA's own event hooks pick
		# up asynchronously and announce through the normal focus pipeline -
		# already handling role suppression for roles like LISTITEM
		# ("Recycle Bin" not "Recycle Bin, list item") and selection-state
		# speech ("selected"/"not selected") correctly, since the touched item
		# genuinely is now the selected one. Speaking it ourselves here too
		# would double-announce every item.
		_touchSelect(obj)
	if posChanged:
		self._pos = pos
		if not speechCanceled:
			speech.cancelSpeech()
		speech.speakTextInfo(pos, reason=controlTypes.OutputReason.CARET)


# Translators: category shown for this add-on's commands in the Input
# Gestures dialog.
_SCRIPT_CATEGORY = _("Touch Explore Sounds")


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	scriptCategory = _SCRIPT_CATEGORY

	def __init__(self):
		super().__init__()
		global _patched, _driftPatched, _timeoutPatched
		if not _patched:
			screenExplorer.ScreenExplorer.moveTo = _patchedMoveTo
			_patched = True
			log.debug("touchExplore: patched ScreenExplorer.moveTo")
		if not _driftPatched:
			touchTracker.maxAccidentalDrift = _PATCHED_MAX_ACCIDENTAL_DRIFT
			_driftPatched = True
			log.debug("touchExplore: raised touchTracker.maxAccidentalDrift")
		if not _timeoutPatched:
			touchTracker.multitouchTimeout = _PATCHED_MULTITOUCH_TIMEOUT
			_timeoutPatched = True
			log.debug("touchExplore: raised touchTracker.multitouchTimeout")
		self._trackpadTouchScreen = None
		self._savedMouseTrackingEnabled = None

	def terminate(self):
		global _patched, _driftPatched, _timeoutPatched
		if self._trackpadTouchScreen is not None:
			self._disableTrackpadTouchScreen()
		if _patched:
			screenExplorer.ScreenExplorer.moveTo = _originalMoveTo
			_patched = False
			log.debug("touchExplore: restored original ScreenExplorer.moveTo")
		if _driftPatched:
			touchTracker.maxAccidentalDrift = _originalMaxAccidentalDrift
			_driftPatched = False
			log.debug("touchExplore: restored original touchTracker.maxAccidentalDrift")
		if _timeoutPatched:
			touchTracker.multitouchTimeout = _originalMultitouchTimeout
			_timeoutPatched = False
			log.debug("touchExplore: restored original touchTracker.multitouchTimeout")
		super().terminate()

	def _enableTrackpadTouchScreen(self):
		mode = touchHandler.handler._curTouchMode if touchHandler.handler else "object"
		self._trackpadTouchScreen = TrackpadTouchScreen(mode=mode)
		self._trackpadTouchScreen.start()
		touchpadOsSettings.minimizeOsGestures()
		# The trackpad is still, physically, an ordinary mouse-class HID
		# device - it keeps generating real WM_MOUSEMOVE events in parallel
		# with the raw digitizer contacts this add-on reads directly, moving
		# the real OS mouse cursor along with the tracked finger. If NVDA's
		# own "report object under mouse pointer" setting is on
		# (config.conf["mouse"]["enableMouseTracking"]), NVDA's
		# mouseHandler.executeMouseMoveEvent() independently announces
		# whatever the real cursor passes over via its own event_mouseMove
		# pipeline - completely separate from, and not covered by, this
		# add-on's screenExplorer.moveTo patch, since that patch only
		# affects touch-explore's own announcement path. Confirmed directly:
		# "Desktop" (the desktop icon view's own name) was being spoken
		# between icons even though _patchedMoveTo's own debug logging
		# showed containerHit=True (correctly silent) for every one of those
		# hits - the speech was coming from mouse tracking, not touch
		# explore. Temporarily disabling mouse tracking while trackpad mode
		# is on removes the interference; the user's real preference is
		# restored exactly when trackpad mode is turned back off.
		self._savedMouseTrackingEnabled = config.conf["mouse"]["enableMouseTracking"]
		config.conf["mouse"]["enableMouseTracking"] = False
		log.debug("touchExplore: trackpad-as-touchscreen mode enabled")

	def _disableTrackpadTouchScreen(self):
		touchpadOsSettings.restoreOsGestures()
		self._trackpadTouchScreen.stop()
		self._trackpadTouchScreen = None
		if self._savedMouseTrackingEnabled is not None:
			config.conf["mouse"]["enableMouseTracking"] = self._savedMouseTrackingEnabled
			self._savedMouseTrackingEnabled = None
		log.debug("touchExplore: trackpad-as-touchscreen mode disabled")

	@script(
		# Translators: Input help mode message for the gesture that toggles
		# trackpad-as-touchscreen mode (using the laptop trackpad's raw
		# multi-touch contacts as if it were a touchscreen, mapped onto the
		# whole screen).
		description=_(
			"Toggles trackpad-as-touchscreen mode, letting you use touch "
			"gestures on a laptop trackpad as if it were a touchscreen",
		),
		gestures=("kb:NVDA+control+shift+t",),
	)
	def script_toggleTrackpadTouchScreen(self, gesture):
		if self._trackpadTouchScreen is None:
			try:
				self._enableTrackpadTouchScreen()
			except Exception:
				log.error("touchExplore: failed to enable trackpad-as-touchscreen mode", exc_info=True)
				self._trackpadTouchScreen = None
				# Translators: reported when trackpad-as-touchscreen mode
				# fails to start (e.g. no supported touchpad found).
				ui.message(_("Could not enable trackpad touchscreen mode"))
				return
			# Translators: reported when trackpad-as-touchscreen mode is turned on.
			ui.message(_("Trackpad touchscreen mode on"))
		else:
			self._disableTrackpadTouchScreen()
			# Translators: reported when trackpad-as-touchscreen mode is turned off.
			ui.message(_("Trackpad touchscreen mode off"))

	@script(
		# Translators: Input help mode message for the split-tap activation
		# gesture (hold one finger on an item, tap elsewhere with a second
		# finger to activate it, like a VoiceOver split-tap).
		description=_(
			"With one finger held on a touch-explored item, tap anywhere "
			"else on the screen with a second finger to activate that item",
		),
		gestures=("ts(object):1finger_hold+tap",),
	)
	def script_touchExploreSplitTapActivate(self, gesture):
		obj = touchHandler.handler.screenExplorer._obj
		if obj is None:
			return
		_activateObject(obj, gesture)
