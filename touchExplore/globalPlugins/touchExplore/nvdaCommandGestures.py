# Touch Explore Sounds - touch gestures for commands NVDA itself already has.
#
# GESTURES are (gesture identifier, NVDA script name) pairs. They're bound onto NVDA's own
# globalCommands.commands object rather than wrapped in a script of ours:
# - NVDA runs its own script natively - its checks, dialogs, messages and
#   press-count behaviour (screen curtain: once = until restart, twice =
#   permanent, via getLastScriptRepeatCount) exactly as for its keyboard
#   shortcut.
# - Input Gestures lists the touch gesture under NVDA's own command (e.g.
#   Vision > "Toggles the state of the screen curtain"), next to its
#   keyboard shortcut, and users can remove or change it there like any
#   default binding; their user gesture map takes precedence.
# - Nothing depends on which keyboard shortcut the command currently has -
#   the link is the script's name, which is also what NVDA's own gestures.ini
#   stores. If a future NVDA renames the script, bindGesture raises
#   LookupError at startup; that's logged and only this gesture is lost.

import types

import inputCore
from logHandler import log

GESTURES = (("ts:3finger_triple_tap", "toggleScreenCurtain"),)


def bind(commands):
	"""Binds GESTURES on commands (globalCommands.commands).
	Returns the gesture identifiers actually bound, for unbinding later.
	Never overwrites a binding NVDA itself already has for that gesture.
	"""
	bound = []
	for gestureId, scriptName in GESTURES:
		# ScriptableObject.getScript() reads only gesture.normalizedIdentifiers,
		# so a stand-in is enough to ask "does NVDA already bind this?".
		probe = types.SimpleNamespace(normalizedIdentifiers=(inputCore.normalizeGestureIdentifier(gestureId),))
		if commands.getScript(probe) is not None:
			log.warning(f"touchExplore: {gestureId} already bound by NVDA; not binding it to {scriptName}")
			continue
		try:
			commands.bindGesture(gestureId, scriptName)
		except LookupError:
			log.warning(f"touchExplore: NVDA has no {scriptName!r} command; {gestureId} left unbound", exc_info=True)
			continue
		bound.append(gestureId)
	return bound


def unbind(commands, bound):
	for gestureId in bound:
		try:
			commands.removeGestureBinding(gestureId)
		except LookupError:
			pass
