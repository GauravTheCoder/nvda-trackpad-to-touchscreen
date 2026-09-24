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
#   - Tip Switch (0x0D, 0x42) and Confidence (0x0D, 0x47) are 1-bit BUTTON
#     usages, not values - they only show up via HidP_GetButtonCaps/
#     HidP_GetUsages, never via HidP_GetValueCaps. (An earlier version of
#     this add-on concluded from a value-caps-only dump that this hardware
#     had no Tip Switch at all; a button-caps probe later showed it does -
#     Tip, Confidence and In Range (0x32) on every contact link collection.)
#     Contact liveness is based on the device-level Contact Count usage
#     (0x0D, 0x54), refined by Tip/Confidence whenever the device exposes
#     them - see _DeviceParser.decode and TrackpadTouchScreen._applyFrame.
#   - Hybrid reporting mode (spec: a device with fewer contact slots per
#     report than contacts it tracks splits one frame across several
#     reports; only the first carries the real Contact Count, the rest
#     report 0, all share the same Scan Time) is reassembled by
#     _FrameAssembler. Not exercised by the development hardware (5 slots
#     per report, parallel mode), so kept strictly additive: a device that
#     always fits a frame in one report behaves exactly as before.
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

import itertools
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

import config
import core
import gui
import inputCore
import screenExplorer
import touchHandler
import touchTracker
import winUser
from logHandler import log

from . import monitors, touchSettings

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
# LIFT_INFERENCE_TIMEOUT_S is a floor, tuned on hardware reporting every few
# milliseconds. A slower device (some Bluetooth/low-power touchpads report far
# less often) could go 80ms between two perfectly ordinary reports for a
# finger that's still down, so the effective timeout also scales with each
# device's own measured report interval: this many typical intervals without
# a real report counts as a lift.
LIFT_INFERENCE_REPORT_INTERVALS = 8
# Gaps between consecutive real reports longer than this are "the hardware
# went quiet" (see above), not the device's report rate, and are excluded
# from the report-interval average.
MAX_REPORT_INTERVAL_SAMPLE_S = 0.25
RID_INPUT = 0x10000003
RIDEV_INPUTSINK = 0x00000100
RIDEV_REMOVE = 0x00000001
RIDEV_NOLEGACY = 0x00000030
RIDEV_DEVNOTIFY = 0x00002000
WM_INPUT_DEVICE_CHANGE = 0x00FE
GIDC_ARRIVAL = 1
GIDC_REMOVAL = 2
# Used to convert millimetre thresholds only when a touchpad doesn't declare
# its physical size (the spec requires it, so this should be rare): a typical
# laptop Precision Touchpad.
FALLBACK_PAD_SIZE_MM = (105.0, 70.0)
RIM_TYPEHID = 2
RIDI_DEVICENAME = 0x20000007
RIDI_DEVICEINFO = 0x2000000B
RIDI_PREPARSEDDATA = 0x20000005
HWND_MESSAGE = -3

GENERIC_ZERO_ACCESS = 0
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3

HidP_Input = 0
HIDP_STATUS_SUCCESS = 0x00110000
# Upper bound on how many digitizer-page buttons one contact link collection
# can have set at once (Tip, Confidence, In Range, ... - 3 on the development
# hardware); generous so HidP_GetUsages never reports BUFFER_TOO_SMALL.
_MAX_USAGES_PER_LINK_COLLECTION = 32

USAGE_PAGE_DIGITIZER = 0x0D
USAGE_TOUCHPAD = 0x05
USAGE_PAGE_GENERIC_DESKTOP = 0x01
USAGE_MOUSE = 0x02
USAGE_X = 0x30
USAGE_Y = 0x31
USAGE_TIP_SWITCH = 0x42
USAGE_CONFIDENCE = 0x47
USAGE_CONTACT_ID = 0x51
USAGE_CONTACT_COUNT = 0x54
USAGE_SCAN_TIME = 0x56


class NoTouchpadFoundError(RuntimeError):
	"""No Windows Precision Touchpad digitizer collection is present - e.g. a
	touchpad using an older vendor (Synaptics/Elan/ALPS) mouse-emulation
	driver, which never exposes Usage Page 0x0D / Usage 0x05 at all.
	"""


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


class HIDP_BUTTON_CAPS(Structure):
	# 72 bytes, same union trick as HIDP_VALUE_CAPS: Range.UsageMin/UsageMax
	# alias NotRange.Usage/Reserved1, so a non-range cap reads as
	# UsageMin == its usage.
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
		("ReportCount", USHORT),
		("Reserved2", USHORT),
		("Reserved", ULONG * 9),
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
hidDll.HidP_GetButtonCaps.argtypes = [c_int, POINTER(HIDP_BUTTON_CAPS), POINTER(USHORT), c_void_p]
hidDll.HidP_GetButtonCaps.restype = c_long
hidDll.HidP_GetUsages.argtypes = [
	c_int,
	USHORT,
	USHORT,
	POINTER(USHORT),
	POINTER(ULONG),
	c_void_p,
	c_void_p,
	ULONG,
]
hidDll.HidP_GetUsages.restype = c_long
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


def _physicalMM(valueCaps):
	"""Physical extent in millimetres of one value cap, from its HID
	Physical Min/Max, Unit and Unit Exponent - which the Precision Touchpad
	spec requires for X and Y. Unit's low nibble is the unit system (1 = SI
	linear: length in centimetres; 3 = English linear: length in inches) and
	its next nibble the length dimension's power (must be 1); Unit Exponent
	is a 4-bit two's-complement power of ten. Development hardware: Physical
	0..11999, Unit 0x11 (SI linear, cm), exponent 0xD (-3) -> 120mm wide.
	Returns None for anything else (unknown units, or no physical range).
	"""
	system = valueCaps.Units & 0xF
	lengthPower = (valueCaps.Units >> 4) & 0xF
	if lengthPower != 1 or system not in (1, 3):
		return None
	span = valueCaps.PhysicalMax - valueCaps.PhysicalMin
	if span <= 0:
		return None
	exponent = valueCaps.UnitsExp & 0xF
	if exponent >= 8:
		exponent -= 16
	mm = span * (10**exponent) * (10.0 if system == 1 else 25.4)
	# Plausibility: a touch surface between 1cm and 1m.
	return mm if 10 <= mm <= 1000 else None


_CONTACT_VALUE_USAGES = (
	(USAGE_PAGE_DIGITIZER, USAGE_CONTACT_ID),
	(USAGE_PAGE_GENERIC_DESKTOP, USAGE_X),
	(USAGE_PAGE_GENERIC_DESKTOP, USAGE_Y),
)


class _DeviceParser:
	"""Caches a touchpad raw-input device's preparsed HID data and the
	capabilities this add-on reads, and decodes contact slots out of its raw
	reports.
	"""

	def __init__(self, preparsedDataBuf, valueCaps, buttonCaps, reportByteLength):
		# Keep the buffer itself alive for as long as this parser is used -
		# self._preparsedData is only a non-owning c_void_p view into it.
		self._preparsedDataBuf = preparsedDataBuf
		self._preparsedData = cast(preparsedDataBuf, c_void_p)
		self.reportByteLength = reportByteLength
		# (linkCollection, (usagePage, usage)) -> (logicalMin, logicalMax) for
		# only the per-contact usages actually needed - decoding every value
		# cap (Width, Height, Azimuth, Pressure, ...) on every report was pure
		# overhead.
		self._ranges = {}
		deviceLevelUsages = set()
		# Physical extent per axis in millimetres (first slot that declares
		# one), for converting millimetre thresholds to pixels - see
		# touchSettings.py and _physicalMM.
		self._physicalMM = {}
		for vc in valueCaps:
			key = (vc.UsagePage, vc.RangeOrNotRange.UsageMin)
			if vc.LinkCollection == 0:
				deviceLevelUsages.add(key)
			elif key in _CONTACT_VALUE_USAGES:
				self._ranges[(vc.LinkCollection, key)] = (vc.LogicalMin, vc.LogicalMax)
				if key[0] == USAGE_PAGE_GENERIC_DESKTOP and key not in self._physicalMM:
					sizeMM = _physicalMM(vc)
					if sizeMM:
						self._physicalMM[key] = sizeMM
		self.hasScanTime = (USAGE_PAGE_DIGITIZER, USAGE_SCAN_TIME) in deviceLevelUsages
		# Contact slots: link collections carrying Contact ID + X + Y, in
		# ascending LinkCollection order (= reported slot order, per the
		# spec's example).
		self.slotLinkCollections = sorted(
			lc
			for lc in {lc for (lc, _key) in self._ranges}
			if all((lc, key) in self._ranges for key in _CONTACT_VALUE_USAGES)
		)
		# Tip/Confidence are 1-bit buttons (see module docstring). Only
		# trusted for a slot whose link collection actually declares them -
		# otherwise "not among the pressed buttons" would wrongly read as
		# tip-up / not-confident on a device that simply lacks the usage.
		self._tipLinkCollections = set()
		self._confidenceLinkCollections = set()
		for bc in buttonCaps:
			if bc.UsagePage != USAGE_PAGE_DIGITIZER:
				continue
			lo = bc.RangeOrNotRange.UsageMin
			hi = bc.RangeOrNotRange.UsageMax if bc.IsRange else lo
			if lo <= USAGE_TIP_SWITCH <= hi:
				self._tipLinkCollections.add(bc.LinkCollection)
			if lo <= USAGE_CONFIDENCE <= hi:
				self._confidenceLinkCollections.add(bc.LinkCollection)

	@property
	def sizeMM(self):
		"""(widthMM, heightMM) of the touch surface, or None if the device
		doesn't declare usable physical units for both axes.
		"""
		width = self._physicalMM.get((USAGE_PAGE_GENERIC_DESKTOP, USAGE_X))
		height = self._physicalMM.get((USAGE_PAGE_GENERIC_DESKTOP, USAGE_Y))
		return (width, height) if width and height else None

	def describe(self):
		return (
			f"slots={self.slotLinkCollections} tip={sorted(self._tipLinkCollections)} "
			f"confidence={sorted(self._confidenceLinkCollections)} scanTime={self.hasScanTime} "
			f"sizeMM={tuple(round(v, 1) for v in self.sizeMM) if self.sizeMM else None} "
			f"reportLen={self.reportByteLength}"
		)

	def _getValue(self, reportBuf, reportLen, usagePage, linkCollection, usage):
		value = ULONG(0)
		status = hidDll.HidP_GetUsageValue(
			HidP_Input,
			usagePage,
			linkCollection,
			usage,
			byref(value),
			self._preparsedData,
			reportBuf,
			reportLen,
		)
		return value.value if status == HIDP_STATUS_SUCCESS else None

	def _getPressedDigitizerButtons(self, reportBuf, reportLen, linkCollection):
		usageList = (USHORT * _MAX_USAGES_PER_LINK_COLLECTION)()
		usageLength = ULONG(_MAX_USAGES_PER_LINK_COLLECTION)
		status = hidDll.HidP_GetUsages(
			HidP_Input,
			USAGE_PAGE_DIGITIZER,
			linkCollection,
			usageList,
			byref(usageLength),
			self._preparsedData,
			reportBuf,
			reportLen,
		)
		if status != HIDP_STATUS_SUCCESS:
			return None
		return set(usageList[: usageLength.value])

	def decode(self, reportBytes):
		"""Decodes one raw report. Returns None if it isn't a contact report
		this parser understands at all (e.g. a different Report ID arriving
		through the same collection: HidP_GetUsageValue fails with
		HIDP_STATUS_INCOMPATIBLE_REPORT_ID) - such a report must be ignored,
		not read as "zero contacts", which would lift every finger.

		Otherwise returns (contactCount, scanTime, slots): contactCount from
		the device-level Contact Count usage (0x0D/0x54); scanTime from Scan
		Time (0x0D/0x56), None if the device lacks it; and slots, one entry
		per contact link collection in slot order - either None (that slot's
		values couldn't be read) or (contactId, xProportion, yProportion,
		tip, confident), proportions in [0.0, 1.0] from the device's own
		logical min/max, tip/confident True/False, or None where the device
		doesn't declare that usage for the slot.

		Which slots are live is decided by the caller (_FrameAssembler, then
		TrackpadTouchScreen._applyFrame), not here: slots beyond the live
		count keep reporting their last real X/Y rather than zeros once a
		finger lifts (confirmed on the development hardware), and in hybrid
		reporting mode the live count spans more than one report.
		"""
		reportLen = len(reportBytes)
		reportBuf = create_string_buffer(reportBytes, reportLen)
		contactCount = self._getValue(reportBuf, reportLen, USAGE_PAGE_DIGITIZER, 0, USAGE_CONTACT_COUNT)
		if contactCount is None:
			return None
		scanTime = None
		if self.hasScanTime:
			scanTime = self._getValue(reportBuf, reportLen, USAGE_PAGE_DIGITIZER, 0, USAGE_SCAN_TIME)
		slots = []
		for linkCollection in self.slotLinkCollections:
			contactId, xValue, yValue = (
				self._getValue(reportBuf, reportLen, page, linkCollection, usage)
				for (page, usage) in _CONTACT_VALUE_USAGES
			)
			if contactId is None or xValue is None or yValue is None:
				slots.append(None)
				continue
			xMin, xMax = self._ranges[(linkCollection, (USAGE_PAGE_GENERIC_DESKTOP, USAGE_X))]
			yMin, yMax = self._ranges[(linkCollection, (USAGE_PAGE_GENERIC_DESKTOP, USAGE_Y))]
			xProportion = (xValue - xMin) / (xMax - xMin) if xMax > xMin else 0.0
			yProportion = (yValue - yMin) / (yMax - yMin) if yMax > yMin else 0.0
			tip = confident = None
			hasTip = linkCollection in self._tipLinkCollections
			hasConfidence = linkCollection in self._confidenceLinkCollections
			if hasTip or hasConfidence:
				pressed = self._getPressedDigitizerButtons(reportBuf, reportLen, linkCollection)
				if pressed is not None:
					if hasTip:
						tip = USAGE_TIP_SWITCH in pressed
					if hasConfidence:
						confident = USAGE_CONFIDENCE in pressed
			slots.append(
				(
					contactId,
					min(1.0, max(0.0, xProportion)),
					min(1.0, max(0.0, yProportion)),
					tip,
					confident,
				),
			)
		return contactCount, scanTime, slots


class _FrameAssembler:
	"""Turns one device's stream of decoded reports into complete frames (the
	slots that are live in one scan), handling hybrid reporting mode: a
	device whose reports have fewer contact slots than contacts it's
	tracking sends one frame as several reports - the first carrying the
	real Contact Count, each following one Contact Count 0 and the same Scan
	Time - and only their slots concatenated are the real frame.

	A device that always fits a frame in one report (parallel mode, e.g. the
	development hardware: 5 slots, at most 5 contacts) takes the
	contactCount > 0 branch every time and gets back exactly the first
	contactCount slots, identical to how liveness was decided before this
	class existed. A Contact Count 0 report that isn't continuing an
	incomplete frame is a genuine empty frame (every finger lifted), also as
	before.
	"""

	def __init__(self):
		self._pending = None  # (expectedCount, scanTime, collectedSlots)

	def feed(self, contactCount, scanTime, slots):
		"""Returns the complete frame's slots (possibly empty), or None if
		this report only started/continued a frame that isn't complete yet.
		"""
		if contactCount > 0:
			if contactCount <= len(slots):
				self._pending = None
				return slots[:contactCount]
			self._pending = (contactCount, scanTime, list(slots))
			return None
		if self._pending is not None:
			expected, pendingScanTime, collected = self._pending
			self._pending = None
			if scanTime is None or pendingScanTime is None or scanTime == pendingScanTime:
				collected = collected + list(slots[: expected - len(collected)])
				if len(collected) >= expected:
					return collected
				self._pending = (expected, pendingScanTime, collected)
				return None
			# A new scan started before the previous frame completed - the rest
			# of that frame was lost; this report is a genuine empty frame.
		return []


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

	buttonCaps = []
	if caps.NumberInputButtonCaps:
		buttonCapsCount = USHORT(caps.NumberInputButtonCaps)
		buttonCapsArray = (HIDP_BUTTON_CAPS * buttonCapsCount.value)()
		bcStatus = hidDll.HidP_GetButtonCaps(HidP_Input, buttonCapsArray, byref(buttonCapsCount), preparsedPtr)
		if bcStatus == HIDP_STATUS_SUCCESS:
			buttonCaps = list(buttonCapsArray)[: buttonCapsCount.value]
		else:
			# Not fatal: without button caps, Tip/Confidence are simply treated
			# as absent and liveness falls back to Contact Count alone.
			log.debugWarning(f"touchExplore: HidP_GetButtonCaps failed for device {hDevice}")

	parser = _DeviceParser(
		preparsedBuf,
		list(valueCapsArray)[: valueCapsCount.value],
		buttonCaps,
		caps.InputReportByteLength,
	)
	if not parser.slotLinkCollections:
		log.debugWarning(f"touchExplore: device {hDevice} has no contact slots with Contact ID/X/Y")
		return None
	log.debug(f"touchExplore: parser for device {hDevice}: {parser.describe()}")
	return parser


def _chooseMapRect():
	"""The screen rectangle the touchpad surface should map onto for a new
	gesture: the monitor holding the foreground window, so a user whose
	active app is on a second monitor can actually reach it (previously the
	pad always mapped onto the primary monitor only).

	Falls back to the primary monitor when NVDA's own edge gestures
	(config.conf["touch"]["edgeGestures"]) are on: touchHandler._getEdge()
	tests coordinates against the PRIMARY screen's size only, so on a monitor
	to the right of the primary one every x would read as "right edge" and
	every gesture would get a spurious edge prefix, matching no binding.
	"""
	try:
		if config.conf["touch"]["edgeGestures"]:
			return monitors.primaryRect()
	except KeyError:
		pass  # NVDA version without edge gestures - nothing to protect.
	return monitors.foregroundRect()


# NVDA versions with sequential-flick gestures ("flickrightthenleft" etc)
# build them in TouchHandler._processGestures(), not inside TrackerManager -
# so a pump() that only drains emitTrackers() itself (as this module's
# originally did, copying the older TouchHandler.pump()) silently never
# produces them. When the running NVDA has that machinery, pump() calls it
# directly with this object as self; it only touches self.trackerManager,
# self._curTouchMode, self._executeGesture and self._tryBuildSequentialGesture,
# all of which TrackpadTouchScreen provides (the last borrowed from
# TouchHandler itself).
_canUseNvdaProcessGestures = hasattr(touchHandler.TouchHandler, "_processGestures") and hasattr(
	touchHandler.TouchHandler,
	"_tryBuildSequentialGesture",
)


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
	min/max) onto one monitor's pixel rectangle (see _chooseMapRect) - the
	trackpad surface maps onto that whole monitor the same way a real
	touchscreen's surface does, independent of and ignoring wherever the OS
	mouse cursor happens to be.
	"""

	if _canUseNvdaProcessGestures:
		_tryBuildSequentialGesture = touchHandler.TouchHandler._tryBuildSequentialGesture

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
		self._frameAssemblers = {}
		# Contacts are keyed by (hDevice, contactId) throughout, not the bare
		# HID contact ID: two touchpads (e.g. built-in + external) each number
		# their contacts from their own small range, and would otherwise
		# collide inside the one TrackerManager both feed. Each live key gets
		# a fresh integer tracker ID (TrackerManager's own ID type).
		self._trackerIds = {}
		self._nextTrackerId = itertools.count(1)
		# (hDevice, contactId) keys the device has flagged as not confident
		# (palm/unintentional per the spec). Ignored until that contact ID
		# stops being reported at all, since the spec says confidence, once
		# cleared, stays cleared for the rest of that contact's lifetime.
		self._rejectedKeys = set()
		# Last reported screen position of each live (hDevice, contactId).
		self._lastContactPositions = {}
		# Smoothed interval between real reports, per device - see
		# LIFT_INFERENCE_REPORT_INTERVALS.
		self._reportIntervals = {}
		self._lastDeviceReportTime = {}
		# Screen rectangle (left, top, width, height) the touchpad surface is
		# currently mapped onto - chosen when the first finger of a gesture
		# lands, then held fixed until every finger has lifted, so a gesture
		# can't jump monitors midway if focus changes during it.
		self._mapRect = None
		# Used when a report arrives with RAWINPUTHEADER.hDevice == 0, which
		# Microsoft documents as possible for precision touchpad input: only
		# attributable if exactly one touchpad is present.
		self._soleDevice = None
		self._useNvdaProcessGestures = _canUseNvdaProcessGestures
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
				raise NoTouchpadFoundError("No Precision Touchpad HID digitizer device found")
			self._soleDevice = deviceHandles[0] if len(deviceHandles) == 1 else None
			ridArray = (RAWINPUTDEVICE * 2)()
			ridArray[0].usUsagePage = USAGE_PAGE_DIGITIZER
			ridArray[0].usUsage = USAGE_TOUCHPAD
			# RIDEV_DEVNOTIFY: WM_INPUT_DEVICE_CHANGE on touchpad arrival/removal
			# (e.g. an external touchpad plugged in or unpaired while this
			# mode is on) - see _handleDeviceChange.
			ridArray[0].dwFlags = RIDEV_INPUTSINK | RIDEV_DEVNOTIFY
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
		if msg == WM_INPUT_DEVICE_CHANGE:
			self._handleDeviceChange(wParam, lParam)
			return 0
		return user32.DefWindowProcW(hwnd, msg, wParam, lParam)

	def _handleDeviceChange(self, change, hDevice):
		"""A touchpad was attached or removed while this mode is on. Raw input
		handles aren't stable across device re-enumeration (see CLAUDE.md), so
		drop anything cached for the handle - including a cached None, in
		case a handle value gets reused for a different device - and lift any
		contacts a removed device still had down, rather than leaving them
		stuck until lift inference catches them.
		"""
		self._parsersByDevice.pop(hDevice, None)
		self._frameAssemblers.pop(hDevice, None)
		if change == GIDC_REMOVAL:
			for key in [key for key in self._lastContactPositions if key[0] == hDevice]:
				self._liftContact(key)
			self._rejectedKeys = {key for key in self._rejectedKeys if key[0] != hDevice}
			self._reportIntervals.pop(hDevice, None)
			self._lastDeviceReportTime.pop(hDevice, None)
			core.requestPump()
		devices = findTouchpadDevices()
		self._soleDevice = devices[0] if len(devices) == 1 else None
		log.debug(f"touchExplore: touchpad device change {change} for {hDevice}, {len(devices)} now present")

	def _trackerIdFor(self, key):
		trackerId = self._trackerIds.get(key)
		if trackerId is None:
			trackerId = self._trackerIds[key] = next(self._nextTrackerId)
		return trackerId

	def _liftContact(self, key):
		"""Tells trackerManager the contact for key has lifted, at its last
		known position, and forgets it.
		"""
		x, y = self._lastContactPositions.pop(key)
		self._lastRealReportTime.pop(key, None)
		self.trackerManager.update(self._trackerIds.pop(key), x, y, True)

	def _liftInferenceTimeout(self, hDevice):
		return max(
			LIFT_INFERENCE_TIMEOUT_S,
			LIFT_INFERENCE_REPORT_INTERVALS * self._reportIntervals.get(hDevice, 0.0),
		)

	def _handlePollTimer(self, now=None):
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
		previous poll re-feed - see _lastRealReportTime) is older than its
		device's lift-inference timeout (_liftInferenceTimeout). Necessary
		because this hardware, confirmed directly, sends no report at all for
		an ACTUAL LIFT following a fast flick, not just for a stationary
		contact - without this, a lifted contact would be kept alive by this
		same poll forever (this method would otherwise re-feed its last
		position with complete=False indefinitely, permanently preventing the
		complete=True call that touchTracker.SingleTouchTracker requires to
		ever classify a tap or flick at all). See CLAUDE.md for the full
		investigation.
		"""
		if not self._lastContactPositions:
			return
		if now is None:
			now = time.time()
		staleKeys = [
			key
			for key, lastReal in self._lastRealReportTime.items()
			if now - lastReal >= self._liftInferenceTimeout(key[0])
		]
		for key in staleKeys:
			log.debug(
				f"touchExplore: inferred lift for contact {key} "
				f"(no real report for >={self._liftInferenceTimeout(key[0]):.3f}s)",
			)
			self._liftContact(key)
		for key, (x, y) in self._lastContactPositions.items():
			self.trackerManager.update(self._trackerIds[key], x, y, False)
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

		hDevice = header.hDevice
		if not hDevice:
			# Documented as possible for precision touchpad input. Only
			# attributable when exactly one touchpad is present.
			if self._soleDevice is None:
				log.debug("touchExplore: WM_INPUT with hDevice=0 and not exactly one touchpad; ignored")
				return
			hDevice = self._soleDevice

		parser = self._parsersByDevice.get(hDevice, "__unset__")
		if parser == "__unset__":
			parser = _buildParser(hDevice)
			self._parsersByDevice[hDevice] = parser  # cache failures (None) too - don't retry every report
		if parser is None:
			log.debug(f"touchExplore: no parser for device {hDevice}")
			return

		hidOffset = sizeof(RAWINPUTHEADER)
		dwSizeHid = int.from_bytes(buf.raw[hidOffset : hidOffset + 4], "little")
		dwCount = int.from_bytes(buf.raw[hidOffset + 4 : hidOffset + 8], "little")
		rawStart = hidOffset + 8
		# RAWHID can batch several same-sized reports into one WM_INPUT
		# (dwCount > 1) - each is a separate report and, in hybrid mode,
		# possibly a separate piece of the same frame.
		for index in range(max(1, dwCount)):
			reportBytes = buf.raw[rawStart + index * dwSizeHid : rawStart + (index + 1) * dwSizeHid]
			if len(reportBytes) < parser.reportByteLength:
				log.debug(f"touchExplore: report too short len={len(reportBytes)} expected={parser.reportByteLength}")
				return
			decoded = parser.decode(reportBytes)
			if decoded is None:
				log.debug(f"touchExplore: ignoring non-contact report from device {hDevice}")
				continue
			assembler = self._frameAssemblers.get(hDevice)
			if assembler is None:
				assembler = self._frameAssemblers[hDevice] = _FrameAssembler()
			frame = assembler.feed(*decoded)
			if frame is not None:
				self._applyFrame(hDevice, frame, time.time())

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

	def _applyFrame(self, hDevice, slots, now):
		"""Applies one complete frame (see _FrameAssembler) from hDevice:
		lifts contacts that are no longer live, updates/creates the rest.

		A slot is live unless its Tip Switch is clear (the spec's explicit
		lift report: the contact is sent once more with tip clear at its last
		position) or its Confidence is clear (palm/unintentional contact).
		Either usage only counts when the device declares it (tip/confident
		None otherwise), so a device without them - or one where reading them
		failed - is judged on Contact Count alone, as before.
		"""
		live = {}
		reportedKeys = set()
		for slot in slots:
			if slot is None:
				continue
			contactId, xProportion, yProportion, tip, confident = slot
			key = (hDevice, contactId)
			reportedKeys.add(key)
			if confident is False and key not in self._rejectedKeys:
				self._rejectedKeys.add(key)
				log.debug(f"touchExplore: contact {key} flagged not confident (palm?); ignoring it")
			if key in self._rejectedKeys or tip is False:
				continue
			live[key] = (xProportion, yProportion)
		# A rejected contact ID that's no longer reported at all has gone;
		# the device may reuse the ID for a new, genuine contact.
		self._rejectedKeys = {key for key in self._rejectedKeys if key[0] != hDevice or key in reportedKeys}

		for key in [key for key in self._lastContactPositions if key[0] == hDevice and key not in live]:
			self._liftContact(key)

		lastReport = self._lastDeviceReportTime.get(hDevice)
		if live and lastReport is not None and 0 < now - lastReport <= MAX_REPORT_INTERVAL_SAMPLE_S:
			previous = self._reportIntervals.get(hDevice)
			interval = now - lastReport
			self._reportIntervals[hDevice] = interval if previous is None else 0.8 * previous + 0.2 * interval
		self._lastDeviceReportTime[hDevice] = now

		if live and (self._mapRect is None or not self._lastContactPositions):
			self._mapRect = _chooseMapRect()
			self._applyThresholds(hDevice)
		for key, (xProportion, yProportion) in live.items():
			left, top, width, height = self._mapRect
			x = left + max(0, min(width - 1, int(xProportion * width)))
			y = top + max(0, min(height - 1, int(yProportion * height)))
			self._lastContactPositions[key] = (x, y)
			self._lastRealReportTime[key] = now
			self.trackerManager.update(self._trackerIdFor(key), x, y, False)

	def _applyThresholds(self, hDevice):
		"""Converts the trackpad's millimetre thresholds (touchSettings) to
		pixels for this gesture: how many screen pixels one millimetre of pad
		covers depends on both the pad's physical size and the monitor it's
		currently mapped onto, so it's recomputed whenever the mapping is
		chosen. The two axes can scale differently (pad and monitor aspect
		ratios needn't match) while touchTracker has one threshold for both,
		so their average is used.
		"""
		parser = self._parsersByDevice.get(hDevice)
		sizeMM = getattr(parser, "sizeMM", None)
		if not sizeMM:
			sizeMM = FALLBACK_PAD_SIZE_MM
			log.debug(f"touchExplore: device {hDevice} has no physical size; assuming {sizeMM}mm")
		_left, _top, width, height = self._mapRect
		touchSettings.apply(touchSettings.TRACKPAD, (width / sizeMM[0] + height / sizeMM[1]) / 2)

	def _executeGesture(self, gesture):
		"""Same contract as touchHandler.TouchHandler._executeGesture, which
		NVDA's own _processGestures() calls on self (see pump()).
		"""
		try:
			inputCore.manager.executeGesture(gesture)
		except inputCore.NoInputGestureAction:
			pass

	def _processGesturesLegacy(self):
		"""Emits pending trackers one-to-one as gestures, for NVDA versions
		without TouchHandler._processGestures() (so without sequential-flick
		gestures either); mirrors the older TouchHandler.pump() loop.
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
			self._executeGesture(touchHandler.TouchInputGesture(preheldTracker, tracker, modeValue))

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

		Prefers NVDA's own TouchHandler._processGestures() when the running
		version has it (see _canUseNvdaProcessGestures), so trackpad input
		gets exactly the same gesture processing - sequential flicks
		included - as a real touchscreen. If that ever fails against a future
		NVDA whose method needs something this class doesn't provide, fall
		back to the legacy loop for the rest of the session rather than
		breaking every pump (the failure mode described above).
		"""
		if self._useNvdaProcessGestures:
			try:
				touchHandler.TouchHandler._processGestures(self)
			except Exception:
				log.exception("touchExplore: NVDA's _processGestures failed; using legacy gesture loop")
				self._useNvdaProcessGestures = False
		else:
			self._processGesturesLegacy()
		interval = self.trackerManager.pendingEmitInterval
		if interval and interval > 0:
			# Ensure we are pumped again by the time more pending multiTouch trackers are ready.
			self.pendingEmitsTimer.Start(int(interval * 1000), True)
		else:
			# Stop the timer in case we were pumped due to something unrelated
			# but just happened to be at the appropriate time to clear any
			# remaining trackers.
			self.pendingEmitsTimer.Stop()
