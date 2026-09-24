# Touch Explore Sounds - short audio cues (earcons), optionally placed where
# on screen they happened: panned left/right by x, and pitched higher/lower
# by y (higher on screen = higher pitch).
#
# Why not nvwave.playWaveFile: it plays a file exactly as stored, and panning
# can't be done afterwards with WavePlayer.setVolume(left=..., right=...),
# because WavePlayer.stop() and open() re-apply NVDA's sound volume to every
# channel (_setVolumeFromConfig, nvwave.py in NVDA 2026.2). So the pan is
# baked into stereo sample data instead, and fed to one persistent
# WavePlayer with purpose=AudioPurpose.SOUNDS - which is what makes NVDA's
# own "sound volume" / "follows voice volume" settings apply on top, the
# same as for playWaveFile. nvwave.decide_playWaveFile is still consulted,
# so add-ons that mute NVDA's sounds mute these too.
#
# Packs live in sounds/<pack>/<cue>.wav (mono, 16-bit; 22050 Hz like NVDA's
# own waves/*.wav). A cue missing from the chosen pack falls back to the
# "earcons" pack. The earcons are from Kenney's "Interface Sounds" pack
# (CC0, see sounds/earcons/License.txt); "classic" is this add-on's
# original explore/click sounds, trimmed to their audible part.

import array
import os
import wave
import config
import controlTypes
import nvwave
from logHandler import log

from . import monitors

SOUNDS_DIR = os.path.join(os.path.dirname(__file__), "sounds")
DEFAULT_PACK = "earcons"
PACKS = ("earcons", "classic")

ITEM = "item"
BUTTON = "button"
LINK = "link"
EDIT = "edit"
TOGGLE = "toggle"
GAP = "gap"
BOUNDARY = "boundary"
ACTIVATE = "activate"
SCROLL = "scroll"
ON = "on"
OFF = "off"

# Roles that get their own cue when role sounds are on; anything else
# touchable is ITEM. Looked up by name so a role missing from an older NVDA
# (e.g. SWITCH) is simply skipped.
_ROLE_CUES = {
	BUTTON: ("BUTTON", "MENUBUTTON", "SPLITBUTTON", "DROPDOWNBUTTON", "TREEVIEWBUTTON"),
	LINK: ("LINK",),
	EDIT: ("EDITABLETEXT", "PASSWORDEDIT", "COMBOBOX", "SPINBUTTON"),
	TOGGLE: ("CHECKBOX", "RADIOBUTTON", "TOGGLEBUTTON", "SWITCH", "CHECKMENUITEM", "RADIOMENUITEM"),
}
_cueForRole = {
	getattr(controlTypes.Role, name): cue
	for cue, names in _ROLE_CUES.items()
	for name in names
	if hasattr(controlTypes.Role, name)
}

# Pan positions are quantised so each cue's rendered data can be cached.
_PAN_STEPS = 10
# Height is sonified as pitch: the top edge of the screen plays this many
# semitones above the original sound, the bottom edge this many below, the
# middle unchanged. Quantised to whole semitones - fine enough to hear
# "higher/lower", and it keeps the cache small.
PITCH_RANGE_SEMITONES = 6
# Rendered (panned + pitched) sounds kept; oldest dropped beyond this. Each is
# a few KB (cues are short), and exploring one screen region reuses a
# handful of pan/pitch combinations.
_MAX_RENDERED = 256

_samples = {}  # path -> (mono array('h'), sampleRate), or None if unreadable
_rendered = {}  # (path, panStep, semitones) -> bytes, in insertion order
_players = {}  # sampleRate -> nvwave.WavePlayer


def _conf():
	return config.conf["touchExplore"]


def cueForObject(obj):
	if not _conf()["roleSounds"]:
		return ITEM
	return _cueForRole.get(obj.role, ITEM)


def _path(cue, pack=None):
	pack = pack or _conf()["soundPack"]
	path = os.path.join(SOUNDS_DIR, pack, cue + ".wav")
	if not os.path.isfile(path):
		path = os.path.join(SOUNDS_DIR, DEFAULT_PACK, cue + ".wav")
	return path


def _load(path):
	if path not in _samples:
		try:
			with wave.open(path, "rb") as w:
				if w.getsampwidth() != 2:
					raise ValueError("only 16-bit sounds are supported")
				data = array.array("h", w.readframes(w.getnframes()))
				if w.getnchannels() == 2:
					data = data[0::2]  # keep one channel; panning re-creates stereo
				_samples[path] = (data, w.getframerate())
		except Exception:
			log.debugWarning(f"touchExplore: can't load sound {path!r}", exc_info=True)
			_samples[path] = None
	return _samples[path]


def _pitched(mono, semitones):
	"""mono resampled so it plays semitones higher (positive) or lower
	(negative) at the same sample rate - by linear interpolation, the way a
	tape sped up or slowed down changes pitch. Duration changes with it (a
	sound an octave up is half as long), which for cues this short just
	makes high ones crisper. Done on the samples, not by changing the
	player's sample rate, so one WavePlayer serves every pitch.
	"""
	if not semitones:
		return mono
	factor = 2 ** (semitones / 12)
	length = int((len(mono) - 1) / factor) + 1
	last = len(mono) - 1
	out = array.array("h", bytes(length * 2))
	for i in range(length):
		pos = i * factor
		j = int(pos)
		if j >= last:
			out[i] = mono[last]
		else:
			frac = pos - j
			out[i] = int(mono[j] + (mono[j + 1] - mono[j]) * frac)
	return out


def _renderedData(path, mono, pan, height):
	"""Interleaved stereo bytes for mono at pan (-1 = full left, 0 = centre,
	1 = full right) and height (-1 = bottom of the screen, lowest pitch; 0 =
	middle, original pitch; 1 = top, highest pitch). Linear pan gains that
	keep the centre at full level (both channels 1.0) rather than
	constant-power, so centred cues are exactly as loud as the mono
	originals were.
	"""
	panStep = round(max(-1.0, min(1.0, pan)) * _PAN_STEPS)
	semitones = round(max(-1.0, min(1.0, height)) * PITCH_RANGE_SEMITONES)
	key = (path, panStep, semitones)
	data = _rendered.get(key)
	if data is None:
		source = _pitched(mono, semitones)
		pan = panStep / _PAN_STEPS
		leftGain = 1.0 if pan <= 0 else 1.0 - pan
		rightGain = 1.0 if pan >= 0 else 1.0 + pan
		out = array.array("h", bytes(len(source) * 4))
		out[0::2] = array.array("h", (int(s * leftGain) for s in source))
		out[1::2] = array.array("h", (int(s * rightGain) for s in source))
		data = _rendered[key] = out.tobytes()
		if len(_rendered) > _MAX_RENDERED:
			del _rendered[next(iter(_rendered))]
	return data


def _player(sampleRate):
	player = _players.get(sampleRate)
	if player is None:
		player = _players[sampleRate] = nvwave.WavePlayer(
			channels=2,
			samplesPerSec=sampleRate,
			bitsPerSample=16,
			outputDevice=config.conf["audio"]["outputDevice"],
			wantDucking=False,
			purpose=nvwave.AudioPurpose.SOUNDS,
		)
	return player


def play(cue, x=None, y=None, *, pack=None, pan=None, height=None, preview=False):
	"""Plays cue at screen position (x, y) if given: panned left/right by x
	and pitched up/down by y, each only if that setting is on. Main thread
	only (touch scripts and the patched moveTo both run there). A new cue
	cuts off the previous one, like NVDA's own sounds. pack/pan/height
	override the settings, and preview plays even with sounds turned off -
	both for the settings panel's preview of unsaved choices.
	"""
	conf = _conf()
	if not preview and not conf["soundsEnabled"]:
		return
	path = _path(cue, pack)
	if not nvwave.decide_playWaveFile.decide(fileName=path, asynchronous=True, isSpeechWaveFileCommand=False):
		return
	loaded = _load(path)
	if loaded is None:
		return
	mono, sampleRate = loaded
	if pan is None or height is None:
		pointPan, pointHeight = positionForPoint(x, y)
		if pan is None:
			pan = pointPan if conf["panSounds"] else 0.0
		if height is None:
			height = pointHeight if conf["pitchSounds"] else 0.0
	try:
		player = _player(sampleRate)
		player.stop()
		player.feed(_renderedData(path, mono, pan, height))
	except Exception:
		# E.g. no audio output device - never let a sound break a gesture.
		log.debugWarning(f"touchExplore: failed to play {cue!r}", exc_info=True)
		_players.pop(sampleRate, None)


def playForObject(obj, x=None, y=None):
	if x is None:
		x, y = centreOf(obj)
	play(cueForObject(obj), x, y)


def centreOf(obj):
	try:
		location = obj.location
		return (location.left + location.width // 2, location.top + location.height // 2)
	except Exception:
		return (None, None)


def positionForPoint(x, y):
	"""(pan, height), each -1..1, for (x, y) within the monitor containing
	it: pan -1 at that monitor's left edge to 1 at its right, height 1 at
	its top edge to -1 at its bottom. (0, 0) - centred, original pitch -
	when there's no position.
	"""
	if x is None or y is None:
		return (0.0, 0.0)
	rect = monitors.rectForPoint(x, y)
	if not rect or rect[2] <= 0 or rect[3] <= 0:
		return (0.0, 0.0)
	left, top, width, height = rect
	return ((x - left) / width * 2 - 1, 1 - (y - top) / height * 2)


def terminate():
	for player in _players.values():
		try:
			player.close()
		except Exception:
			pass
	_players.clear()
	_rendered.clear()
	_samples.clear()
