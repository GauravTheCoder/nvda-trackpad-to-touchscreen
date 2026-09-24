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

import os

import api
import config
import controlTypes
import globalPluginHandler
import locationHelper
import nvwave
import screenExplorer
import speech
import textInfos
import touchHandler
import touchTracker
import ui
from comtypes import COMError
from logHandler import log
from NVDAObjects import NVDAObject
from scriptHandler import script
from utils.security import objectBelowLockScreenAndWindowsIsLocked

from . import touchpadOsSettings
from . import virtualDesktop
from .trackpadTouch import TrackpadTouchScreen

_SOUNDS_DIR = os.path.join(os.path.dirname(__file__), "sounds")
EXPLORE_SOUND_PATH = os.path.join(_SOUNDS_DIR, "explore.wav")
CLICK_SOUND_PATH = os.path.join(_SOUNDS_DIR, "click.wav")


def _playSound(path: str) -> None:
	"""Plays a bundled UI sound asynchronously, matching how NVDA plays its
	own built-in sounds (waves/*.wav via nvwave.playWaveFile). Sounds are
	WAV, not the MP3 they were originally supplied as - nvwave.playWaveFile
	uses the stdlib wave module internally (wave.open(fileName, "r")),
	which only reads WAV; there is no MP3 decoding anywhere in NVDA itself,
	and this add-on has no external dependencies to add one. Converted once
	with ffmpeg to 22050 Hz mono 16-bit PCM, matching NVDA's own waves/*.wav
	files exactly (confirmed by inspecting one, e.g. waves/browseMode.wav)
	rather than guessing a format nvwave would accept.
	"""
	try:
		nvwave.playWaveFile(path)
	except Exception:
		log.debugWarning(f"touchExplore: failed to play sound {path!r}", exc_info=True)

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
	moment the second finger taps. Plays a click sound but otherwise stays
	silent (no "Activate"/action-name speech), unlike a regular double-tap.
	"""
	while obj and not objectBelowLockScreenAndWindowsIsLocked(obj):
		try:
			obj.doAction()
			_playSound(CLICK_SOUND_PATH)
			if isinstance(gesture, touchHandler.TouchInputGesture):
				touchHandler.handler.notifyInteraction(obj)
			return
		except NotImplementedError:
			obj = obj.parent


def _navigateAndAnnounce(newObj) -> None:
	"""Sets newObj as the navigator object and announces it, mirroring
	globalCommands.py's own script_navigatorObject_next/_previous/
	_nextInFlow/_previousInFlow - except those stock scripts only ever move
	the navigator/review position, never real OS focus or selection (unlike
	this add-on's own touch-explore path via _touchSelect), so flicking
	through a list reports "not selected" on every item regardless of
	context, the same way keyboard-based object navigation
	(NVDA+numpad6/4/8/2, etc) always has - it's stock NVDA behavior, not
	something touch/trackpad-specific.

	This makes flick-based object navigation match touch-explore's own
	behavior instead: if newObj is focusable, _touchSelect() moves real
	focus/selection to it (same as touching it directly would), and NVDA's
	own event hooks announce it correctly (role suppression, accurate
	selection state) - so we don't call speech.speakObject() ourselves here,
	same reasoning as _patchedMoveTo (see its comments). If newObj isn't
	focusable (plain static content, text, etc - not every navigable object
	is a selectable control), _touchSelect() is a no-op, so we fall back to
	announcing it exactly like the stock scripts do.

	Virtual-desktop guard: creating/switching virtual desktops (Ctrl+Win+D)
	doesn't destroy or move windows, only hides them, so the navigator
	object can still be sitting inside a window that's no longer on the
	visible desktop (e.g. WhatsApp, if it was touch-explored right before
	switching desktops). Walking further from there via simpleNext/etc and
	then calling _touchSelect() would move REAL OS focus/selection into
	that hidden window - not just stale narration, an actual focus-steal
	into an app the user can't see. Skip the real focus/selection move (and
	fall back to plain narration, matching the "not focusable" branch
	below) whenever newObj's window is confirmed to be on a different
	virtual desktop than the current one. See virtualDesktop.py.
	"""
	if not api.setNavigatorObject(newObj):
		import gui

		ui.reviewMessage(gui.blockAction.Context.WINDOWS_LOCKED.translatedMessage)
		return
	onCurrentDesktop = virtualDesktop.isOnCurrentVirtualDesktop(getattr(newObj, "windowHandle", None))
	if onCurrentDesktop is False:
		log.debug(
			f"touchExplore: _navigateAndAnnounce newObj={newObj!r} is on a "
			"different virtual desktop - skipping real focus/selection move",
		)
		speech.speakObject(newObj, reason=controlTypes.OutputReason.FOCUS)
		return
	statesBefore = newObj.states
	_touchSelect(newObj)
	if controlTypes.State.FOCUSABLE not in statesBefore or controlTypes.State.FOCUSED in statesBefore:
		# _touchSelect() was a no-op (not focusable, or already focused) -
		# nothing will announce newObj on its own, so do it ourselves,
		# matching the stock scripts' own fallback behavior.
		speech.speakObject(newObj, reason=controlTypes.OutputReason.FOCUS)


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
		_playSound(EXPLORE_SOUND_PATH)
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

	@script(
		description=_(
			# Translators: Input help mode message for activate current
			# object command (same-spot double-tap).
			"Performs the default action on the current navigator object "
			"(example: presses it if it is a button).",
		),
		gestures=("ts:double_tap",),
	)
	def script_touchExploreDoubleTapActivate(self, gesture):
		"""Same-spot double-tap activation. Overrides NVDA's own stock
		globalCommands.script_review_activate for touch input specifically
		(that gesture, "ts:double_tap", is also bound to kb:NVDA+numpadEnter/
		kb(laptop):NVDA+enter - those keyboard gestures are untouched in
		effect, since the click sound and silence below are both gated on
		isinstance(gesture, touchHandler.TouchInputGesture); a keyboard
		activation via this same script still calls doAction()/pos.activate()
		normally, it just doesn't get the touch-only sound). Mirrors
		_activateObject (this add-on's split-tap activation): plays a click
		sound on success and stays otherwise silent (no "Activate"/
		action-name speech) instead of stock's ui.message() announcement -
		the user found the per-icon "Activate"/"Double Click" wording
		variance (genuine, per-object MSAA accDefaultAction text, see
		"Diagnosed and clarified" in CLAUDE.md) more confusing than useful,
		and asked for just the confirmation sound, matching split-tap.
		"""
		pos = api.getReviewPosition()
		if objectBelowLockScreenAndWindowsIsLocked(pos.obj):
			import gui

			ui.message(gui.blockAction.Context.WINDOWS_LOCKED.translatedMessage)
			return
		isTouch = isinstance(gesture, touchHandler.TouchInputGesture)
		try:
			pos.activate()
			if isTouch:
				_playSound(CLICK_SOUND_PATH)
				touchHandler.handler.notifyInteraction(pos.NVDAObjectAtStart)
			return
		except NotImplementedError:
			pass
		obj = api.getNavigatorObject()
		while obj and not objectBelowLockScreenAndWindowsIsLocked(obj):
			try:
				obj.doAction()
				if isTouch:
					_playSound(CLICK_SOUND_PATH)
					touchHandler.handler.notifyInteraction(obj)
				return
			except NotImplementedError:
				pass
			obj = obj.parent
		# Translators: the message reported when there is no action to
		# perform on the review position or navigator object.
		ui.message(_("No action"))

	# --- Flick-based object navigation with real focus/selection ----------
	# NVDA's own stock ts(object):flick*/2finger_flick* scripts (in
	# globalCommands.py: script_navigatorObject_parent/_firstChild/_next/
	# _previous/_nextInFlow/_previousInFlow) only ever move the navigator/
	# review position - unlike this add-on's own touch-explore path
	# (_patchedMoveTo -> _touchSelect), they never touch real OS focus or
	# selection. That makes "selected"/"not selected" state speech fire on
	# every flicked-to item regardless of context, exactly the way it would
	# via keyboard-based object navigation on stock NVDA - not a bug
	# introduced by trackpad mode, but inconsistent with how touch-explore
	# already behaves in this add-on. Binding our own scripts to the same
	# gesture IDs takes priority over globalCommands's bindings for touch
	# input specifically (scriptHandler resolves global plugin scripts
	# before globalCommands.GlobalCommands - the same mechanism this add-on
	# already relies on for its own split-tap gesture above) while leaving
	# the keyboard equivalents (NVDA+numpad6, etc) completely untouched, so
	# only touch/trackpad flicks get the real-selection treatment.
	# Movement logic mirrors each stock script's exactly (same simpleNext/
	# simplePrevious/simpleParent/simpleFirstChild/simpleReviewMode
	# handling), swapping only the final "set navigator object and
	# announce" step for _navigateAndAnnounce().

	def _getCurrentNavigatorObjectOrReport(self):
		curObject = api.getNavigatorObject()
		if not isinstance(curObject, NVDAObject):
			# Translators: Reported when the user tries to perform a command
			# related to the navigator object but there is no current
			# navigator object.
			ui.reviewMessage(_("No navigator object"))
			return None
		return curObject

	@script(
		description=_(
			# Translators: Input help mode message for move to parent object command.
			"Moves the navigator object to the object containing it",
		),
		gestures=("ts(object):flickup",),
	)
	def script_touchExploreFlickParent(self, gesture):
		curObject = self._getCurrentNavigatorObjectOrReport()
		if curObject is None:
			return
		simpleReviewMode = config.conf["reviewCursor"]["simpleReviewMode"]
		newObject = curObject.simpleParent if simpleReviewMode else curObject.parent
		if newObject is None:
			# Translators: Reported when there is no containing (parent)
			# object such as when focused on desktop.
			ui.reviewMessage(_("No containing object"))
			return
		_navigateAndAnnounce(newObject)

	@script(
		description=_(
			# Translators: Input help mode message for move to first child object command.
			"Moves the navigator object to the first object inside it",
		),
		gestures=("ts(object):flickdown",),
	)
	def script_touchExploreFlickFirstChild(self, gesture):
		curObject = self._getCurrentNavigatorObjectOrReport()
		if curObject is None:
			return
		simpleReviewMode = config.conf["reviewCursor"]["simpleReviewMode"]
		newObject = curObject.simpleFirstChild if simpleReviewMode else curObject.firstChild
		if newObject is None:
			# Translators: Reported when there is no contained (first
			# child) object such as inside a document.
			ui.reviewMessage(_("No objects inside"))
			return
		_navigateAndAnnounce(newObject)

	@script(
		description=_(
			# Translators: Input help mode message for move to next object command.
			"Moves the navigator object to the next object",
		),
		gestures=("ts(object):2finger_flickright",),
	)
	def script_touchExploreFlickNext(self, gesture):
		curObject = self._getCurrentNavigatorObjectOrReport()
		if curObject is None:
			return
		simpleReviewMode = config.conf["reviewCursor"]["simpleReviewMode"]
		newObject = curObject.simpleNext if simpleReviewMode else curObject.next
		if newObject is None:
			# Translators: Reported when there is no next object (current
			# object is the last object).
			ui.reviewMessage(_("No next"))
			return
		_navigateAndAnnounce(newObject)

	@script(
		description=_(
			# Translators: Input help mode message for move to previous object command.
			"Moves the navigator object to the previous object",
		),
		gestures=("ts(object):2finger_flickleft",),
	)
	def script_touchExploreFlickPrevious(self, gesture):
		curObject = self._getCurrentNavigatorObjectOrReport()
		if curObject is None:
			return
		simpleReviewMode = config.conf["reviewCursor"]["simpleReviewMode"]
		newObject = curObject.simplePrevious if simpleReviewMode else curObject.previous
		if newObject is None:
			# Translators: Reported when there is no previous object
			# (current object is the first object).
			ui.reviewMessage(_("No previous"))
			return
		_navigateAndAnnounce(newObject)

	@script(
		description=_(
			# Translators: Input help mode message for a touchscreen gesture.
			"Moves to the next object in a flattened view of the object navigation hierarchy",
		),
		gestures=("ts(object):flickright",),
	)
	def script_touchExploreFlickNextInFlow(self, gesture):
		curObject = self._getCurrentNavigatorObjectOrReport()
		if curObject is None:
			return
		newObject = None
		if curObject.simpleFirstChild:
			newObject = curObject.simpleFirstChild
		elif curObject.simpleNext:
			newObject = curObject.simpleNext
		elif curObject.simpleParent:
			parent = curObject.simpleParent
			while parent and not parent.simpleNext:
				parent = parent.simpleParent
			if parent:
				newObject = parent.simpleNext
		if not newObject:
			# Translators: a message when there is no next object when navigating
			ui.reviewMessage(_("No next"))
			return
		_navigateAndAnnounce(newObject)

	@script(
		description=_(
			# Translators: Input help mode message for a touchscreen gesture.
			"Moves to the previous object in a flattened view of the object navigation hierarchy",
		),
		gestures=("ts(object):flickleft",),
	)
	def script_touchExploreFlickPreviousInFlow(self, gesture):
		curObject = self._getCurrentNavigatorObjectOrReport()
		if curObject is None:
			return
		newObject = curObject.simplePrevious
		if newObject:
			while newObject.simpleLastChild:
				newObject = newObject.simpleLastChild
		else:
			newObject = curObject.simpleParent
		if not newObject:
			# Translators: a message when there is no previous object when navigating
			ui.reviewMessage(_("No previous"))
			return
		_navigateAndAnnounce(newObject)
