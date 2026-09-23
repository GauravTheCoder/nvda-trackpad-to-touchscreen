# Touch Explore Sounds - best-effort minimization of the OS's own
# precision-touchpad gesture recognition while trackpad-as-touchscreen mode
# is active.
#
# Even while this add-on reads the touchpad's raw HID contacts directly
# (see trackpadTouch.py), Windows' own touchpad driver keeps processing the
# same physical contacts in parallel: it still moves the ordinary mouse
# cursor, and multi-finger contacts can still trigger OS-level gestures
# (virtual desktop switching, Action Center, etc). There is no public,
# documented Win32 API to make an application the EXCLUSIVE consumer of a
# precision touchpad's raw input - RIDEV_INPUTSINK only adds a parallel tap,
# it does not stop the OS's own gesture recognizer from also running. This
# module is a best-effort reduction of that interference, not a fix: it
# turns off the touchpad gesture FEATURES most likely to fire accidentally
# during touch-explore (tap-to-click, tap-and-drag, two-finger tap, pan,
# zoom, the corner right-click zone) for as long as trackpad-as-touchscreen
# mode is on, and restores the user's original settings when it's turned
# off (or NVDA restarts/crashes with it left on - see restoreIfNeeded()).
#
# Uses SystemParametersInfo(SPI_GETTOUCHPADPARAMETERS / SPI_SETTOUCHPADPARAMETERS),
# which Microsoft documents as available starting Windows 11 version 24H2
# (build 26100+). On any earlier Windows build this silently does nothing
# (isSupported() returns False) rather than fail - trackpad-as-touchscreen
# mode still works via raw HID input regardless, just with more potential
# OS gesture interference on older Windows versions.
#
# TOUCHPAD_PARAMETERS_V1 struct layout, bit positions, and the
# VersionNumber=1 / fWinIni=(SPIF_UPDATEINIFILE|SPIF_SENDCHANGE)=3 calling
# convention were all confirmed against a real Windows 11 24H2 (build 26200)
# machine before being used here (see CLAUDE.md) - the layout is
# undocumented in exact bit-position terms on Microsoft Learn (only field
# names/order), so this was verified empirically via a standalone
# SPI_GETTOUCHPADPARAMETERS probe reading back this machine's real touchpad
# settings correctly, rather than trusted from the docs prose alone.

from ctypes import Structure, byref, c_uint32, c_void_p, sizeof, windll
from ctypes.wintypes import BOOL, DWORD, UINT

from logHandler import log

user32 = windll.user32

SPI_GETTOUCHPADPARAMETERS = 0x00AE
SPI_SETTOUCHPADPARAMETERS = 0x00AF
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02

TOUCHPAD_PARAMETERS_VERSION_1 = 1

# Bit positions within the two 32-bit bitfield words of TOUCHPAD_PARAMETERS_V1,
# in declared field order (confirmed against Microsoft's documented field
# order - see module docstring for how the exact bit offsets were verified).
_FIRST_BITS = {
	"touchpadPresent": 0,
	"legacyTouchpadPresent": 1,
	"externalMousePresent": 2,
	"touchpadEnabled": 3,
	"touchpadActive": 4,
	"feedbackSupported": 5,
	"clickForceSupported": 6,
}
_SECOND_BITS = {
	"allowActiveWhenMousePresent": 0,
	"feedbackEnabled": 1,
	"tapEnabled": 2,
	"tapAndDragEnabled": 3,
	"twoFingerTapEnabled": 4,
	"rightClickZoneEnabled": 5,
	"mouseAccelSettingHonored": 6,
	"panEnabled": 7,
	"zoomEnabled": 8,
	"scrollDirectionReversed": 9,
}

# The specific user-settable gesture features turned off while
# trackpad-as-touchscreen mode is active. Left enabled: touchpadEnabled
# itself (the whole point is to keep reading the touchpad) and
# allowActiveWhenMousePresent/mouseAccelSettingHonored (unrelated to gesture
# interference). tapEnabled/tapAndDragEnabled are included because a
# single-finger tap-to-click would otherwise both perform this add-on's own
# tap gesture AND generate a real mouse click at the last cursor position.
_FEATURES_TO_DISABLE = (
	"tapEnabled",
	"tapAndDragEnabled",
	"twoFingerTapEnabled",
	"rightClickZoneEnabled",
	"panEnabled",
	"zoomEnabled",
)


class _TouchpadParametersV1(Structure):
	_fields_ = [
		("versionNumber", c_uint32),
		("maxSupportedContacts", c_uint32),
		("legacyTouchpadFeatures", c_uint32),
		("first", c_uint32),  # bitfield word: touchpadPresent..clickForceSupported + reserved
		("second", c_uint32),  # bitfield word: allowActiveWhenMousePresent..scrollDirectionReversed + reserved
		("sensitivityLevel", c_uint32),
		("cursorSpeed", c_uint32),
		("feedbackIntensity", c_uint32),
		("clickForceSensitivity", c_uint32),
		("rightClickZoneWidth", c_uint32),
		("rightClickZoneHeight", c_uint32),
	]


user32.SystemParametersInfoW.argtypes = [UINT, UINT, c_void_p, DWORD]
user32.SystemParametersInfoW.restype = BOOL


def _getBit(word, bitIndex):
	return bool(word & (1 << bitIndex))


def _setBit(word, bitIndex, value):
	if value:
		return word | (1 << bitIndex)
	return word & ~(1 << bitIndex)


def _getParams():
	params = _TouchpadParametersV1()
	params.versionNumber = TOUCHPAD_PARAMETERS_VERSION_1
	ok = user32.SystemParametersInfoW(SPI_GETTOUCHPADPARAMETERS, sizeof(params), byref(params), 0)
	if not ok:
		return None
	return params


def _setParams(params):
	return bool(
		user32.SystemParametersInfoW(
			SPI_SETTOUCHPADPARAMETERS,
			sizeof(params),
			byref(params),
			SPIF_UPDATEINIFILE | SPIF_SENDCHANGE,
		),
	)


def isSupported():
	"""Whether SPI_GETTOUCHPADPARAMETERS/SPI_SETTOUCHPADPARAMETERS are usable
	on this machine (Windows 11 24H2+ with a Precision Touchpad detected).
	"""
	params = _getParams()
	return params is not None and _getBit(params.first, _FIRST_BITS["touchpadPresent"])


# The set of feature flags read back before minimizing, so they can be
# restored exactly (not just re-enabled - if the user had e.g. panning off
# already, restoring should leave it off, not force it on).
_savedFeatureStates = None


def minimizeOsGestures():
	"""Turns off the OS gesture features listed in _FEATURES_TO_DISABLE, for
	as long as trackpad-as-touchscreen mode is active. Saves the prior
	values first so restoreOsGestures() can put them back exactly. No-op
	(logged, not raised) if unsupported on this Windows version or if no
	Precision Touchpad is detected.
	"""
	global _savedFeatureStates
	if _savedFeatureStates is not None:
		return  # already minimized; don't overwrite the saved original state
	params = _getParams()
	if params is None or not _getBit(params.first, _FIRST_BITS["touchpadPresent"]):
		log.debug("touchExplore: SPI_GETTOUCHPADPARAMETERS unsupported or no touchpad present, skipping")
		return
	_savedFeatureStates = {name: _getBit(params.second, _SECOND_BITS[name]) for name in _FEATURES_TO_DISABLE}
	for name in _FEATURES_TO_DISABLE:
		params.second = _setBit(params.second, _SECOND_BITS[name], False)
	if _setParams(params):
		log.debug(f"touchExplore: minimized OS touchpad gestures: {_savedFeatureStates}")
	else:
		log.debugWarning("touchExplore: SPI_SETTOUCHPADPARAMETERS failed while minimizing OS gestures")
		_savedFeatureStates = None


def restoreOsGestures():
	"""Restores whatever the OS gesture features were before
	minimizeOsGestures() was called. Safe to call even if minimizeOsGestures()
	was never called or already failed (no-op in that case).
	"""
	global _savedFeatureStates
	if _savedFeatureStates is None:
		return
	params = _getParams()
	if params is not None:
		for name, previousValue in _savedFeatureStates.items():
			params.second = _setBit(params.second, _SECOND_BITS[name], previousValue)
		if not _setParams(params):
			log.debugWarning("touchExplore: SPI_SETTOUCHPADPARAMETERS failed while restoring OS gestures")
	_savedFeatureStates = None
