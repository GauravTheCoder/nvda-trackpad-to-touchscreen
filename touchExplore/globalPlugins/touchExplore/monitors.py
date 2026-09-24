# Touch Explore Sounds - monitor geometry helpers, shared by trackpadTouch.py
# (which monitor the trackpad maps onto) and audioCues.py (left/right pan).
#
# Kept in one module on purpose: ctypes argtypes/restype are process-global
# attributes of the underlying function object, so two modules each
# declaring GetMonitorInfoW with their OWN MONITORINFO Structure class would
# break each other - whichever module loaded last would make the other's
# byref(itsOwnStruct) fail with ctypes.ArgumentError.

from ctypes import POINTER, Structure, byref, sizeof, windll
from ctypes.wintypes import BOOL, DWORD, HANDLE, HWND, POINT, RECT

MONITOR_DEFAULTTONEAREST = 2
SM_CXSCREEN = 0
SM_CYSCREEN = 1


class MONITORINFO(Structure):
	_fields_ = [
		("cbSize", DWORD),
		("rcMonitor", RECT),
		("rcWork", RECT),
		("dwFlags", DWORD),
	]


user32 = windll.user32
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = HWND
user32.MonitorFromWindow.argtypes = [HWND, DWORD]
user32.MonitorFromWindow.restype = HANDLE
user32.MonitorFromPoint.argtypes = [POINT, DWORD]
user32.MonitorFromPoint.restype = HANDLE
user32.GetMonitorInfoW.argtypes = [HANDLE, POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = BOOL


def primaryRect():
	"""(left, top, width, height) of the primary monitor."""
	return (0, 0, user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN))


def _rect(monitor):
	info = MONITORINFO()
	info.cbSize = sizeof(MONITORINFO)
	if not monitor or not user32.GetMonitorInfoW(monitor, byref(info)):
		return None
	rc = info.rcMonitor
	return (rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top)


def foregroundRect():
	"""(left, top, width, height) of the monitor holding the foreground
	window, or the primary monitor if that can't be determined.
	"""
	monitor = user32.MonitorFromWindow(user32.GetForegroundWindow(), MONITOR_DEFAULTTONEAREST)
	return _rect(monitor) or primaryRect()


def rectForPoint(x, y):
	"""(left, top, width, height) of the monitor containing (x, y), or None."""
	return _rect(user32.MonitorFromPoint(POINT(int(x), int(y)), MONITOR_DEFAULTTONEAREST))
