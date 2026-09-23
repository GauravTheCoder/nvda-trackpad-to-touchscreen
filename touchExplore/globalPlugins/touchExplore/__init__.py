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
#
# Touch explore-by-touch reporting lives in
# screenExplorer.ScreenExplorer.moveTo(), called directly by touchHandler
# as the finger moves. There's no extension point for it, and touchHandler
# can recreate ScreenExplorer instances at any time (e.g. when touch
# support is toggled or a config profile switch occurs), so rather than
# patching one instance, we patch the moveTo method on the class itself.

import api
import controlTypes
import globalPluginHandler
import locationHelper
import screenExplorer
import speech
import textInfos
import tones
import touchHandler
from comtypes import COMError
from logHandler import log
from scriptHandler import script
from utils.security import objectBelowLockScreenAndWindowsIsLocked

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
		global _patched
		if not _patched:
			screenExplorer.ScreenExplorer.moveTo = _patchedMoveTo
			_patched = True
			log.debug("touchExplore: patched ScreenExplorer.moveTo")

	def terminate(self):
		global _patched
		if _patched:
			screenExplorer.ScreenExplorer.moveTo = _originalMoveTo
			_patched = False
			log.debug("touchExplore: restored original ScreenExplorer.moveTo")
		super().terminate()

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
