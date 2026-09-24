# Touch Explore Sounds - a plain-text report of everything about this
# machine's touch setup that has mattered when diagnosing problems (see
# CLAUDE.md): versions, touch hardware, pixel densities, the thresholds
# actually in effect, and each touchpad's HID capabilities. Meant to be
# pasted into a bug report, so a problem on hardware the developer doesn't
# have can be understood without guessing.

from ctypes import windll

import config
import touchHandler
import touchTracker

from . import touchpadOsSettings, touchSettings, trackpadTouch

_SM_MAXIMUMTOUCHES = 95


def _confValue(section, key):
	# NVDA's config sections aren't guaranteed to support dict-style
	# .get()/.items(); a missing key (e.g. edgeGestures before NVDA had it)
	# raises KeyError.
	try:
		return config.conf[section][key]
	except KeyError:
		return "n/a"


def _section(lines, title, producer):
	lines.append(f"[{title}]")
	try:
		lines.extend(producer())
	except Exception as e:
		lines.append(f"error: {e!r}")
	lines.append("")


def _versions():
	import addonHandler
	import versionInfo
	import winVersion

	addon = addonHandler.getCodeAddon()
	return [
		f"NVDA: {versionInfo.version}",
		f"Add-on: {addon.manifest['version']}",
		f"Windows: {winVersion.getWinVer()}",
	]


def _touch(trackpadScreen):
	handler = touchHandler.handler
	return [
		f"Touchscreen max touches (SM_MAXIMUMTOUCHES): {windll.user32.GetSystemMetrics(_SM_MAXIMUMTOUCHES)}",
		f"touchHandler.handler: {type(handler).__name__ if handler is not None else None}",
		f"Trackpad touchscreen mode: {'on' if trackpadScreen is not None else 'off'}",
		f"OS touchpad-gesture minimisation supported: {touchpadOsSettings.isSupported()}",
		f"Edge gestures setting: {_confValue('touch', 'edgeGestures')}",
	]


def _thresholds():
	lines = [
		f"Screen px/mm: {touchSettings.screenPxPerMm():.2f}",
		f"Active source: {touchSettings.activeSource()} at {touchSettings.activePxPerMm()} px/mm",
	]
	for name in ("maxAccidentalDrift", "minFlickDistance", "minPinchDistance", "multitouchTimeout"):
		lines.append(f"touchTracker.{name} = {getattr(touchTracker, name, 'n/a')}")
	lines.extend(
		f"setting {key} = {_confValue(touchSettings.CONFIG_SECTION, key)}" for key in touchSettings.allKeys()
	)
	return lines


def _touchpads(trackpadScreen):
	lines = []
	devices = trackpadTouch.findTouchpadDevices()
	if not devices:
		return ["No Precision Touchpad (HID 0x0D/0x05) found"]
	for hDevice in devices:
		info = trackpadTouch._getDeviceInfo(hDevice)
		lines.append(f"Device {hDevice}: {trackpadTouch._getDeviceName(hDevice)}")
		if info is not None:
			lines.append(f"  VID 0x{info.VendorId:04X} PID 0x{info.ProductId:04X} version {info.VersionNumber}")
		parser = None
		interval = None
		if trackpadScreen is not None:
			parser = trackpadScreen._parsersByDevice.get(hDevice)
			interval = trackpadScreen._reportIntervals.get(hDevice)
		if parser is None:
			parser = trackpadTouch._buildParser(hDevice)
		lines.append(f"  {parser.describe() if parser else 'no usable HID layout'}")
		if interval:
			lines.append(f"  measured report interval: {interval * 1000:.1f} ms")
	return lines


def collect(trackpadScreen):
	lines = ["Touch Explore Sounds diagnostics", ""]
	_section(lines, "Versions", _versions)
	_section(lines, "Touch", lambda: _touch(trackpadScreen))
	_section(lines, "Thresholds", _thresholds)
	_section(lines, "Touchpads", lambda: _touchpads(trackpadScreen))
	return "\n".join(lines)
