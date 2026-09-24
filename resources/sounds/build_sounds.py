"""Rebuilds the add-on's sound packs (touchExplore/globalPlugins/touchExplore/
sounds/<pack>/<cue>.wav) from their sources: silence trimmed, a fade-out of up
to 30ms (no click at the cut), peak-normalised, and converted to NVDA's own
sound format (22050 Hz mono 16-bit PCM WAV). Needs ffmpeg on PATH and numpy.

Sources:
- earcons: Kenney "Interface Sounds" 1.0, CC0 (public domain),
  https://kenney.nl/assets/interface-sounds - download and extract the zip,
  then run: python build_sounds.py <extracted kenney folder>
- classic: explore.mp3 / click.mp3 in this folder (the add-on's original
  sounds); the explore sound is cut to its audible first 150ms.
"""

import os
import subprocess
import sys
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "..", "touchExplore", "globalPlugins", "touchExplore", "sounds")
RATE = 22050

# cue: (Kenney file, peak dBFS). Chosen short (under 0.3s; the frequent ones
# under ~0.1s) and distinct in pitch/timbre from each other.
EARCONS = {
	"item": ("select_002.ogg", -3),
	"button": ("drop_003.ogg", -3),
	"link": ("glass_002.ogg", -4),
	"edit": ("pluck_001.ogg", -3),
	"toggle": ("toggle_004.ogg", -4),
	"gap": ("tick_002.ogg", -15),  # deliberately faint
	"boundary": ("bong_001.ogg", -3),
	"activate": ("confirmation_001.ogg", -4),
	"scroll": ("drop_001.ogg", -5),
	"on": ("maximize_009.ogg", -5),
	"off": ("minimize_009.ogg", -5),
}
# cue: (file in this folder, peak dBFS or None to keep its level, max seconds)
CLASSIC = {
	"item": ("explore.mp3", None, 0.15),
	"activate": ("click.mp3", None, None),
}


def decode(path):
	raw = subprocess.run(
		["ffmpeg", "-v", "quiet", "-i", path, "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
		capture_output=True,
		check=True,
	).stdout
	return np.frombuffer(raw, dtype=np.int16).astype(float) / 32768


def process(a, peakDb, maxLen=None):
	idx = np.where(np.abs(a) > 0.005)[0]
	a = a[max(0, idx[0] - 22) : idx[-1] + 1]  # trim silence, keep 1ms lead-in
	if maxLen:
		a = a[: int(maxLen * RATE)]
	fade = min(len(a) // 4, int(0.03 * RATE))
	a[len(a) - fade :] *= np.linspace(1, 0, fade)
	if peakDb is not None:
		a = a / np.max(np.abs(a)) * 10 ** (peakDb / 20)
	return (np.clip(a, -1, 1) * 32767).astype(np.int16)


def write(path, samples):
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with wave.open(path, "wb") as w:
		w.setnchannels(1)
		w.setsampwidth(2)
		w.setframerate(RATE)
		w.writeframes(samples.tobytes())


def main():
	if len(sys.argv) != 2:
		raise SystemExit(__doc__)
	kenneyAudio = os.path.join(sys.argv[1], "Audio")
	for cue, (name, peak) in EARCONS.items():
		samples = process(decode(os.path.join(kenneyAudio, name)), peak)
		write(os.path.join(OUT, "earcons", cue + ".wav"), samples)
		print(f"earcons/{cue}.wav {len(samples) / RATE * 1000:.0f}ms")
	for cue, (name, peak, maxLen) in CLASSIC.items():
		samples = process(decode(os.path.join(HERE, name)), peak, maxLen)
		write(os.path.join(OUT, "classic", cue + ".wav"), samples)
		print(f"classic/{cue}.wav {len(samples) / RATE * 1000:.0f}ms")


if __name__ == "__main__":
	main()
