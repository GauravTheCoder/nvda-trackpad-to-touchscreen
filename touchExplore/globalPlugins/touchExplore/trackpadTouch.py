# Touch Explore Sounds - trackpad-as-touchscreen mode.
#
# Reads the Windows Precision Touchpad's raw HID multi-touch contacts
# directly (independent of the OS mouse cursor and independent of whatever
# gestures Windows itself recognizes from the same physical trackpad), maps
# each contact proportionally onto the screen, and feeds them into NVDA's
# real touch pipeline (touchTracker.TrackerManager -> TouchInputGesture ->
# inputCore.manager.executeGesture) exactly as touchHandler.TouchHandler
# does for genuine touchscreen hardware. This makes every touch gesture -
# NVDA's own plus this add-on's split-tap - available from a trackpad on a
# machine with no touchscreen at all.
#
# Why raw HID rather than the mouse/cursor position: touchHandler's own
# touchSupported() requires GetSystemMetrics(SM_MAXIMUMTOUCHES) > 0, i.e. a
# real digitizer, so touchHandler.handler never exists on a trackpad-only
# machine - there is nothing to piggyback on. A trackpad exposed as a mouse
# only ever reports one cursor position at a time (multi-finger OS gestures
# are consumed by the trackpad driver before they reach an app as distinct
# points), so genuine multi-finger detection requires reading the
# touchpad's own HID digitizer collection (Usage Page 0x0D, Usage 0x05)
# directly via the Raw Input API, bypassing the OS cursor entirely.
#
# Confirmed by instrumented testing on real hardware (see CLAUDE.md):
#   - The touchpad enumerates as TWO separate raw input HID devices with
#     the same UsagePage/Usage/VendorId/ProductId: a real PnP device node
#     (openable via CreateFileW, used by the OS's own touchpad driver) and
#     a synthetic "\\?\Microsoft HID RID\..." device that raw input
#     messages actually arrive from. CreateFileW on the synthetic device's
#     own name fails (ERROR_PATH_NOT_FOUND); it must not be opened at all.
#   - The synthetic device has its OWN preparsed HID data, fetchable via
#     GetRawInputDeviceInfo(RIDI_PREPARSEDDATA) directly on its raw-input
#     handle - no CreateFileW/HidD_GetPreparsedData needed for it. Its
#     report layout (confirmed: report ID 1, 182-byte reports, 5 contact
#     link collections) does NOT match its PnP sibling's (report ID 129,
#     50-byte reports) - they are independent report descriptors and must
#     not be cross-matched; HidP_GetUsageValue against the wrong preparsed
#     data fails with HIDP_STATUS_INCOMPATIBLE_REPORT_ID.
#   - The Windows Precision Touchpad spec documents Tip Switch (0x0D, 0x42)
#     as mandatory, but this hardware's report doesn't expose it at all
#     ((0x0D, 0x30) is Pressure, not Tip - easy to confuse, confirmed
#     against the Windows Precision Touchpad Collection reference
#     touchpad-windows-precision-touchpad-collection). Contact liveness is
#     therefore determined from the device-level Contact Count usage
#     (0x0D, 0x54) instead - see _DeviceParser.decodeContacts.
#   - A capability-only CreateFileW open of a HID device (dwDesiredAccess=0)
#     succeeds even while the OS's own touchpad driver holds the device for
#     read/write; requesting GENERIC_READ|GENERIC_WRITE fails with
#     ERROR_SHARING_VIOLATION. Irrelevant now that RIDI_PREPARSEDDATA avoids
#     CreateFileW for the synthetic device entirely, but kept as a fallback
#     path in case a future/different touchpad doesn't support
#     RIDI_PREPARSEDDATA on its raw-input handle.
#   - Raw input device handles are NOT stable across process restart; a
#     fresh device search (by UsagePage/Usage, and for the PnP fallback
#     path also VendorId/ProductId) must be done each time this starts.

import threading
import time
from ctypes import (
	POINTER,
	GetLastError,
	Structure,
	byref,
	c_int,
	c_long,
	c_uint32,
	c_ubyte,
	c_void_p,
	cast,
	create_string_buffer,
	create_unicode_buffer,
	sizeof,
	windll,
)
from ctypes.wintypes import (
	BOOL,
	BOOLEAN,
	DWORD,
	HANDLE,
	HWND,
	LPVOID,
	MSG,
	ULONG,
	UINT,
	USHORT,
	WPARAM,
)

import core
import gui
import inputCore
import screenExplorer
import touchHandler
import touchTracker
import winUser
from logHandler import log

user32 = windll.user32
kernel32 = windll.kernel32
hidDll = windll.hid

# --- Win32 / HID constants (verified against real hardware; see module
# docstring and CLAUDE.md for how) ---------------------------------------
WM_INPUT = 0x00FF
WM_QUIT = 0x0012
WM_TIMER = 0x0113
# How often to re-feed each currently-down contact's last known position into
# the tracker manager even when no new HID report has arrived. Needed because
# touchTracker.SingleTouchTracker's tap-vs-hover classification is time-based
# (elapsed wall-clock time since the contact started) but is only actually
# evaluated when TrackerManager.update() is called - and this hardware's HID
# driver stops sending reports entirely for a contact that isn't moving
# (confirmed directly: a gap of over a second with zero WM_INPUT messages
# during a quick, genuine multi-finger tap attempt - see CLAUDE.md). Without
# a periodic poll, a real tap/flick can silently miss its own classification
# window and lock in as a plain hover once multitouchTimeout (250ms) elapses
# with no intervening update() call to catch it while still within budget.
CONTACT_POLL_INTERVAL_MS = 20
POLL_TIMER_ID = 1
# How long a contact may go without a REAL HID report (not counting this
# add-on's own timer-poll re-feeds, which deliberately don't count) before
# it's treated as lifted even though the hardware never sent an explicit
# lift/contact-count-drop report for it. Necessary because this hardware
# stops sending ANY report at all - not just for a stationary contact, but
# also, confirmed directly, for an ACTUAL LIFT following a fast flick - so
# "no new real report" cannot by itself distinguish "finger genuinely still
# down, not moving" from "finger already lifted, driver just didn't say so."
# Deliberately short: prioritizes tap/flick gestures actually being
# classifiable (touchTracker.SingleTouchTracker can only classify a contact
# as a tap or flick at the moment it's told the contact is complete/lifted -
# see CLAUDE.md) over perfectly supporting an intentionally long, perfectly
# motionless hold - accepted tradeoff after asking the user directly, since
# taps/flicks were the reported-broken behavior and holds were not.
LIFT_INFERENCE_TIMEOUT_S = 0.08
RID_INPUT = 0x10000003
RIDEV_INPUTSINK = 0x00000100
RIDEV_REMOVE = 0x00000001
RIDEV_NOLEGACY = 0x00000030
RIM_TYPEHID = 2
RIDI_DEVICENAME = 0x20000007
RIDI_DEVICEINFO = 0x2000000B
RIDI_PREPARSEDDATA = 0x20000005
HWND_MESSAGE = -3
SM_CXSCREEN = 0
SM_CYSCREEN = 1

GENERIC_ZERO_ACCESS = 0
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3

HidP_Input = 0
HIDP_STATUS_SUCCESS = 0x00110000

USAGE_PAGE_DIGITIZER = 0x0D
USAGE_TOUCHPAD = 0x05
USAGE_PAGE_GENERIC_DESKTOP = 0x01
USAGE_MOUSE = 0x02
USAGE_X = 0x30
USAGE_Y = 0x31
USAGE_CONTACT_ID = 0x51
USAGE_CONTACT_COUNT = 0x54


class RAWINPUTDEVICE(Structure):
	_fields_ = [
		("usUsagePage", USHORT),
		("usUsage", USHORT),
		("dwFlags", DWORD),
		("hwndTarget", HWND),
	]


class RAWINPUTHEADER(Structure):
	_fields_ = [
		("dwType", DWORD),
		("dwSize", DWORD),
		("hDevice", HANDLE),
		("wParam", WPARAM),
	]


class RAWINPUTDEVICELIST(Structure):
	_fields_ = [
		("hDevice", HANDLE),
		("dwType", DWORD),
	]


class RID_DEVICE_INFO_HID(Structure):
	_fields_ = [
		("cbSize", DWORD),
		("dwType", DWORD),
		("VendorId", DWORD),
		("ProductId", DWORD),
		("VersionNumber", DWORD),
		("UsagePage", USHORT),
		("Usage", USHORT),
	]


class HIDP_CAPS(Structure):
	_fields_ = [
		("Usage", USHORT),
		("UsagePage", USHORT),
		("InputReportByteLength", USHORT),
		("OutputReportByteLength", USHORT),
		("FeatureReportByteLength", USHORT),
		("Reserved", USHORT * 17),
		("NumberLinkCollectionNodes", USHORT),
		("NumberInputButtonCaps", USHORT),
		("NumberInputValueCaps", USHORT),
		("NumberInputDataIndices", USHORT),
		("NumberOutputButtonCaps", USHORT),
		("NumberOutputValueCaps", USHORT),
		("NumberOutputDataIndices", USHORT),
		("NumberFeatureButtonCaps", USHORT),
		("NumberFeatureValueCaps", USHORT),
		("NumberFeatureDataIndices", USHORT),
	]


class _HIDP_VALUE_CAPS_RANGE(Structure):
	_fields_ = [
		("UsageMin", USHORT),
		("UsageMax", USHORT),
		("StringMin", USHORT),
		("StringMax", USHORT),
		("DesignatorMin", USHORT),
		("DesignatorMax", USHORT),
		("DataIndexMin", USHORT),
		("DataIndexMax", USHORT),
	]


class HIDP_VALUE_CAPS(Structure):
	_fields_ = [
		("UsagePage", USHORT),
		("ReportID", c_ubyte),
		("IsAlias", BOOLEAN),
		("BitField", USHORT),
		("LinkCollection", USHORT),
		("LinkUsage", USHORT),
		("LinkUsagePage", USHORT),
		("IsRange", BOOLEAN),
		("IsStringRange", BOOLEAN),
		("IsDesignatorRange", BOOLEAN),
		("IsAbsolute", BOOLEAN),
		("HasNull", BOOLEAN),
		("Reserved", c_ubyte),
		("BitSize", USHORT),
		("ReportCount", USHORT),
		("Reserved2", USHORT * 5),
		("UnitsExp", ULONG),
		("Units", ULONG),
		("LogicalMin", c_long),
		("LogicalMax", c_long),
		("PhysicalMin", c_long),
		("PhysicalMax", c_long),
		# Only UsageMin (aliased with NotRange.Usage at the same offset) is
		# ever read here (single, non-range usages), so this shared layout
		# covers both union members we care about without a real ctypes Union.
		("RangeOrNotRange", _HIDP_VALUE_CAPS_RANGE),
	]


# Explicit argtypes/restype for every Win32 function this module calls.
# ctypes' default (untyped) marshaling guesses too narrow a C type for
# 64-bit pointer/handle values (e.g. a >2GB module base address or window
# handle) and either truncates them or raises OverflowError - confirmed
# directly while developing this module (see CLAUDE.md): GetModuleHandleW
# untyped truncated its return value, and passing that truncated value on to
# CreateWindowExW's hInstance parameter failed outright once the return
# value was corrected but the call itself was left untyped. touchHandler.py
# calls several of these same functions (RegisterClassExW, CreateWindowExW,
# GetMessageW, DefWindowProcW, DestroyWindow, GetSystemMetrics,
# PostThreadMessageW, GetModuleHandleW) without setting argtypes/restype at
# all - ctypes argtypes/restype assignments are attached to the underlying
# function object and are process-global, but a CORRECT, fully-general type
# declaration only accepts every value a legitimate caller would pass in the
# first place, so declaring the right types here cannot break
# touchHandler.py's own calls to the same functions.
user32.GetRawInputDeviceList.argtypes = [c_void_p, POINTER(UINT), UINT]
user32.GetRawInputDeviceList.restype = UINT
user32.GetRawInputDeviceInfoW.argtypes = [HANDLE, UINT, c_void_p, POINTER(UINT)]
user32.GetRawInputDeviceInfoW.restype = c_int
user32.RegisterRawInputDevices.argtypes = [POINTER(RAWINPUTDEVICE), UINT, UINT]
user32.RegisterRawInputDevices.restype = BOOL
user32.GetRawInputData.argtypes = [HANDLE, UINT, c_void_p, POINTER(UINT), UINT]
user32.GetRawInputData.restype = c_uint32
user32.RegisterClassExW.argtypes = [POINTER(winUser.WNDCLASSEXW)]
user32.RegisterClassExW.restype = USHORT  # ATOM
user32.CreateWindowExW.argtypes = [
	DWORD,
	c_void_p,  # class atom (low word) or class name pointer
	c_void_p,  # window name pointer
	DWORD,
	c_int,
	c_int,
	c_int,
	c_int,
	HWND,
	HANDLE,
	HANDLE,
	LPVOID,
]
user32.CreateWindowExW.restype = HWND
user32.DefWindowProcW.argtypes = [HWND, UINT, WPARAM, c_void_p]
user32.DefWindowProcW.restype = c_long
user32.DestroyWindow.argtypes = [HWND]
user32.DestroyWindow.restype = BOOL
user32.UnregisterClassW.argtypes = [c_void_p, HANDLE]
user32.UnregisterClassW.restype = BOOL
user32.GetSystemMetrics.argtypes = [c_int]
user32.GetSystemMetrics.restype = c_int
user32.GetMessageW.argtypes = [POINTER(MSG), HWND, UINT, UINT]
user32.GetMessageW.restype = c_int
user32.TranslateMessage.argtypes = [POINTER(MSG)]
user32.TranslateMessage.restype = BOOL
user32.DispatchMessageW.argtypes = [POINTER(MSG)]
user32.DispatchMessageW.restype = c_long
user32.PostThreadMessageW.argtypes = [DWORD, UINT, WPARAM, c_void_p]
user32.PostThreadMessageW.restype = BOOL
user32.SetTimer.argtypes = [HWND, c_void_p, UINT, c_void_p]
user32.SetTimer.restype = c_void_p
user32.KillTimer.argtypes = [HWND, c_void_p]
user32.KillTimer.restype = BOOL

kernel32.CreateFileW.argtypes = [c_void_p, DWORD, DWORD, LPVOID, DWORD, DWORD, HANDLE]
kernel32.CreateFileW.restype = HANDLE
kernel32.GetModuleHandleW.argtypes = [c_void_p]
kernel32.GetModuleHandleW.restype = HANDLE
kernel32.CloseHandle.argtypes = [HANDLE]
kernel32.CloseHandle.restype = BOOL

hidDll.HidD_GetPreparsedData.argtypes = [HANDLE, POINTER(c_void_p)]
hidDll.HidD_GetPreparsedData.restype = BOOLEAN
hidDll.HidP_GetCaps.argtypes = [c_void_p, POINTER(HIDP_CAPS)]
hidDll.HidP_GetCaps.restype = c_long
hidDll.HidP_GetValueCaps.argtypes = [c_int, POINTER(HIDP_VALUE_CAPS), POINTER(USHORT), c_void_p]
hidDll.HidP_GetValueCaps.restype = c_long
hidDll.HidP_GetUsageValue.argtypes = [
	c_int,
	USHORT,
	USHORT,
	USHORT,
	POINTER(ULONG),
	c_void_p,
	c_void_p,
	ULONG,
]
hidDll.HidP_GetUsageValue.restype = c_long


class _DeviceParser:
	"""Caches a touchpad raw-input device's preparsed HID data and value
	capabilities, and decodes contacts out of its raw reports.
	"""

	def __init__(self, preparsedDataBuf, valueCaps, reportByteLength):
		# Keep the buffer itself alive for as long as this parser is used -
		# self._preparsedData is only a non-owning c_void_p view into it.
		self._preparsedDataBuf = preparsedDataBuf
		self._preparsedData = cast(preparsedDataBuf, c_void_p)
		self._valueCaps = valueCaps
		self.reportByteLength = reportByteLength

	def decodeContacts(self, reportBytes):
		"""Returns {contactId: (xProportion, yProportion)} for the currently
		down contacts in this report, where each proportion is in
		[0.0, 1.0] relative to the touchpad surface, computed from the
		device's own per-axis logical min/max.

		Contact liveness is determined by the device-level Contact Count
		usage (0x0D/0x54), not by a per-contact Tip Switch usage (0x0D/0x42):
		confirmed by instrumented testing that the hardware this add-on was
		developed against does not expose a Tip Switch usage in its raw
		HID report at all (its value caps list only Contact ID, X, Y, Width,
		Height, Azimuth and Pressure per contact link collection), even
		though Tip Switch is documented as mandatory for a spec-compliant
		Windows Precision Touchpad - contact link collections beyond the
		live count still report their last real X/Y rather than zeros once
		a finger lifts, so treating "present" as "live" produces stale
		phantom contacts. Link collections are populated in ascending
		numeric order by contact slot per the spec's example, so the first
		contactCount link collections (by LinkCollection number, i.e.
		reported slot order, not by comparing contact IDs) are the live
		ones.
		"""
		byLinkCollection = {}
		reportBuf = create_string_buffer(reportBytes, len(reportBytes))
		for vc in self._valueCaps:
			usage = vc.RangeOrNotRange.UsageMin
			value = ULONG(0)
			status = hidDll.HidP_GetUsageValue(
				HidP_Input,
				vc.UsagePage,
				vc.LinkCollection,
				usage,
				byref(value),
				self._preparsedData,
				reportBuf,
				len(reportBytes),
			)
			if status != HIDP_STATUS_SUCCESS:
				continue
			byLinkCollection.setdefault(vc.LinkCollection, {})[(vc.UsagePage, usage)] = (
				value.value,
				vc.LogicalMin,
				vc.LogicalMax,
			)

		deviceLevel = byLinkCollection.get(0, {})
		contactCountEntry = deviceLevel.get((USAGE_PAGE_DIGITIZER, USAGE_CONTACT_COUNT))
		contactCount = contactCountEntry[0] if contactCountEntry else 0

		contacts = {}
		for linkCollection in sorted(lc for lc in byLinkCollection if lc != 0)[:contactCount]:
			fields = byLinkCollection[linkCollection]
			cidEntry = fields.get((USAGE_PAGE_DIGITIZER, USAGE_CONTACT_ID))
			xEntry = fields.get((USAGE_PAGE_GENERIC_DESKTOP, USAGE_X))
			yEntry = fields.get((USAGE_PAGE_GENERIC_DESKTOP, USAGE_Y))
			if cidEntry is None or xEntry is None or yEntry is None:
				continue
			contactId = cidEntry[0]
			xValue, xMin, xMax = xEntry
			yValue, yMin, yMax = yEntry
			xProportion = (xValue - xMin) / (xMax - xMin) if xMax > xMin else 0.0
			yProportion = (yValue - yMin) / (yMax - yMin) if yMax > yMin else 0.0
			contacts[contactId] = (xProportion, yProportion)
		return contacts


def _getDeviceName(hDevice):
	nameSize = UINT(0)
	user32.GetRawInputDeviceInfoW(hDevice, RIDI_DEVICENAME, None, byref(nameSize))
	if nameSize.value == 0:
		return ""
	buf = create_unicode_buffer(nameSize.value + 1)
	gotSize = UINT(nameSize.value + 1)
	user32.GetRawInputDeviceInfoW(hDevice, RIDI_DEVICENAME, buf, byref(gotSize))
	return buf.value


def _getDeviceInfo(hDevice):
	sizeQuery = UINT(0)
	user32.GetRawInputDeviceInfoW(hDevice, RIDI_DEVICEINFO, None, byref(sizeQuery))
	if sizeQuery.value == 0:
		return None
	info = RID_DEVICE_INFO_HID()
	info.cbSize = sizeQuery.value
	gotSize = UINT(sizeQuery.value)
	result = user32.GetRawInputDeviceInfoW(hDevice, RIDI_DEVICEINFO, byref(info), byref(gotSize))
	if result < 0:
		return None
	return info


def findTouchpadDevices():
	"""Returns a list of raw input HANDLEs for HID devices whose top-level
	collection is Digitizer/TouchPad (UsagePage 0x0D, Usage 0x05). A machine
	may expose more than one such collection (observed: one per Precision
	Touchpad device instance); every match is registered for input.
	"""
	numDevices = UINT(0)
	user32.GetRawInputDeviceList(None, byref(numDevices), sizeof(RAWINPUTDEVICELIST))
	if numDevices.value == 0:
		return []
	deviceArray = (RAWINPUTDEVICELIST * numDevices.value)()
	user32.GetRawInputDeviceList(deviceArray, byref(numDevices), sizeof(RAWINPUTDEVICELIST))
	matches = []
	for item in deviceArray:
		if item.dwType != RIM_TYPEHID:
			continue
		info = _getDeviceInfo(item.hDevice)
		if info is None:
			continue
		if info.UsagePage == USAGE_PAGE_DIGITIZER and info.Usage == USAGE_TOUCHPAD:
			matches.append(item.hDevice)
	return matches


def _findOpenablePnpSiblingPath(vendorId, productId, usagePage, usage):
	"""Fallback for a touchpad whose synthetic raw-input device doesn't
	support RIDI_PREPARSEDDATA directly (not observed on the hardware this
	add-on was developed against, but not guaranteed on every device): find
	its PnP HID device node sibling (same VID/PID/UsagePage/Usage) that
	CreateFileW can actually open for a capability-only query.
	IMPORTANT: this sibling's report layout is not guaranteed to match the
	synthetic device's own reports (confirmed different on this add-on's own
	test hardware) - only used if RIDI_PREPARSEDDATA on the raw-input handle
	itself is unavailable.
	"""
	numDevices = UINT(0)
	user32.GetRawInputDeviceList(None, byref(numDevices), sizeof(RAWINPUTDEVICELIST))
	if numDevices.value == 0:
		return None
	deviceArray = (RAWINPUTDEVICELIST * numDevices.value)()
	user32.GetRawInputDeviceList(deviceArray, byref(numDevices), sizeof(RAWINPUTDEVICELIST))
	for item in deviceArray:
		if item.dwType != RIM_TYPEHID:
			continue
		info = _getDeviceInfo(item.hDevice)
		if info is None:
			continue
		if (info.VendorId, info.ProductId, info.UsagePage, info.Usage) != (
			vendorId,
			productId,
			usagePage,
			usage,
		):
			continue
		name = _getDeviceName(item.hDevice)
		if name and "Microsoft HID RID" not in name:
			return name
	return None


def _buildParser(hDevice):
	"""Builds a _DeviceParser for hDevice, preferring the device's own
	RIDI_PREPARSEDDATA (works directly on the raw-input handle, no file
	handle needed) and falling back to opening a PnP sibling node only if
	that's unavailable. Returns None if no usable preparsed data could be
	obtained.
	"""
	preparsedSize = UINT(0)
	user32.GetRawInputDeviceInfoW(hDevice, RIDI_PREPARSEDDATA, None, byref(preparsedSize))
	preparsedBuf = None
	if preparsedSize.value > 0:
		candidateBuf = create_string_buffer(preparsedSize.value)
		gotSize = UINT(preparsedSize.value)
		result = user32.GetRawInputDeviceInfoW(hDevice, RIDI_PREPARSEDDATA, candidateBuf, byref(gotSize))
		if result > 0:
			preparsedBuf = candidateBuf
		else:
			log.debugWarning(f"touchExplore: RIDI_PREPARSEDDATA failed for device {hDevice}, result={result}")
	else:
		log.debugWarning(f"touchExplore: RIDI_PREPARSEDDATA unavailable for device {hDevice}")

	if preparsedBuf is None:
		info = _getDeviceInfo(hDevice)
		if info is None:
			return None
		pnpPath = _findOpenablePnpSiblingPath(info.VendorId, info.ProductId, info.UsagePage, info.Usage)
		if not pnpPath:
			log.debugWarning(f"touchExplore: no openable PnP sibling found for device {hDevice}")
			return None
		hFile = kernel32.CreateFileW(
			pnpPath,
			GENERIC_ZERO_ACCESS,
			FILE_SHARE_READ | FILE_SHARE_WRITE,
			None,
			OPEN_EXISTING,
			0,
			None,
		)
		if hFile in (0, HANDLE(-1).value):
			log.debugWarning(f"touchExplore: CreateFileW failed for PnP sibling {pnpPath!r}")
			return None
		preparsedPtr = c_void_p()
		gotPreparsed = hidDll.HidD_GetPreparsedData(hFile, byref(preparsedPtr))
		kernel32.CloseHandle(hFile)
		if not gotPreparsed:
			log.debugWarning("touchExplore: HidD_GetPreparsedData failed on PnP sibling")
			return None
		preparsedBuf = preparsedPtr

	preparsedPtr = cast(preparsedBuf, c_void_p)
	caps = HIDP_CAPS()
	capsStatus = hidDll.HidP_GetCaps(preparsedPtr, byref(caps))
	if capsStatus != HIDP_STATUS_SUCCESS or caps.UsagePage != USAGE_PAGE_DIGITIZER or caps.Usage != USAGE_TOUCHPAD:
		log.debugWarning(
			f"touchExplore: unexpected HidP_GetCaps result for device {hDevice}: "
			f"status=0x{capsStatus & 0xFFFFFFFF:08X} UsagePage=0x{caps.UsagePage:04X} Usage=0x{caps.Usage:04X}",
		)
		return None

	valueCapsCount = USHORT(caps.NumberInputValueCaps)
	valueCapsArray = (HIDP_VALUE_CAPS * valueCapsCount.value)()
	vcStatus = hidDll.HidP_GetValueCaps(HidP_Input, valueCapsArray, byref(valueCapsCount), preparsedPtr)
	if vcStatus != HIDP_STATUS_SUCCESS:
		log.debugWarning(f"touchExplore: HidP_GetValueCaps failed for device {hDevice}")
		return None

	return _DeviceParser(preparsedBuf, list(valueCapsArray)[: valueCapsCount.value], caps.InputReportByteLength)


class TrackpadTouchScreen:
	"""Runs a background thread that reads raw touchpad HID contacts and
	feeds them into a touchTracker.TrackerManager + a screenExplorer.
	ScreenExplorer, exactly mirroring what touchHandler.TouchHandler does
	for real touchscreen hardware.

	While active, an instance of this class is installed as
	touchHandler.handler itself (see start()/stop()) rather than being kept
	as a disconnected parallel object. This matters because NVDA's own touch
	scripts - script_touch_newExplore, script_touch_explore,
	script_touch_changeMode, etc, in globalCommands.py - hardcode references
	to the module-level touchHandler.handler singleton (e.g.
	"touchHandler.handler.screenExplorer.moveTo(...)"), not to whatever
	object a gesture happened to come from. Since touchSupported() requires
	real touch hardware, that singleton is normally None on a trackpad-only
	machine and those scripts would fail outright; installing this object in
	its place is what makes stock NVDA touch-explore narration, tap-to-
	explore, and mode-cycling all work unmodified from trackpad input. This
	mirrors and cooperates with the class-level moveTo() patch already
	applied to screenExplorer.ScreenExplorer elsewhere in this add-on
	(see __init__.py's _patchedMoveTo), since that patch applies to any
	instance, including the one created here.

	x/y fed to the tracker manager are real screen pixel coordinates,
	computed by scaling each contact's proportional position on the
	touchpad surface (0.0-1.0 per axis, from the device's own logical
	min/max) onto the primary screen's pixel dimensions - the trackpad
	surface maps onto the whole screen the same way a real touchscreen's
	surface does, independent of and ignoring wherever the OS mouse cursor
	happens to be.
	"""

	def __init__(self, mode="object"):
		# Constructing gui.NonReEntrantTimer (a wx.Timer subclass) requires
		# the wx GUI/main thread - safe here because this object is always
		# constructed from an NVDA script (script_toggleTrackpadTouchScreen
		# in __init__.py), which NVDA always calls on the main thread.
		self._curTouchMode = mode
		self._thread = None
		self._threadId = None
		self._hwnd = None
		self._mouseLegacySuppressed = False
		self._wndProcRef = None
		self._parsersByDevice = {}
		self._lastContactPositions = {}
		self._lastContactIds = set()
		# Per-contact timestamp (time.time()) of its last REAL HID report -
		# distinct from the timer-poll re-feeds in _handlePollTimer(), which
		# intentionally do not update this. Used to infer a lift when the
		# hardware goes silent for LIFT_INFERENCE_TIMEOUT_S without ever
		# sending an explicit lift report - see _handlePollTimer() and
		# CLAUDE.md for why this is necessary on this hardware.
		self._lastRealReportTime = {}
		self.trackerManager = touchTracker.TrackerManager()
		self.screenExplorer = screenExplorer.ScreenExplorer()
		self.screenExplorer.updateReview = True
		self._previousHandler = "__not_installed__"
		self._initializedEvent = threading.Event()
		self._initError = None
		# Mirrors touchHandler.TouchHandler.pendingEmitsTimer: ensures a
		# pluralized-tap merge window (e.g. double-tap detection) that can
		# only resolve via timeout - no further touch activity to trigger
		# another core.requestPump() - still gets flushed.
		self.pendingEmitsTimer = gui.NonReEntrantTimer(core.requestPump)

	def setMode(self, mode):
		if mode not in touchHandler.availableTouchModes:
			raise ValueError(f"Unknown mode {mode}")
		self._curTouchMode = mode

	def notifyInteraction(self, obj):
		"""Same contract and implementation as
		touchHandler.TouchHandler.notifyInteraction - some callers (e.g. this
		add-on's own split-tap script, and NVDA's touch typing/activation
		scripts) call touchHandler.handler.notifyInteraction() directly, so
		this needs to exist once this object IS touchHandler.handler.
		"""
		windll.oleacc.AccNotifyTouchInteraction(
			gui.mainFrame.Handle,
			obj.windowHandle,
			obj.location.center.toPOINT(),
		)

	def start(self):
		"""Starts the capture thread and blocks until it has either finished
		initializing (window created, a touchpad digitizer device found, raw
		input registered) or failed to. Raises the failure (e.g. no Precision
		Touchpad found) synchronously rather than leaving the caller unaware
		that trackpad-as-touchscreen mode didn't actually start.
		"""
		if self._thread is not None:
			return
		self._previousHandler = touchHandler.handler
		touchHandler.handler = self
		self._thread = threading.Thread(
			target=self._run,
			name="touchExplore.TrackpadTouchScreen",
			daemon=True,
		)
		self._thread.start()
		self._initializedEvent.wait()
		if self._initError is not None:
			error = self._initError
			self.stop()
			raise error

	def stop(self):
		if self._thread is None:
			return
		if self._threadId:
			user32.PostThreadMessageW(self._threadId, WM_QUIT, 0, None)
		self._thread.join(timeout=2)
		self._thread = None
		self._threadId = None
		self.pendingEmitsTimer.Stop()
		if self._previousHandler != "__not_installed__":
			if touchHandler.handler is self:
				touchHandler.handler = self._previousHandler
			self._previousHandler = "__not_installed__"

	def terminate(self):
		"""Alias for stop(), matching touchHandler.TouchHandler's contract:
		touchHandler.terminate() (called during NVDA's own shutdown, from
		core._terminate) calls touchHandler.handler.terminate() on whatever
		object touchHandler.handler currently is - if this object is still
		installed there (trackpad-as-touchscreen mode left on when NVDA
		exits) that call must not fail with AttributeError, the same class
		of bug pump()'s prior absence caused (see CLAUDE.md).
		"""
		self.stop()

	def _run(self):
		self._threadId = threading.get_ident()
		hInstance = None
		classAtom = None
		try:
			hInstance = kernel32.GetModuleHandleW(None)
			self._wndProcRef = winUser.WNDPROC(self._wndProc)
			wndClass = winUser.WNDCLASSEXW(
				cbSize=sizeof(winUser.WNDCLASSEXW),
				lpfnWndProc=self._wndProcRef,
				hInstance=hInstance,
				lpszClassName="touchExploreTrackpadTouchWindowClass",
			)
			classAtom = user32.RegisterClassExW(byref(wndClass))
			if not classAtom:
				raise OSError("RegisterClassExW failed for trackpad touch window")
			self._hwnd = user32.CreateWindowExW(
				0,
				classAtom,
				None,
				0,
				0,
				0,
				0,
				0,
				HWND(HWND_MESSAGE),
				None,
				hInstance,
				None,
			)
			if not self._hwnd:
				raise OSError("CreateWindowExW failed for trackpad touch window")

			deviceHandles = findTouchpadDevices()
			if not deviceHandles:
				raise RuntimeError("No Precision Touchpad HID digitizer device found")
			ridArray = (RAWINPUTDEVICE * 2)()
			ridArray[0].usUsagePage = USAGE_PAGE_DIGITIZER
			ridArray[0].usUsage = USAGE_TOUCHPAD
			ridArray[0].dwFlags = RIDEV_INPUTSINK
			ridArray[0].hwndTarget = self._hwnd
			# The trackpad is, physically, also an ordinary mouse-class HID
			# device generating real WM_MOUSEMOVE/click messages in parallel
			# with the raw digitizer reports above - moving the real OS
			# cursor along with whatever finger is being tracked (confirmed
			# directly; see CLAUDE.md). RIDEV_NOLEGACY for the mouse usage
			# class (0x01/0x02) stops Windows from generating those legacy
			# messages/cursor movement at all while this is registered -
			# confirmed empirically (a plain RIDEV_NOLEGACY registration on
			# a message-only window had NO effect; RIDEV_NOLEGACY only
			# suppresses legacy messages while the registering window is
			# foreground UNLESS RIDEV_INPUTSINK is also set, matching this
			# add-on's own message-only, never-foreground window - both
			# flags are required together). This affects EVERY mouse device
			# on the system, not just the trackpad - Windows has no
			# documented way to scope RIDEV_NOLEGACY to one specific
			# physical device, only to a whole HID usage class - accepted
			# tradeoff (see CLAUDE.md and README.md).
			ridArray[1].usUsagePage = USAGE_PAGE_GENERIC_DESKTOP
			ridArray[1].usUsage = USAGE_MOUSE
			ridArray[1].dwFlags = RIDEV_NOLEGACY | RIDEV_INPUTSINK
			ridArray[1].hwndTarget = self._hwnd
			if not user32.RegisterRawInputDevices(ridArray, 2, sizeof(RAWINPUTDEVICE)):
				raise OSError("RegisterRawInputDevices failed for trackpad touch input")
			self._mouseLegacySuppressed = True
			if not user32.SetTimer(self._hwnd, POLL_TIMER_ID, CONTACT_POLL_INTERVAL_MS, None):
				log.debugWarning(f"touchExplore: SetTimer failed, err={GetLastError()}")
			log.debug(f"touchExplore: trackpad touch input active, {len(deviceHandles)} digitizer device(s) found")
			self._initializedEvent.set()

			msg = MSG()
			while True:
				result = user32.GetMessageW(byref(msg), None, 0, 0)
				if result <= 0:
					break
				user32.TranslateMessage(byref(msg))
				user32.DispatchMessageW(byref(msg))
		except Exception as e:
			log.error("touchExplore: trackpad touch input thread failed", exc_info=True)
			self._initError = e
		finally:
			self._initializedEvent.set()
			# Unconditionally attempt to remove BOTH registrations, regardless
			# of which succeeded above - RegisterRawInputDevices's atomicity
			# across multiple array entries isn't documented, and leaving the
			# mouse's RIDEV_NOLEGACY registered (freezing every mouse cursor
			# system-wide, confirmed by direct testing - see CLAUDE.md) is
			# the highest-risk state this code can leave behind, so its
			# removal must not depend on any other cleanup step succeeding
			# first. RIDEV_REMOVE on something never actually registered is
			# a harmless no-op/failure, not an error worth guarding against.
			removeArray = (RAWINPUTDEVICE * 2)()
			removeArray[0].usUsagePage = USAGE_PAGE_DIGITIZER
			removeArray[0].usUsage = USAGE_TOUCHPAD
			removeArray[0].dwFlags = RIDEV_REMOVE
			removeArray[0].hwndTarget = None
			removeArray[1].usUsagePage = USAGE_PAGE_GENERIC_DESKTOP
			removeArray[1].usUsage = USAGE_MOUSE
			removeArray[1].dwFlags = RIDEV_REMOVE
			removeArray[1].hwndTarget = None
			if not user32.RegisterRawInputDevices(removeArray, 2, sizeof(RAWINPUTDEVICE)):
				log.debugWarning(f"touchExplore: RIDEV_REMOVE failed, err={GetLastError()}")
			self._mouseLegacySuppressed = False
			if self._hwnd:
				user32.KillTimer(self._hwnd, POLL_TIMER_ID)
				user32.DestroyWindow(self._hwnd)
				self._hwnd = None
			if classAtom:
				# Must unregister the window class on the way out, or the next
				# start() on this same NVDA process fails with
				# RegisterClassExW returning 0 (ERROR_CLASS_ALREADY_EXISTS) -
				# confirmed directly: enabling trackpad-as-touchscreen mode a
				# second time in the same NVDA session failed outright until
				# this was added (see CLAUDE.md). The class atom (not a
				# string) is passed as the low word of what would normally be
				# the class name pointer (argtypes declares this parameter as
				# c_void_p, so passing the plain integer atom sets only its
				# low word, matching UnregisterClassW's documented
				# convention: "the atom must be in the low-order word ...
				# the high-order word must be zero").
				if not user32.UnregisterClassW(classAtom, hInstance):
					log.debugWarning(
						f"touchExplore: UnregisterClassW failed, err={GetLastError()}",
					)

	def _wndProc(self, hwnd, msg, wParam, lParam):
		if msg == WM_INPUT:
			self._handleRawInput(lParam)
			return 0
		if msg == WM_TIMER:
			self._handlePollTimer()
			return 0
		return user32.DefWindowProcW(hwnd, msg, wParam, lParam)

	def _handlePollTimer(self):
		"""Re-feeds every currently-down contact's LAST KNOWN position into
		trackerManager on a short fixed interval, independent of whether a
		new HID report has actually arrived. Needed because this hardware's
		HID driver stops sending reports entirely for a contact that isn't
		moving (confirmed directly - see the module docstring and
		CLAUDE.md), but touchTracker.SingleTouchTracker's tap-vs-hover
		classification is time-based and is only evaluated when
		TrackerManager.update() is called - so a real, quick tap or
		multi-finger tap could otherwise sit unclassified past its own
		250ms window and default to hover, purely because no report arrived
		to prompt re-evaluation in time.

		Also infers a lift for any contact whose last REAL report (not a
		previous poll re-feed - see _lastRealReportTime) is older than
		LIFT_INFERENCE_TIMEOUT_S. Necessary because this hardware, confirmed
		directly, sends no report at all for an ACTUAL LIFT following a fast
		flick, not just for a stationary contact - without this, a lifted
		contact would be kept alive by this same poll forever (this method
		would otherwise re-feed its last position with complete=False
		indefinitely, permanently preventing the complete=True call that
		touchTracker.SingleTouchTracker requires to ever classify a tap or
		flick at all). See CLAUDE.md for the full investigation.
		"""
		if not self._lastContactPositions:
			return
		now = time.time()
		staleIds = [
			contactId
			for contactId, lastReal in self._lastRealReportTime.items()
			if now - lastReal >= LIFT_INFERENCE_TIMEOUT_S
		]
		for contactId in staleIds:
			x, y = self._lastContactPositions.pop(contactId)
			self._lastRealReportTime.pop(contactId, None)
			self._lastContactIds.discard(contactId)
			log.debug(
				f"touchExplore: inferred lift for contact {contactId} "
				f"(no real report for >={LIFT_INFERENCE_TIMEOUT_S}s)",
			)
			self.trackerManager.update(contactId, x, y, True)
		for contactId, (x, y) in self._lastContactPositions.items():
			self.trackerManager.update(contactId, x, y, False)
		core.requestPump()

	def _handleRawInput(self, lParam):
		size = UINT(0)
		user32.GetRawInputData(lParam, RID_INPUT, None, byref(size), sizeof(RAWINPUTHEADER))
		if size.value == 0:
			log.debug("touchExplore: WM_INPUT with size=0")
			return
		buf = create_string_buffer(size.value)
		got = user32.GetRawInputData(lParam, RID_INPUT, buf, byref(size), sizeof(RAWINPUTHEADER))
		if got != size.value:
			log.debug(f"touchExplore: GetRawInputData size mismatch got={got} expected={size.value}")
			return
		header = cast(buf, POINTER(RAWINPUTHEADER)).contents
		if header.dwType != RIM_TYPEHID:
			log.debug(f"touchExplore: WM_INPUT non-HID dwType={header.dwType}")
			return

		parser = self._parsersByDevice.get(header.hDevice, "__unset__")
		if parser == "__unset__":
			parser = _buildParser(header.hDevice)
			self._parsersByDevice[header.hDevice] = parser  # cache failures (None) too - don't retry every report
		if parser is None:
			log.debug(f"touchExplore: no parser for device {header.hDevice}")
			return

		hidOffset = sizeof(RAWINPUTHEADER)
		dwSizeHid = int.from_bytes(buf.raw[hidOffset : hidOffset + 4], "little")
		rawStart = hidOffset + 8
		reportBytes = buf.raw[rawStart : rawStart + dwSizeHid]
		if len(reportBytes) < parser.reportByteLength:
			log.debug(f"touchExplore: report too short len={len(reportBytes)} expected={parser.reportByteLength}")
			return

		contacts = parser.decodeContacts(reportBytes)
		screenWidth = user32.GetSystemMetrics(SM_CXSCREEN)
		screenHeight = user32.GetSystemMetrics(SM_CYSCREEN)

		currentIds = set(contacts.keys())
		for contactId in self._lastContactIds - currentIds:
			lastPos = self._lastContactPositions.pop(contactId, None)
			self._lastRealReportTime.pop(contactId, None)
			if lastPos is not None:
				self.trackerManager.update(contactId, lastPos[0], lastPos[1], True)
		now = time.time()
		for contactId, (xProportion, yProportion) in contacts.items():
			x = max(0, min(screenWidth - 1, int(xProportion * screenWidth)))
			y = max(0, min(screenHeight - 1, int(yProportion * screenHeight)))
			self._lastContactPositions[contactId] = (x, y)
			self._lastRealReportTime[contactId] = now
			self.trackerManager.update(contactId, x, y, False)
		self._lastContactIds = currentIds

		# Only update trackerManager and request a pump here - this method
		# runs on the background HID capture thread, and actually dispatching
		# gestures (pump(), below) must happen on the main thread, exactly
		# like touchHandler.TouchHandler.inputTouchWndProc does for real
		# touch hardware. core.py's own core pump loop calls
		# touchHandler.handler.pump() unconditionally on every cycle once
		# touchHandler.handler is set - which is this object, while active -
		# so pump() must both exist (its absence previously caused an
		# AttributeError on every single core pump cycle, silently breaking
		# NVDA's speech queue processing until restart - see CLAUDE.md) and
		# must not be called redundantly from here.
		core.requestPump()

	def pump(self):
		"""Called by core.py's CorePump.Notify() on the main thread, exactly
		as it calls touchHandler.TouchHandler.pump() for real touch hardware
		- required to exist here since this object IS touchHandler.handler
		while trackpad-as-touchscreen mode is active (see start()). Its
		absence previously caused an AttributeError on every single core
		pump cycle once installed as touchHandler.handler, which - since
		core.py's CorePump.Notify() catches and logs but does not re-raise
		that exception - silently aborted every later step in that pump
		cycle (including queueHandler.pumpAll(), which drains NVDA's speech
		queue), breaking NVDA's own speech and forcing a restart to recover.
		See CLAUDE.md.
		"""
		for preheldTracker, tracker in self.trackerManager.emitTrackers():
			log.debug(
				f"touchExplore: emitted tracker action={tracker.action!r} "
				f"numFingers={tracker.numFingers} actionCount={tracker.actionCount} "
				f"preheld={preheldTracker.numFingers if preheldTracker else None}",
			)
			# TouchInputGesture's gesture identifier is built with "%s" %
			# mode (see _get_identifiers in touchHandler.py) - self._curTouchMode
			# may be a touchHandler.TouchMode enum member on current NVDA
			# versions (added after this add-on was first written against an
			# older source snapshot; see CLAUDE.md), and formatting an enum
			# with %s produces "TouchMode.OBJECT" instead of "object",
			# breaking every "ts(object):..."-style gesture binding, this
			# add-on's own included. Real TouchHandler._processGestures()
			# normalizes the same way immediately before constructing each
			# TouchInputGesture; mirrored here rather than normalizing once
			# in __init__/setMode, in case a caller mutates _curTouchMode
			# directly the way real TouchHandler's own browse-mode-tracking
			# code does.
			modeValue = getattr(self._curTouchMode, "value", self._curTouchMode)
			gesture = touchHandler.TouchInputGesture(preheldTracker, tracker, modeValue)
			try:
				inputCore.manager.executeGesture(gesture)
			except inputCore.NoInputGestureAction:
				pass
		interval = self.trackerManager.pendingEmitInterval
		if interval and interval > 0:
			# Ensure we are pumped again by the time more pending multiTouch trackers are ready.
			self.pendingEmitsTimer.Start(int(interval * 1000), True)
		else:
			# Stop the timer in case we were pumped due to something unrelated
			# but just happened to be at the appropriate time to clear any
			# remaining trackers.
			self.pendingEmitsTimer.Stop()
