#!/usr/bin/env python3
"""Builds touchExplore.nvda-addon from the touchExplore/ source folder."""

import os
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(ROOT, "touchExplore")
OUT_FILE = os.path.join(ROOT, "touchExplore.nvda-addon")


def main():
	if not os.path.isdir(SRC_DIR):
		raise SystemExit(f"Source folder not found: {SRC_DIR}")
	if os.path.exists(OUT_FILE):
		os.remove(OUT_FILE)
	with zipfile.ZipFile(OUT_FILE, "w", zipfile.ZIP_DEFLATED) as zf:
		for dirpath, _dirnames, filenames in os.walk(SRC_DIR):
			for filename in filenames:
				fullPath = os.path.join(dirpath, filename)
				relPath = os.path.relpath(fullPath, SRC_DIR).replace(os.sep, "/")
				zf.write(fullPath, relPath)
	print(f"Built {OUT_FILE}")


if __name__ == "__main__":
	main()
