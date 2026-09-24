# Touch Explore Sounds - touch gesture thresholds in physical units.
#
# NVDA's own tap/flick/pinch thresholds (touchTracker.maxAccidentalDrift,
# minFlickDistance, minPinchDistance) are plain module-level pixel counts,
# read by name every time a contact is classified. A fixed pixel count means
# a different physical distance on every device: on the development machine
# 25px was 3.2mm on its touchscreen (7.9 px/mm) but only 1.3mm on its
# trackpad in trackpad-as-touchscreen mode (120mm of pad stretched across
# 2304px = 19.2 px/mm). This module stores the thresholds in millimetres,
# separately per input source, and converts them to pixels for whichever
# source is currently active - reassigning the same touchTracker globals this
# add-on already patched (see CLAUDE.md), just with computed values instead
# of constants.
#
# multitouchTimeout is a time, not a distance, but is per source too, so the
# calibration wizard can tune each independently. In NVDA 2026.2 it governs
# three things at once (touchTracker.SingleTouchTracker.update and
# MultiTouchTracker.__init__): the longest a touch may last and still be a
# tap or flick, the window in which a second tap must START (measured from
# the first tap's start) to make a double tap, and how long a single tap
# waits before firing (in case it becomes a double tap).
#
# Defaults reproduce this add-on's previous fixed values on the development
# hardware (25px drift / 50px flick and pinch / 0.4s), so upgrading changes
# nothing there - only other hardware, whose pixel density differs, now gets
# the same physical feel instead of the same pixel count.

from ctypes import windll

import config
import touchTracker
from logHandler import log

TOUCHSCREEN = "touchscreen"
TRACKPAD = "trackpad"
SOURCES = (TOUCHSCREEN, TRACKPAD)

# (setting name, touchTracker global it drives)
_DISTANCE_SETTINGS = (
	("tapDriftMM", "maxAccidentalDrift"),
	("flickDistanceMM", "minFlickDistance"),
	("pinchDistanceMM", "minPinchDistance"),
)

SETTING_NAMES = ("tapDriftMM", "flickDistanceMM", "pinchDistanceMM", "timeoutMS")

DEFAULTS = {
	TOUCHSCREEN: {"tapDriftMM": 3.2, "flickDistanceMM": 6.3, "pinchDistanceMM": 6.3, "timeoutMS": 400},
	TRACKPAD: {"tapDriftMM": 1.3, "flickDistanceMM": 2.6, "pinchDistanceMM": 2.6, "timeoutMS": 400},
}

# Sanity limits, enforced by the settings panel and the calibration wizard.
MIN_DRIFT_MM = 0.3
MAX_DRIFT_MM = 15.0
MIN_FLICK_MM = 0.8
MAX_FLICK_MM = 40.0
MIN_TIMEOUT_MS = 150
MAX_TIMEOUT_MS = 1500
# A flick has to move clearly further than a tap may drift, or the two
# classifications overlap. Mirrors NVDA's own defaults (10px vs 50px) loosely.
MIN_FLICK_TO_DRIFT_RATIO = 1.5

CONFIG_SECTION = "touchExplore"


def _key(source, name):
	return f"{source}_{name}"


# Sound settings (see audioCues.py). Same config section, not per source.
SOUND_SPEC = {
	"soundsEnabled": "boolean(default=True)",
	"soundPack": 'option("earcons", "classic", default="earcons")',
	"roleSounds": "boolean(default=True)",
	"gapSound": "boolean(default=True)",
	"panSounds": "boolean(default=True)",
	"pitchSounds": "boolean(default=True)",
}


def allKeys():
	return list(SOUND_SPEC) + [_key(source, name) for source in SOURCES for name in SETTING_NAMES]


def registerConfig():
	spec = dict(SOUND_SPEC)
	for source in SOURCES:
		for name, value in DEFAULTS[source].items():
			kind = "integer" if isinstance(value, int) else "float"
			spec[_key(source, name)] = f"{kind}(default={value})"
	config.conf.spec[CONFIG_SECTION] = spec


def getSetting(source, name):
	return config.conf[CONFIG_SECTION][_key(source, name)]


def setSetting(source, name, value):
	config.conf[CONFIG_SECTION][_key(source, name)] = value


def validate(driftMM, flickMM, pinchMM, timeoutMS):
	"""Returns an error message for an invalid combination, or None."""
	if not MIN_DRIFT_MM <= driftMM <= MAX_DRIFT_MM:
		# Translators: validation error for the tap movement tolerance setting.
		return _("Tap movement tolerance must be between {lo} and {hi} millimetres.").format(
			lo=MIN_DRIFT_MM,
			hi=MAX_DRIFT_MM,
		)
	for value in (flickMM, pinchMM):
		if not MIN_FLICK_MM <= value <= MAX_FLICK_MM:
			# Translators: validation error for the flick/pinch distance settings.
			return _("Flick and pinch distances must be between {lo} and {hi} millimetres.").format(
				lo=MIN_FLICK_MM,
				hi=MAX_FLICK_MM,
			)
	# (Small tolerance: e.g. 0.8 * 1.5 is 1.2000000000000002 in floating point.)
	if flickMM < driftMM * MIN_FLICK_TO_DRIFT_RATIO - 1e-6:
		# Translators: validation error when the flick distance is too close to the tap tolerance.
		return _("Flick distance must be at least {ratio} times the tap movement tolerance.").format(
			ratio=MIN_FLICK_TO_DRIFT_RATIO,
		)
	if not MIN_TIMEOUT_MS <= timeoutMS <= MAX_TIMEOUT_MS:
		# Translators: validation error for the gesture timing setting.
		return _("Gesture time must be between {lo} and {hi} milliseconds.").format(
			lo=MIN_TIMEOUT_MS,
			hi=MAX_TIMEOUT_MS,
		)
	return None


# --- Physical screen density ---------------------------------------------
_HORZSIZE = 4
_VERTSIZE = 6
_LOGPIXELSX = 88
_DESKTOPVERTRES = 117
_DESKTOPHORZRES = 118


def screenPxPerMm():
	"""Physical pixels per millimetre of the primary display, from the
	panel's own reported size (EDID, via GetDeviceCaps HORZSIZE/VERTSIZE) -
	the real touchscreen is almost always the built-in, primary panel.
	DESKTOPHORZRES/DESKTOPVERTRES rather than HORZRES/VERTRES: the latter
	are scaled by Windows' display scaling for a thread that isn't DPI-aware
	(confirmed: 1536 instead of 2304 at 150% on the development machine),
	while touch coordinates are physical pixels. Falls back to the logical
	DPI (what NVDA's own touchHandler._getEdge uses) when the panel doesn't
	report a size. Development machine: 2304x1536 px over 291x194 mm.
	"""
	user32 = windll.user32
	gdi32 = windll.gdi32
	dc = user32.GetDC(0)
	try:
		widthMM = gdi32.GetDeviceCaps(dc, _HORZSIZE)
		heightMM = gdi32.GetDeviceCaps(dc, _VERTSIZE)
		if widthMM > 0 and heightMM > 0:
			return (
				gdi32.GetDeviceCaps(dc, _DESKTOPHORZRES) / widthMM + gdi32.GetDeviceCaps(dc, _DESKTOPVERTRES) / heightMM
			) / 2
		return (gdi32.GetDeviceCaps(dc, _LOGPIXELSX) or 96) / 25.4
	finally:
		user32.ReleaseDC(0, dc)


# --- Applying to touchTracker ---------------------------------------------
_originals = {}
_activeSource = None
_activePxPerMm = None


def _saveOriginals():
	if _originals:
		return
	for _name, globalName in _DISTANCE_SETTINGS:
		if hasattr(touchTracker, globalName):
			_originals[globalName] = getattr(touchTracker, globalName)
	_originals["multitouchTimeout"] = touchTracker.multitouchTimeout


def apply(source, pxPerMm):
	"""Sets touchTracker's globals from source's millimetre settings at
	pxPerMm. Safe to call from any thread: each is a single module-attribute
	assignment, and touchTracker reads them by name at classification time.
	Globals missing from the running NVDA version are skipped, not created.
	"""
	global _activeSource, _activePxPerMm
	_saveOriginals()
	for name, globalName in _DISTANCE_SETTINGS:
		if globalName in _originals:
			setattr(touchTracker, globalName, max(1, round(getSetting(source, name) * pxPerMm)))
	touchTracker.multitouchTimeout = getSetting(source, "timeoutMS") / 1000
	if (source, round(pxPerMm, 2)) != (_activeSource, _activePxPerMm):
		log.debug(
			f"touchExplore: applied {source} thresholds at {pxPerMm:.2f} px/mm: "
			f"drift={getattr(touchTracker, 'maxAccidentalDrift', None)}px "
			f"flick={getattr(touchTracker, 'minFlickDistance', None)}px "
			f"pinch={getattr(touchTracker, 'minPinchDistance', None)}px "
			f"timeout={touchTracker.multitouchTimeout}s",
		)
	_activeSource = source
	_activePxPerMm = round(pxPerMm, 2)


def activeSource():
	return _activeSource


def activePxPerMm():
	return _activePxPerMm


def reapply():
	"""Re-applies the active source's settings (after a settings change or a
	config profile switch), at the last known density.
	"""
	if _activeSource is not None:
		apply(_activeSource, _activePxPerMm)


def applyTouchscreen():
	apply(TOUCHSCREEN, screenPxPerMm())


def restoreOriginals():
	global _activeSource, _activePxPerMm
	for globalName, value in _originals.items():
		setattr(touchTracker, globalName, value)
	_originals.clear()
	_activeSource = _activePxPerMm = None
