# Touch Explore Sounds - virtual-desktop awareness for flick-based object
# navigation.
#
# Bug: flick navigation (script_touchExploreFlick*/2finger_flick* in
# __init__.py) walks the navigator object tree (simpleNext/simplePrevious/
# simpleParent/simpleFirstChild) starting from whatever the current
# navigator object already is. Pressing Ctrl+Win+D to create/switch virtual
# desktops does not destroy or move any window - it only hides it - so an
# app's window (e.g. WhatsApp) that was the navigator object before a
# desktop switch stays fully alive in NVDA's accessibility tree. A
# subsequent flick then keeps walking through that hidden window's object
# tree, and _navigateAndAnnounce's call to _touchSelect() can move REAL OS
# focus/selection into it - not just narration drifting, but an actual
# focus-steal into an app on a different, invisible virtual desktop. This
# is stock NVDA behavior too (identical with NVDA+numpad6 etc, no touch
# involved) since plain UIA/MSAA tree walking has no concept of virtual
# desktops at all - not a regression this add-on introduced, but this
# add-on's flick scripts are the ones calling _touchSelect(), so they're
# where the guard belongs.
#
# Fix: before _touchSelect()-ing a flicked-to object, check whether its
# window is on the CURRENTLY VISIBLE virtual desktop via the public,
# documented IVirtualDesktopManager COM interface. If it isn't (or the
# check is inconclusive), fall back to the stock, narration-only behavior
# (speakObject) instead of moving real focus/selection into a hidden
# desktop's window.
#
# IVirtualDesktopManager (not to be confused with the various undocumented
# IVirtualDesktop/IVirtualDesktopManagerInternal/IApplicationView shell-
# private interfaces that most community virtual-desktop tools use instead)
# is genuinely public and documented by Microsoft:
# https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nn-shobjidl_core-ivirtualdesktopmanager
# GUIDs below confirmed against three independent, mutually-consistent
# sources (pyvda's com_defns.py, MScholtes/VirtualDesktop.cs, and a
# community AutoHotkey sample) since Microsoft Learn's own pages don't
# print the raw GUID literals. Method signatures/vtable order confirmed
# directly from the three Learn method pages. Available since Windows 10
# (Learn's own requirements table) - no version-gating like the touchpad
# APIs elsewhere in this add-on.
#
# IsWindowOnCurrentVirtualDesktop's parameter is literally named
# topLevelWindow - confirmed via a standalone probe that a genuine CHILD
# control HWND still returns a plausible answer with no COM error, but
# GetAncestor(hwnd, GA_ROOT) to the real top-level window is done anyway to
# match the documented contract, since "no exception raised" was confirmed
# elsewhere (during investigation of a related, harder bug - see CLAUDE.md)
# to not be reliable evidence of a correct answer for every window type.
# This module's own scope (flick navigation's ordinary NVDAObjects, not
# WebView2/MSIX split-process windows) has not shown that failure mode in
# testing; isOnCurrentVirtualDesktop's None-on-failure return makes callers
# fail safe regardless (skip the real focus/selection move) if this or any
# other part of the check is ever wrong.

from ctypes import POINTER, windll
from ctypes.wintypes import BOOL, DWORD, HWND

from comtypes import COMError, CoCreateInstance, CoInitializeEx, COMMETHOD, GUID, HRESULT, IUnknown
from logHandler import log

_CLSID_VirtualDesktopManager = GUID("{AA509086-5CA9-4C25-8F95-589D3C07B48A}")
_IID_IVirtualDesktopManager = GUID("{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}")

user32 = windll.user32
user32.GetAncestor.restype = HWND
user32.GetAncestor.argtypes = [HWND, DWORD]
GA_ROOT = 2


class _IVirtualDesktopManager(IUnknown):
	_case_insensitive_ = True
	_iid_ = _IID_IVirtualDesktopManager
	_methods_ = [
		COMMETHOD(
			[],
			HRESULT,
			"IsWindowOnCurrentVirtualDesktop",
			(["in"], HWND, "topLevelWindow"),
			(["out"], POINTER(BOOL), "onCurrentDesktop"),
		),
		COMMETHOD(
			[],
			HRESULT,
			"GetWindowDesktopId",
			(["in"], HWND, "topLevelWindow"),
			(["out"], POINTER(GUID), "desktopId"),
		),
		COMMETHOD(
			[],
			HRESULT,
			"MoveWindowToDesktop",
			(["in"], HWND, "topLevelWindow"),
			(["in"], POINTER(GUID), "desktopId"),
		),
	]


_comInitialized = False


def _ensureComInitialized() -> None:
	global _comInitialized
	if _comInitialized:
		return
	try:
		CoInitializeEx()
	except OSError:
		# Already initialized on this thread (e.g. RPC_E_CHANGED_MODE if some
		# other apartment model was set up first) - not fatal, comtypes calls
		# below still work against whichever apartment is already active.
		log.debugWarning("touchExplore: CoInitializeEx failed/already initialized", exc_info=True)
	_comInitialized = True


def isOnCurrentVirtualDesktop(windowHandle) -> "bool | None":
	"""Whether the given window handle is on the currently visible virtual
	desktop. Returns None (not True/False) if this can't be determined (no
	virtual desktops support, COM failure, invalid/destroyed window, etc) -
	callers must treat None as "unknown, don't act on virtual-desktop state"
	rather than assuming either True or False, since a false positive here
	(wrongly treating a hidden-desktop window as current) is the exact bug
	this module exists to prevent.
	"""
	if not windowHandle:
		return None
	topLevelHandle = user32.GetAncestor(windowHandle, GA_ROOT) or windowHandle
	_ensureComInitialized()
	try:
		mgr = CoCreateInstance(_CLSID_VirtualDesktopManager, interface=_IVirtualDesktopManager)
	except (COMError, OSError):
		log.debugWarning(
			"touchExplore: CoCreateInstance(CLSID_VirtualDesktopManager) failed",
			exc_info=True,
		)
		return None
	try:
		return bool(mgr.IsWindowOnCurrentVirtualDesktop(topLevelHandle))
	except (COMError, OSError):
		log.debugWarning(
			f"touchExplore: IsWindowOnCurrentVirtualDesktop failed for hwnd={windowHandle!r}",
			exc_info=True,
		)
		return None
