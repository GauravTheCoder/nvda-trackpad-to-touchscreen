# Touch Explore Sounds - touch calibration: measuring how this person
# actually taps and flicks, and deriving thresholds from it.
#
# Recording works by wrapping the ACTIVE touch source's
# trackerManager.update on the instance (touchHandler.handler.trackerManager
# - NVDA's own TouchHandler for a real touchscreen, or this add-on's
# TrackpadTouchScreen in trackpad mode). Both feed every contact position and
# lift through that one method, from their own input thread, in screen
# pixels - exactly what touchTracker then classifies - so the measurements
# are of what NVDA really sees (for the trackpad that includes the
# lift-inference delay, which really is part of a tap's duration there).
# Nothing here is NVDA-version-specific beyond that one method's signature,
# update(ID, x, y, complete=False), unchanged since at least 2025.3.
#
# Everything except Recorder.install/uninstall is pure logic, testable
# without NVDA.

import threading
import time

from . import touchSettings

TAPS = "taps"
TWO_FINGER_TAPS = "twoFingerTaps"
DOUBLE_TAPS = "doubleTaps"
FLICKS = "flicks"

# (step, samples wanted). Order matters: flick acceptance compares against
# the tap drift measured in the earlier steps.
STEPS = (
	(TAPS, 8),
	(TWO_FINGER_TAPS, 5),
	(DOUBLE_TAPS, 5),
	(FLICKS, 8),
)

# Anything longer than this is a hold/explore, not an attempt at the step's
# gesture, and is rejected rather than stretching the timing.
MAX_SAMPLE_DURATION_S = 1.5
# Two single taps further apart (start to start) than this aren't a double
# tap attempt; the second becomes the first of a new pair.
MAX_DOUBLE_TAP_INTERVAL_S = 1.2
# Margins applied to what was measured.
DRIFT_MARGIN = 1.25
DRIFT_PADDING_MM = 0.3
FLICK_MARGIN = 0.6
TIMEOUT_MARGIN = 1.2
TIMEOUT_PADDING_MS = 30
MIN_CALIBRATED_TIMEOUT_MS = 200
MAX_CALIBRATED_TIMEOUT_MS = 1200


class Contact:
	__slots__ = ("startTime", "startX", "startY", "maxDX", "maxDY", "endTime")

	def __init__(self, t, x, y):
		self.startTime = t
		self.startX = x
		self.startY = y
		self.maxDX = 0
		self.maxDY = 0
		self.endTime = None

	@property
	def duration(self):
		return self.endTime - self.startTime

	@property
	def travelPx(self):
		"""Largest single-axis distance from the start point - the quantity
		touchTracker compares against both maxAccidentalDrift (tap) and
		minFlickDistance (flick), per axis.
		"""
		return max(self.maxDX, self.maxDY)


class Episode:
	"""Everything from the first finger touching down (with none already
	down) until every finger has lifted.
	"""

	def __init__(self, contacts, pxPerMm):
		self.contacts = contacts
		self.pxPerMm = pxPerMm

	@property
	def fingers(self):
		return len(self.contacts)

	@property
	def startTime(self):
		return min(c.startTime for c in self.contacts)

	@property
	def duration(self):
		return max(c.endTime for c in self.contacts) - self.startTime

	def travelsMM(self):
		return [c.travelPx / self.pxPerMm for c in self.contacts]


class EpisodeBuilder:
	"""Turns update(ID, x, y, complete) events into Episodes. Repeated
	updates at an unchanged position (e.g. the trackpad's poll re-feeds) are
	harmless.
	"""

	def __init__(self):
		self._live = {}
		self._done = []

	def feed(self, t, contactId, x, y, complete, pxPerMm):
		contact = self._live.get(contactId)
		if contact is None:
			if complete:
				return None  # a lift for a contact that started before recording
			contact = self._live[contactId] = Contact(t, x, y)
			self._pxPerMm = pxPerMm
		contact.maxDX = max(contact.maxDX, abs(x - contact.startX))
		contact.maxDY = max(contact.maxDY, abs(y - contact.startY))
		if not complete:
			return None
		contact.endTime = t
		self._done.append(self._live.pop(contactId))
		if self._live:
			return None
		episode = Episode(self._done, self._pxPerMm)
		self._done = []
		return episode


class Recorder:
	"""Collects raw events from a trackerManager's input thread; the dialog
	drains them on the main thread (drain()).
	"""

	def __init__(self, trackerManager, pxPerMmGetter):
		self._trackerManager = trackerManager
		self._pxPerMmGetter = pxPerMmGetter
		self._lock = threading.Lock()
		self._events = []
		self._installed = False

	def install(self):
		original = self._trackerManager.update

		def update(ID, x, y, complete=False):
			with self._lock:
				self._events.append((time.time(), ID, x, y, complete, self._pxPerMmGetter()))
			return original(ID, x, y, complete)

		self._trackerManager.update = update
		self._installed = True

	def uninstall(self):
		if self._installed:
			# Removing the instance attribute re-exposes the class's own method.
			del self._trackerManager.update
			self._installed = False

	def drain(self):
		with self._lock:
			events, self._events = self._events, []
		return events


class Session:
	"""Walks the steps, deciding which episodes count as samples of the
	current step. feed() returns "accepted", "rejected" or None (an episode
	that's only half of a double tap so far).
	"""

	def __init__(self):
		self.stepIndex = 0
		self.samples = {step: [] for step, _count in STEPS}
		self._pendingTap = None

	@property
	def step(self):
		return STEPS[self.stepIndex][0] if self.stepIndex < len(STEPS) else None

	@property
	def wanted(self):
		return STEPS[self.stepIndex][1]

	@property
	def done(self):
		return self.stepIndex >= len(STEPS)

	def count(self):
		return len(self.samples[self.step])

	def skipStep(self):
		self.stepIndex += 1
		self._pendingTap = None

	def _maxTapTravelMM(self):
		travels = [m for step in (TAPS, TWO_FINGER_TAPS) for ep in self.samples[step] for m in ep.travelsMM()]
		return max(travels) if travels else None

	def feed(self, episode):
		step = self.step
		if step is None or episode.duration > MAX_SAMPLE_DURATION_S:
			return "rejected"
		if step == TAPS:
			if episode.fingers != 1:
				return "rejected"
			self.samples[step].append(episode)
		elif step == TWO_FINGER_TAPS:
			if episode.fingers != 2:
				return "rejected"
			self.samples[step].append(episode)
		elif step == DOUBLE_TAPS:
			if episode.fingers != 1:
				self._pendingTap = None
				return "rejected"
			first = self._pendingTap
			if first is None or episode.startTime - first.startTime > MAX_DOUBLE_TAP_INTERVAL_S:
				self._pendingTap = episode
				return None
			self._pendingTap = None
			self.samples[step].append((first, episode))
		elif step == FLICKS:
			maxTap = self._maxTapTravelMM()
			travel = episode.travelsMM()[0] if episode.fingers == 1 else 0
			# A flick attempt has to move clearly further than any tap did.
			if episode.fingers != 1 or (maxTap is not None and travel < maxTap * 2):
				return "rejected"
			self.samples[step].append(episode)
		if len(self.samples[step]) >= self.wanted:
			self.stepIndex += 1
			self._pendingTap = None
		return "accepted"


def _round1(value):
	return round(value * 10) / 10


def _ceil1(value):
	return -(-round(value * 1000) // 100) / 10


def compute(session, current):
	"""Derives new settings from session's samples. current is the source's
	existing {"tapDriftMM", "flickDistanceMM", "pinchDistanceMM", "timeoutMS"};
	anything a skipped step would have measured is kept from it. Returns
	(newSettings, warnings), warnings being a list of plain-language
	strings.
	"""
	s = session.samples
	new = dict(current)
	warnings = []

	tapEpisodes = s[TAPS] + s[TWO_FINGER_TAPS] + [ep for pair in s[DOUBLE_TAPS] for ep in pair]
	tapTravels = [m for ep in tapEpisodes for m in ep.travelsMM()]
	if tapTravels:
		drift = max(tapTravels) * DRIFT_MARGIN + DRIFT_PADDING_MM
		new["tapDriftMM"] = _round1(min(max(drift, touchSettings.MIN_DRIFT_MM), touchSettings.MAX_DRIFT_MM))

	if s[FLICKS]:
		shortestFlick = min(ep.travelsMM()[0] for ep in s[FLICKS])
		flick = shortestFlick * FLICK_MARGIN
		floor = new["tapDriftMM"] * touchSettings.MIN_FLICK_TO_DRIFT_RATIO
		if flick < floor:
			flick = floor
			if floor > shortestFlick * 0.9:
				warnings.append(
					# Translators: calibration warning when taps and flicks overlap in distance.
					_(
						"Some of your flicks were barely longer than your taps, so short flicks may "
						"still be read as taps. Longer flicks will be recognised reliably.",
					),
				)
		new["flickDistanceMM"] = _round1(min(max(flick, touchSettings.MIN_FLICK_MM), touchSettings.MAX_FLICK_MM))

	# The one timeout must cover every gesture that has to finish (or, for a
	# double tap, restart) inside it - see touchSettings.py.
	durations = [ep.duration for ep in tapEpisodes + s[FLICKS]]
	durations += [second.startTime - first.startTime for first, second in s[DOUBLE_TAPS]]
	if durations:
		timeout = max(durations) * 1000 * TIMEOUT_MARGIN + TIMEOUT_PADDING_MS
		timeout = int(-(-timeout // 10) * 10)  # round up to 10ms
		new["timeoutMS"] = min(max(timeout, MIN_CALIBRATED_TIMEOUT_MS), MAX_CALIBRATED_TIMEOUT_MS)
		if timeout > MAX_CALIBRATED_TIMEOUT_MS:
			warnings.append(
				# Translators: calibration warning when gestures were slower than the maximum timing.
				_(
					"Some gestures took longer than {ms} milliseconds, the longest time allowed. "
					"Slower gestures may be read as exploring instead.",
				).format(ms=MAX_CALIBRATED_TIMEOUT_MS),
			)

	# Whatever was or wasn't measured (e.g. flicks skipped but taps turned
	# out sloppier than before), keep flick and pinch clearly above the tap
	# tolerance - touchSettings.validate requires it. Rounded UP, so rounding
	# can't land just under the ratio.
	floor = _ceil1(new["tapDriftMM"] * touchSettings.MIN_FLICK_TO_DRIFT_RATIO)
	new["flickDistanceMM"] = max(new["flickDistanceMM"], floor)
	new["pinchDistanceMM"] = max(new["pinchDistanceMM"], floor)
	return new, warnings
