# Touch Explore Sounds - settings panel (NVDA Settings > Touch Explore) and
# the touch calibration dialog.

import config
import gui
import inputCore
import tones
import touchHandler
import ui
import wx
from gui import guiHelper, nvdaControls
from wx.lib.expando import ExpandoTextCtrl
from gui.settingsDialogs import SettingsPanel
from logHandler import log

from . import audioCues, calibration, touchSettings

_SOURCE_LABELS = {
	# Translators: name of the real touchscreen as a touch input source.
	touchSettings.TOUCHSCREEN: _("Touchscreen"),
	# Translators: name of the trackpad (in trackpad touchscreen mode) as a touch input source.
	touchSettings.TRACKPAD: _("Trackpad"),
}


def activeCalibrationTarget():
	"""(source, trackerManager) for whichever touch input is live right now,
	or None. touchHandler.handler is NVDA's own TouchHandler for a real
	touchscreen, or this add-on's TrackpadTouchScreen while trackpad mode is
	on (see trackpadTouch.py) - only one is active at a time.
	"""
	from .trackpadTouch import TrackpadTouchScreen

	handler = touchHandler.handler
	if handler is None:
		return None
	source = touchSettings.TRACKPAD if isinstance(handler, TrackpadTouchScreen) else touchSettings.TOUCHSCREEN
	return source, handler.trackerManager


def openCalibration(parent=None, onSaved=None):
	target = activeCalibrationTarget()
	if target is None:
		gui.messageBox(
			# Translators: shown when calibration is started with no touch input active.
			_(
				"No touch input is active. For a touchscreen, turn on NVDA's touch support. "
				"To calibrate the trackpad, turn on trackpad touchscreen mode "
				"(NVDA+Control+Shift+T) first.",
			),
			# Translators: title of the touch calibration dialog and its messages.
			_("Touch calibration"),
			wx.OK | wx.ICON_INFORMATION,
			parent,
		)
		return
	source, trackerManager = target
	gui.mainFrame.prePopup()
	dialog = CalibrationDialog(parent or gui.mainFrame, source, trackerManager, onSaved)
	try:
		dialog.ShowModal()
	finally:
		# wx's own Escape/Cancel handling ends the modal loop in C++, not
		# through any Python override, so this is the one place guaranteed to
		# run however the dialog closed: touch gestures must never be left
		# blocked, nor the recorder installed.
		dialog.stopCapture()
		dialog.Destroy()
		gui.mainFrame.postPopup()


_ANNOUNCE_DELAY_MS = 300

_STEP_INSTRUCTIONS = {
	# Translators: calibration step instructions. {n} is how many to do.
	calibration.TAPS: _("Step 1 of 4: tap anywhere with one finger, the way you normally would. {n} taps."),
	calibration.TWO_FINGER_TAPS: _("Step 2 of 4: tap with two fingers at the same time. {n} times."),
	calibration.DOUBLE_TAPS: _(
		"Step 3 of 4: double tap with one finger, the way you normally would. {n} double taps.",
	),
	calibration.FLICKS: _(
		"Step 4 of 4: flick with one finger in any direction, the way you normally would. {n} flicks.",
	),
}


class CalibrationDialog(wx.Dialog):
	"""Speech-guided: every instruction and every counted/rejected sample is
	spoken and beeped, so it's usable without reading the screen. While it's
	capturing, touch gestures are blocked (inputCore.decide_executeGesture)
	so the calibration taps don't activate or explore anything; they're
	unblocked again for the results, so a touch-only user can reach Save.
	Keyboard: Escape cancels at any point.
	"""

	def __init__(self, parent, source, trackerManager, onSaved):
		super().__init__(
			parent,
			# Translators: title of the calibration dialog; {source} is Touchscreen or Trackpad.
			title=_("Touch calibration: {source}").format(source=_SOURCE_LABELS[source]),
		)
		self._source = source
		self._onSaved = onSaved
		self._session = calibration.Session()
		self._builder = calibration.EpisodeBuilder()
		self._recorder = calibration.Recorder(trackerManager, self._pxPerMm)
		self._capturing = False
		self._newSettings = None

		sHelper = guiHelper.BoxSizerHelper(self, orientation=wx.VERTICAL)
		# Read-only but focusable, so the current instruction/results can be
		# reviewed with the keyboard.
		# (ExpandoTextCtrl for the same reason NVDA's own General settings
		# panel uses it: a plain read-only TextCtrl doesn't take keyboard
		# focus.)
		self._status = sHelper.addItem(
			ExpandoTextCtrl(self, size=(self.FromDIP(500), -1), style=wx.TE_READONLY),
			flag=wx.EXPAND,
		)
		buttons = guiHelper.ButtonHelper(wx.HORIZONTAL)
		# Translators: button in the calibration dialog.
		self._skipButton = buttons.addButton(self, label=_("&Skip this step"))
		self._skipButton.Bind(wx.EVT_BUTTON, self._onSkip)
		# Translators: button in the calibration dialog.
		self._saveButton = buttons.addButton(self, label=_("&Save"))
		self._saveButton.Bind(wx.EVT_BUTTON, self._onSave)
		self._saveButton.Hide()
		self._cancelButton = buttons.addButton(self, id=wx.ID_CANCEL)
		sHelper.addDialogDismissButtons(buttons)
		self.SetEscapeId(wx.ID_CANCEL)
		self.Bind(wx.EVT_SHOW, self._onShow)

		mainSizer = wx.BoxSizer(wx.VERTICAL)
		mainSizer.Add(sHelper.sizer, border=guiHelper.BORDER_FOR_DIALOGS, flag=wx.ALL | wx.EXPAND)
		self.SetSizer(mainSizer)
		mainSizer.Fit(self)
		self.CentreOnScreen()

		self._timer = wx.Timer(self)
		self.Bind(wx.EVT_TIMER, self._onTimer, self._timer)

	def _pxPerMm(self):
		# Called on the input thread, from inside trackerManager.update: by
		# then trackpadTouch has applied this gesture's density (see
		# TrackpadTouchScreen._applyThresholds), and the touchscreen's is
		# applied whenever trackpad mode isn't on.
		return touchSettings.activePxPerMm() or touchSettings.screenPxPerMm()

	def _onShow(self, evt):
		evt.Skip()
		if evt.IsShown() and not self._capturing and self._newSettings is None:
			self._startCapture()

	def _startCapture(self):
		self._recorder.install()
		inputCore.decide_executeGesture.register(self._blockTouchGestures)
		self._capturing = True
		self._timer.Start(100)
		self._status.SetFocus()
		# Delayed: NVDA announcing the dialog and the newly focused field would
		# otherwise cut this off (a focus change cancels speech).
		wx.CallLater(
			_ANNOUNCE_DELAY_MS,
			self._announceStep,
			# Translators: spoken/shown at the start of calibration.
			_("While calibrating, touch gestures don't do anything. Press Escape to cancel. "),
		)

	def stopCapture(self):
		if not self._capturing:
			return
		self._capturing = False
		self._timer.Stop()
		inputCore.decide_executeGesture.unregister(self._blockTouchGestures)
		self._recorder.uninstall()

	def _blockTouchGestures(self, gesture, **kwargs):
		return not isinstance(gesture, touchHandler.TouchInputGesture)

	def _announceStep(self, prefix=""):
		if not self or self._session.done:
			return  # dialog closed (or finished) before a delayed call ran
		step = self._session.step
		text = prefix + _STEP_INSTRUCTIONS[step].format(n=self._session.wanted)
		self._status.SetValue(text)
		ui.message(text)

	def _onTimer(self, evt):
		for t, contactId, x, y, complete, pxPerMm in self._recorder.drain():
			episode = self._builder.feed(t, contactId, x, y, complete, pxPerMm)
			if episode is None or not self._capturing:
				continue
			step = self._session.step
			result = self._session.feed(episode)
			if result == "rejected":
				tones.beep(220, 120)
				# Translators: spoken when a calibration sample doesn't match the current step.
				ui.message(_("Try again"))
			elif result == "accepted":
				tones.beep(880, 50)
				if self._session.done:
					self._finish()
				elif self._session.step != step:
					self._announceStep()
				else:
					ui.message(str(self._session.count()))

	def _onSkip(self, evt):
		self._session.skipStep()
		if self._session.done:
			self._finish()
		else:
			self._announceStep()

	def _finish(self):
		self.stopCapture()
		current = {name: touchSettings.getSetting(self._source, name) for name in touchSettings.SETTING_NAMES}
		self._newSettings, warnings = calibration.compute(self._session, current)
		# Translators: calibration results, one line per setting; {new} and {old} are numbers.
		lines = [
			_("Tap movement tolerance: {new} mm (was {old} mm)").format(
				new=self._newSettings["tapDriftMM"],
				old=current["tapDriftMM"],
			),
			# Translators: calibration result line.
			_("Minimum flick distance: {new} mm (was {old} mm)").format(
				new=self._newSettings["flickDistanceMM"],
				old=current["flickDistanceMM"],
			),
			# Translators: calibration result line.
			_("Gesture time: {new} ms (was {old} ms)").format(
				new=self._newSettings["timeoutMS"],
				old=current["timeoutMS"],
			),
		]
		lines += warnings
		# Translators: final line of the calibration results.
		lines.append(_("Press Save to use these settings, or Cancel to keep the old ones."))
		text = "\n".join(lines)
		self._status.SetValue(text)
		self._skipButton.Hide()
		self._saveButton.Show()
		self.Layout()
		# Focus Save so Enter accepts; the results stay reviewable one
		# Shift+Tab back. Spoken after the focus announcement, not cut off by it.
		self._saveButton.SetFocus()
		wx.CallLater(_ANNOUNCE_DELAY_MS, ui.message, text)

	def _onSave(self, evt):
		for name, value in self._newSettings.items():
			touchSettings.setSetting(self._source, name, value)
		touchSettings.reapply()
		log.debug(f"touchExplore: calibration saved for {self._source}: {self._newSettings}")
		if self._onSaved:
			self._onSaved(self._source, self._newSettings)
		self.EndModal(wx.ID_OK)



class TouchExploreSettingsPanel(SettingsPanel):
	# Translators: title of this add-on's panel in NVDA's settings dialog.
	title = _("Touch Explore")

	def makeSettings(self, settingsSizer):
		sHelper = guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
		sHelper.addItem(
			wx.StaticText(
				self,
				# Translators: explanation at the top of the Touch Explore settings panel.
				label=_(
					"Distances are in millimetres on the touch surface, so they feel the same "
					"on any screen or trackpad. Calibrate touch measures your own taps and flicks.",
				),
			),
		)
		self._controls = {}
		for source in touchSettings.SOURCES:
			box = wx.StaticBoxSizer(wx.VERTICAL, self, label=_SOURCE_LABELS[source])
			boxHelper = guiHelper.BoxSizerHelper(box.GetStaticBox(), sizer=box)
			sHelper.addItem(boxHelper)
			controls = {}
			# Translators: label for the tap movement tolerance setting.
			controls["tapDriftMM"] = boxHelper.addLabeledControl(
				_("Tap movement tolerance (mm)"),
				wx.TextCtrl,
			)
			# Translators: label for the minimum flick distance setting.
			controls["flickDistanceMM"] = boxHelper.addLabeledControl(
				_("Minimum flick distance (mm)"),
				wx.TextCtrl,
			)
			# Translators: label for the minimum pinch distance setting.
			controls["pinchDistanceMM"] = boxHelper.addLabeledControl(
				_("Minimum pinch distance (mm)"),
				wx.TextCtrl,
			)
			controls["timeoutMS"] = boxHelper.addLabeledControl(
				# Translators: label for the gesture timing setting (milliseconds).
				_("Gesture time: longest tap or flick, and double tap window (ms)"),
				nvdaControls.SelectOnFocusSpinCtrl,
				min=touchSettings.MIN_TIMEOUT_MS,
				max=touchSettings.MAX_TIMEOUT_MS,
				initial=touchSettings.getSetting(source, "timeoutMS"),
			)
			self._controls[source] = controls
			self._load(source, {name: touchSettings.getSetting(source, name) for name in touchSettings.SETTING_NAMES})

		self._makeSoundSettings(sHelper)

		buttons = guiHelper.ButtonHelper(wx.HORIZONTAL)
		# Translators: button that opens the touch calibration dialog.
		calibrate = buttons.addButton(self, label=_("&Calibrate touch..."))
		calibrate.Bind(wx.EVT_BUTTON, self._onCalibrate)
		# Translators: button that resets the touch settings to their defaults.
		reset = buttons.addButton(self, label=_("&Reset to defaults"))
		reset.Bind(wx.EVT_BUTTON, self._onReset)
		sHelper.addItem(buttons)

	_PACK_LABELS = {
		# Translators: name of the default sound pack (short interface earcons).
		"earcons": _("Earcons"),
		# Translators: name of the sound pack with this add-on's original sounds.
		"classic": _("Classic"),
	}

	def _makeSoundSettings(self, sHelper):
		# Translators: label of the group of sound settings.
		box = wx.StaticBoxSizer(wx.VERTICAL, self, label=_("Sounds"))
		boxParent = box.GetStaticBox()
		boxHelper = guiHelper.BoxSizerHelper(boxParent, sizer=box)
		sHelper.addItem(boxHelper)
		conf = config.conf[touchSettings.CONFIG_SECTION]
		self._soundCheckboxes = {}
		for key, label in (
			# Translators: sound setting checkbox.
			("soundsEnabled", _("&Play sounds")),
			# Translators: sound setting checkbox.
			("roleSounds", _("Different sounds for &buttons, links, edit fields and check boxes")),
			# Translators: sound setting checkbox.
			("gapSound", _("Sound when your finger moves off an item into &empty space")),
			# Translators: sound setting checkbox.
			("panSounds", _("Play sounds &left or right by where they are on the screen")),
			# Translators: sound setting checkbox.
			("pitchSounds", _("Play sounds &higher or lower by how high up the screen they are")),
		):
			checkbox = boxHelper.addItem(wx.CheckBox(boxParent, label=label))
			checkbox.SetValue(conf[key])
			self._soundCheckboxes[key] = checkbox
		self._packChoice = boxHelper.addLabeledControl(
			# Translators: label of the sound pack choice.
			_("Sound pac&k:"),
			wx.Choice,
			choices=[self._PACK_LABELS[pack] for pack in audioCues.PACKS],
		)
		self._packChoice.SetSelection(audioCues.PACKS.index(conf["soundPack"]))
		# Translators: button that plays a sample of the selected sound pack.
		preview = boxHelper.addItem(wx.Button(boxParent, label=_("Pre&view sounds")))
		preview.Bind(wx.EVT_BUTTON, self._onPreview)

	def _onPreview(self, evt):
		"""Plays the main cues of the selected (not necessarily saved) pack,
		one after another, moving from the top left of the screen to the
		bottom right - so panning and pitch, when ticked, can be heard too.
		"""
		pack = audioCues.PACKS[self._packChoice.GetSelection()]
		cues = (audioCues.ITEM, audioCues.BUTTON, audioCues.LINK, audioCues.EDIT, audioCues.TOGGLE, audioCues.ACTIVATE)
		panned = self._soundCheckboxes["panSounds"].GetValue()
		pitched = self._soundCheckboxes["pitchSounds"].GetValue()
		for index, cue in enumerate(cues):
			progress = index / (len(cues) - 1) * 2 - 1  # -1 .. 1
			wx.CallLater(
				1 + index * 450,
				audioCues.play,
				cue,
				pack=pack,
				pan=progress if panned else 0.0,
				height=-progress if pitched else 0.0,
				preview=True,
			)

	def _load(self, source, values):
		for name, control in self._controls[source].items():
			value = values[name]
			if isinstance(control, wx.TextCtrl):
				control.SetValue(f"{value:g}")
			else:
				control.SetValue(int(value))

	def _read(self, source):
		"""Returns (values, None) or (None, (errorMessage,))."""
		values = {}
		for name, control in self._controls[source].items():
			if isinstance(control, wx.TextCtrl):
				try:
					values[name] = float(control.GetValue().strip().replace(",", "."))
				except ValueError:
					# Translators: validation error for a millimetre setting that isn't a number.
					return None, (_("Please enter a number, such as 2.5."),)
			else:
				values[name] = int(control.GetValue())
		error = touchSettings.validate(
			values["tapDriftMM"],
			values["flickDistanceMM"],
			values["pinchDistanceMM"],
			values["timeoutMS"],
		)
		return (None, (error,)) if error else (values, None)

	def _onCalibrate(self, evt):
		openCalibration(self, onSaved=self._load)

	def _onReset(self, evt):
		for source in touchSettings.SOURCES:
			self._load(source, touchSettings.DEFAULTS[source])

	def isValid(self):
		for source in touchSettings.SOURCES:
			_values, problem = self._read(source)
			if problem:
				self._validationErrorMessageBox(problem[0], _SOURCE_LABELS[source])
				return False
		return super().isValid()

	def onSave(self):
		for source in touchSettings.SOURCES:
			values, _problem = self._read(source)
			for name, value in values.items():
				touchSettings.setSetting(source, name, value)
		touchSettings.reapply()
		conf = config.conf[touchSettings.CONFIG_SECTION]
		for key, checkbox in self._soundCheckboxes.items():
			conf[key] = checkbox.GetValue()
		conf["soundPack"] = audioCues.PACKS[self._packChoice.GetSelection()]
