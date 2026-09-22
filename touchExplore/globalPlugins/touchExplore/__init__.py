# Touch Explore Sounds
# A global plugin for NVDA that improves touch-explore feedback:
# - Plays a short tone when the finger lands on a real, actionable item
#   (icon, list item, button, link, etc).
# - Stays silent when the finger is over empty space inside a container
#   (e.g. the gap between desktop icons), instead of repeating the
#   container's "N items" description on every micro-movement.
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
from logHandler import log
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

_originalMoveTo = screenExplorer.ScreenExplorer.moveTo
_patched = False

# Tracks the last thing actually spoken by _patchedMoveTo, so we can collapse
# exact back-to-back duplicates that arise from an item being reachable via
# both the object path (speakObject) and the text/cell path (speakTextInfo).
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
		speech.speakObject(obj)
	if posChanged:
		self._pos = pos
		if not speechCanceled:
			speech.cancelSpeech()
		speech.speakTextInfo(pos, reason=controlTypes.OutputReason.CARET)


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
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
