"""Robotic Hand control panel (native GUI, Qt).

Run with:  python3 hand_control.py

Calibration: click a finger's "Setup" button and use the arrow buttons
to jog the servo to the finger's straight position (Start) and fully
bent position (End). "Reverse" flips direction for servos mounted
backwards. The main slider then sweeps 0-100% between your two points.
Calibration is saved to hand_cal.json and survives restarts.
"""

import json
import os
import random
import sys
import time
from collections import Counter

import serial
import serial.tools.list_ports

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider,
    QPushButton, QFrame, QToolButton, QCheckBox,
)

BAUD = 115200
FINGERS = ["Thumb", "Index", "Middle", "Ring", "Pinky"]
PINS = [3, 5, 6, 9, 10]
_DIR = os.path.dirname(os.path.abspath(__file__))
CAL_FILE = os.path.join(_DIR, "hand_cal.json")
MODEL_FILE = os.path.join(_DIR, "hand_landmarker.task")
SETTINGS_FILE = os.path.join(_DIR, "hand_settings.json")
DEFAULT_SETTINGS = {"emote": True}


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
        return {**DEFAULT_SETTINGS, **data} if isinstance(data, dict) \
            else dict(DEFAULT_SETTINGS)
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)


def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f)

# --- webcam finger tracking -------------------------------------------------
# MediaPipe gives 21 hand landmarks; a finger's curl is measured as the 3D
# angle at its middle joint: straight finger ~ 175 deg, curled into a fist
# ~ 60 deg (thumb bends less, so it gets a narrower range).
#             (joint triplet, open angle, closed angle)
CURL_SPECS = [((1, 2, 4), 165, 115),    # thumb
              ((5, 6, 8), 172, 60),     # index
              ((9, 10, 12), 172, 60),   # middle
              ((13, 14, 16), 172, 60),  # ring
              ((17, 18, 20), 172, 60)]  # pinky

BONES = [(0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8),
         (5, 9), (9, 10), (10, 11), (11, 12), (9, 13), (13, 14), (14, 15),
         (15, 16), (13, 17), (17, 18), (18, 19), (19, 20), (0, 17)]


def _joint_angle(a, b, c):
    """3D angle at point b, in degrees."""
    import math
    v1 = (a.x - b.x, a.y - b.y, a.z - b.z)
    v2 = (c.x - b.x, c.y - b.y, c.z - b.z)
    n1 = math.sqrt(sum(u * u for u in v1))
    n2 = math.sqrt(sum(u * u for u in v2))
    if n1 * n2 == 0:
        return 180.0
    cosv = sum(u * v for u, v in zip(v1, v2)) / (n1 * n2)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosv))))


def finger_curls(lm):
    """Landmarks -> per-finger openness, 0 = open, 100 = fully curled."""
    out = []
    for (a, b, c), open_a, closed_a in CURL_SPECS:
        ang = _joint_angle(lm[a], lm[b], lm[c])
        t = (open_a - ang) / (open_a - closed_a) * 100
        out.append(max(0, min(100, int(round(t)))))
    return out


def classify_gesture(curls):
    """Rock / paper / scissors from finger curls (thumb ignored)."""
    idx, mid, ring, pinky = curls[1:5]
    is_open = [c < 45 for c in (idx, mid, ring, pinky)]
    is_closed = [c > 55 for c in (idx, mid, ring, pinky)]
    if all(is_closed):
        return "rock"
    if all(is_open):
        return "paper"
    if is_open[0] and is_open[1] and is_closed[2] and is_closed[3]:
        return "scissors"
    return None


class TrackerThread(QThread):
    values = Signal(object)   # [thumb..pinky] percents for the sliders
    frame = Signal(QImage)    # camera preview
    failed = Signal(str)
    info = Signal(str)        # status line messages (used by the game)

    def __init__(self):
        super().__init__()
        self._stop = False
        self._smooth = None
        self._last_send = 0.0

    def stop(self):
        self._stop = True

    def draw_text(self, img, text, y=44, scale=1.1):
        cv2 = self._cv2
        cv2.putText(img, text, (12, y), cv2.FONT_HERSHEY_SIMPLEX, scale,
                    (0, 0, 0), 6, cv2.LINE_AA)
        cv2.putText(img, text, (12, y), cv2.FONT_HERSHEY_SIMPLEX, scale,
                    (255, 255, 255), 2, cv2.LINE_AA)

    # hooks: mimic mode drives sliders directly; the game overrides these
    def on_hand(self, lm, curls, img, now):
        if now - self._last_send > 0.07:  # ~14 updates/sec to the servos
            self.values.emit(list(curls))
            self._last_send = now

    def on_no_hand(self, img, now):
        pass

    def run(self):
        try:
            import cv2
            import mediapipe as mp
            from mediapipe.tasks.python import BaseOptions, vision
        except Exception as e:
            self.failed.emit(f"Tracking libraries failed to load: {e}")
            return
        if not os.path.exists(MODEL_FILE):
            self.failed.emit("hand_landmarker.task model file is missing")
            return
        self._cv2 = cv2
        landmarker = vision.HandLandmarker.create_from_options(
            vision.HandLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=MODEL_FILE),
                running_mode=vision.RunningMode.VIDEO,
                num_hands=1))
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            self.failed.emit("Couldn't open the camera — allow camera access "
                             "in System Settings → Privacy & Security → Camera")
            return
        t0 = time.time()
        while not self._stop:
            ok, img = cap.read()
            if not ok:
                self.failed.emit("Camera feed stopped")
                break
            img = cv2.flip(img, 1)  # mirror, so it moves like a mirror image
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            res = landmarker.detect_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb),
                int((time.time() - t0) * 1000))
            now = time.time()
            if res.hand_landmarks:
                lm = res.hand_landmarks[0]
                curls = finger_curls(lm)
                self._smooth = curls if self._smooth is None else [
                    int(round(0.55 * s + 0.45 * c))
                    for s, c in zip(self._smooth, curls)]
                h, w = img.shape[:2]
                pts = [(int(p.x * w), int(p.y * h)) for p in lm]
                for a, b in BONES:
                    cv2.line(img, pts[a], pts[b], (0, 200, 80), 2)
                for p in pts:
                    cv2.circle(img, p, 4, (255, 255, 255), -1)
                self.on_hand(lm, self._smooth, img, now)
            else:
                self.on_no_hand(img, now)
            small = cv2.resize(img, (400, 400 * img.shape[0] // img.shape[1]))
            small = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            qimg = QImage(small.data, small.shape[1], small.shape[0],
                          small.shape[1] * 3, QImage.Format_RGB888).copy()
            self.frame.emit(qimg)
        cap.release()
        landmarker.close()


class GameThread(TrackerThread):
    """Rock-paper-scissors: press Play, robot counts 3-2-1 on its fingers
    (one second per number), throws a random sign, judges, then optionally
    emotes: ILY sign if the robot won, middle finger if it lost."""

    # slider percents [thumb, index, middle, ring, pinky]; 0 = open/up
    POSE = {
        "open":       [0, 0, 0, 0, 0],
        "paper":      [0, 0, 0, 0, 0],
        "rock":       [100, 100, 100, 100, 100],
        "scissors":   [100, 0, 0, 100, 100],
        "3":          [100, 0, 0, 0, 100],      # index+middle+ring up
        "2":          [100, 0, 0, 100, 100],    # index+middle up
        "1":          [100, 0, 100, 100, 100],  # index up
        "mid_finger": [0, 100, 0, 100, 100],    # middle+thumb up (sore loser)
        "love":       [0, 0, 100, 100, 0],      # thumb+index+pinky up (ILY)
    }
    BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
    GREEN, RED, GRAY, BLUE = ((70, 180, 60), (50, 50, 210),
                              (120, 120, 120), (190, 120, 40))

    def __init__(self, emote=True):
        super().__init__()
        self.emote = emote
        self.state = "idle"
        self.play_requested = False
        self.t0 = 0.0             # countdown start
        self.robot = None
        self.samples = []
        self.judge_from = self.judge_until = 0.0
        self.t_show = self.t_emote = self.show_until = 0.0
        self._emoted = False
        self._count_shown = None
        self._cur_pose = None
        self.result = None        # "user", "robot", "draw", None (unreadable)
        self.title = self.subtitle = ""

    def request_play(self):
        self.play_requested = True  # set from the UI thread

    def pose(self, name):
        if name != self._cur_pose:
            self._cur_pose = name
            self.values.emit(list(self.POSE[name]))

    # --- drawing helpers ---
    def draw_center(self, img, text, scale, color=(255, 255, 255),
                    thickness=5, y_off=0):
        cv2 = self._cv2
        h, w = img.shape[:2]
        font = cv2.FONT_HERSHEY_SIMPLEX
        (tw, th), _ = cv2.getTextSize(text, font, scale, thickness)
        x, y = (w - tw) // 2, (h + th) // 2 + y_off
        cv2.putText(img, text, (x, y), font, scale, (0, 0, 0),
                    thickness + 6, cv2.LINE_AA)
        cv2.putText(img, text, (x, y), font, scale, color,
                    thickness, cv2.LINE_AA)

    def draw_banner(self, img, title, subtitle, color):
        cv2 = self._cv2
        h, w = img.shape[:2]
        overlay = img.copy()
        cv2.rectangle(overlay, (0, h // 2 - 80), (w, h // 2 + 58), color, -1)
        cv2.addWeighted(overlay, 0.7, img, 0.3, 0, img)
        self.draw_center(img, title, 1.7, y_off=-18)
        if subtitle:
            self.draw_center(img, subtitle, 0.7, y_off=38, thickness=2)

    # --- round phases ---
    def _throw(self, now):
        self.robot = random.choice(["rock", "paper", "scissors"])
        self.pose(self.robot)
        self.info.emit(f"Robot throws {self.robot}! Show yours!")
        self.state = "judge"
        self.samples = []
        self.judge_from = now + 0.25
        self.judge_until = now + 1.3

    def _finish(self, now):
        user = (Counter(self.samples).most_common(1)[0][0]
                if self.samples else None)
        if user is None:
            self.result = None
            self.title, self.subtitle = "NO CONTEST", "Couldn't read your hand"
            self.info.emit("Couldn't read your throw — no contest")
        elif user == self.robot:
            self.result = "draw"
            self.title = "DRAW"
            self.subtitle = f"Both threw {user}"
            self.info.emit(f"Both threw {user} — draw!")
        elif self.BEATS[user] == self.robot:
            self.result = "user"
            self.title = "YOU WIN!"
            self.subtitle = f"Your {user} beats {self.robot}"
            self.info.emit(f"Your {user} beats {self.robot} — you win!")
        else:
            self.result = "robot"
            self.title = "ROBOT WINS!"
            self.subtitle = f"Robot's {self.robot} beats your {user}"
            self.info.emit(f"Robot's {self.robot} beats your {user}!")
        self.state = "show"
        self.t_show = now
        self.t_emote = now + 1.0
        self._emoted = False
        emoting = self.emote and self.result in ("user", "robot")
        self.show_until = now + (3.5 if emoting else 2.5)

    def _machine(self, gesture, img, now, visible):
        st = self.state
        if st == "idle":
            self.pose("open")
            self.draw_text(img, "Press Play Round to challenge me")
            if self.play_requested:
                self.play_requested = False
                self.state = "count"
                self.t0 = now
                self._count_shown = None
        elif st == "count":
            k = int(now - self.t0)  # 0, 1, 2 -> counts 3, 2, 1
            if k <= 2:
                n = 3 - k
                if self._count_shown != n:
                    self._count_shown = n
                    self.pose(str(n))
                    self.info.emit(f"{n}…")
                self.draw_center(img, str(n), 4.0, thickness=10)
            else:
                self._throw(now)
        elif st == "judge":
            self.draw_text(img, f"Robot: {self.robot.upper()}")
            if now >= self.judge_from and gesture:
                self.samples.append(gesture)
            if now >= self.judge_until:
                self._finish(now)
        elif st == "show":
            color = {"user": self.GREEN, "robot": self.RED,
                     "draw": self.GRAY, None: self.BLUE}[self.result]
            self.draw_banner(img, self.title, self.subtitle, color)
            if (self.emote and not self._emoted and now >= self.t_emote
                    and self.result in ("user", "robot")):
                self._emoted = True
                if self.result == "robot":
                    self.pose("love")       # gracious winner: I-love-you sign
                    self.info.emit("Robot wins — but it loves you 🤟")
                else:
                    self.pose("mid_finger")  # sore loser
                    self.info.emit("Robot lost — and it's not happy about it 🖕")
            if now >= self.show_until:
                self.state = "idle"
                self.pose("open")
                self.info.emit("Press Play Round for another game")

    def on_hand(self, lm, curls, img, now):
        self._machine(classify_gesture(curls), img, now, visible=True)

    def on_no_hand(self, img, now):
        self._machine(None, img, now, visible=False)


class Hand:
    """Serial link + calibration state."""

    def __init__(self):
        self.ser = None
        self.last_connect_try = 0.0
        # start/end are real servo degrees; start > end means reversed
        self.cal = [{"start": 0, "end": 180} for _ in FINGERS]
        self.last_actual = [90] * len(FINGERS)  # Arduino boots at 90
        self.load_cal()

    def load_cal(self):
        try:
            with open(CAL_FILE) as f:
                data = json.load(f)
            if (isinstance(data, list) and len(data) == len(FINGERS)
                    and all("start" in d and "end" in d for d in data)):
                self.cal = [{"start": max(0, min(180, int(d["start"]))),
                             "end": max(0, min(180, int(d["end"])))} for d in data]
        except (OSError, ValueError, TypeError):
            pass

    def save_cal(self):
        with open(CAL_FILE, "w") as f:
            json.dump(self.cal, f)

    @staticmethod
    def find_port():
        for p in serial.tools.list_ports.comports():
            if any(tag in p.device for tag in ("usbmodem", "usbserial",
                                               "wchusbserial")):
                return p.device
        return None

    def connect(self):
        # don't hammer reconnect attempts (each one blocks ~2.5s)
        if time.time() - self.last_connect_try < 3:
            return False
        self.last_connect_try = time.time()
        port = self.find_port()
        if not port:
            return False
        try:
            self.ser = serial.Serial(port, BAUD, timeout=1)
            time.sleep(2.5)  # opening the port resets the Arduino; wait for boot
            self.last_actual = [90] * len(FINGERS)  # reset re-centers servos
            return True
        except serial.SerialException:
            self.ser = None
            return False

    def send_raw(self, idx, actual):
        actual = max(0, min(180, int(round(actual))))
        for _ in range(2):  # one retry: reconnect if the write fails
            if self.ser is None and not self.connect():
                continue
            try:
                self.ser.write(f"{idx}:{actual}\n".encode())
                self.last_actual[idx] = actual
                return actual
            except serial.SerialException:
                self.ser = None
        return None

    def send_percent(self, idx, t):
        c = self.cal[idx]
        t = max(0, min(100, t))
        return self.send_raw(idx, c["start"] + t / 100.0 * (c["end"] - c["start"]))

    def initial_percent(self, idx):
        c = self.cal[idx]
        span = c["end"] - c["start"]
        if span == 0:
            return 50
        t = (self.last_actual[idx] - c["start"]) / span * 100
        return max(0, min(100, int(round(t))))


class FingerRow(QFrame):
    def __init__(self, idx, hand, status_cb):
        super().__init__()
        self.idx = idx
        self.hand = hand
        self.status = status_cb
        self.setFrameStyle(QFrame.StyledPanel)

        outer = QVBoxLayout(self)

        # --- main row: name, slider, labels, setup toggle ---
        row = QHBoxLayout()
        self.name_label = QLabel(f"{FINGERS[idx]}  (pin {PINS[idx]})")
        self.name_label.setFixedWidth(120)
        row.addWidget(self.name_label)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(hand.initial_percent(idx))
        self.slider.valueChanged.connect(self.on_slider)
        row.addWidget(self.slider, stretch=1)

        self.pct_label = QLabel()
        self.pct_label.setFixedWidth(44)
        self.pct_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.pct_label)

        self.real_label = QLabel()
        self.real_label.setFixedWidth(44)
        self.real_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.real_label.setStyleSheet("color: gray;")
        row.addWidget(self.real_label)

        self.setup_btn = QToolButton()
        self.setup_btn.setText("Setup")
        self.setup_btn.setCheckable(True)
        self.setup_btn.toggled.connect(self.toggle_setup)
        row.addWidget(self.setup_btn)
        outer.addLayout(row)

        # --- setup panel (hidden by default) ---
        self.panel = QWidget()
        panel_lay = QVBoxLayout(self.panel)
        panel_lay.setContentsMargins(20, 4, 4, 4)

        self.point_labels = {}
        for which, title in (("start", "Start"), ("end", "End")):
            prow = QHBoxLayout()
            lab = QLabel(title)
            lab.setFixedWidth(44)
            prow.addWidget(lab)
            for delta in (-10, -1):
                b = QPushButton(f"{delta:+d}")
                b.setFixedWidth(44)
                b.clicked.connect(lambda _=False, w=which, d=delta: self.jog(w, d))
                prow.addWidget(b)
            deg = QPushButton()
            deg.setFlat(True)
            deg.setFixedWidth(52)
            deg.setToolTip("Click to move the servo to this point")
            deg.setStyleSheet("font-weight: bold; text-decoration: underline;")
            deg.clicked.connect(lambda _=False, w=which: self.goto_point(w))
            self.point_labels[which] = deg
            prow.addWidget(deg)
            for delta in (1, 10):
                b = QPushButton(f"{delta:+d}")
                b.setFixedWidth(44)
                b.clicked.connect(lambda _=False, w=which, d=delta: self.jog(w, d))
                prow.addWidget(b)
            if which == "end":
                rev = QPushButton("Reverse")
                rev.setToolTip("Swap start and end (for servos mounted backwards)")
                rev.clicked.connect(self.reverse)
                prow.addWidget(rev)
            prow.addStretch()
            panel_lay.addLayout(prow)

        hint = QLabel("Arrows move the servo live. Click a degree number to jump "
                      "the servo to that point.")
        hint.setStyleSheet("color: gray; font-size: 11px;")
        panel_lay.addWidget(hint)
        self.panel.setVisible(False)
        outer.addWidget(self.panel)

        self.refresh_labels()

    # --- helpers ---
    @property
    def cal(self):
        return self.hand.cal[self.idx]

    def real_angle(self, t):
        c = self.cal
        return int(round(c["start"] + t / 100.0 * (c["end"] - c["start"])))

    def refresh_labels(self):
        t = self.slider.value()
        self.pct_label.setText(f"{t}%")
        self.real_label.setText(f"{self.real_angle(t)}°")
        for which in ("start", "end"):
            self.point_labels[which].setText(f"{self.cal[which]}°")
        if self.cal["start"] == self.cal["end"]:
            self.name_label.setStyleSheet("font-weight: bold; color: #b25000;")
            self.name_label.setToolTip("Not calibrated — use Setup")
        else:
            self.name_label.setStyleSheet("font-weight: bold;")
            self.name_label.setToolTip("")

    def set_slider_silent(self, value):
        self.slider.blockSignals(True)
        self.slider.setValue(value)
        self.slider.blockSignals(False)
        self.refresh_labels()

    # --- actions ---
    def toggle_setup(self, checked):
        self.panel.setVisible(checked)
        self.window().adjustSize()

    def on_slider(self, t):
        self.refresh_labels()
        c = self.cal
        if c["start"] == c["end"]:
            self.hand.send_percent(self.idx, t)  # wake servo to the neutral point
            self.status(False, f"{FINGERS[self.idx]} isn't calibrated yet — "
                        "click its Setup button and set Start and End first")
            return
        ok = self.hand.send_percent(self.idx, t) is not None
        self.status(ok, "Connected" if ok
                    else "Arduino not found — is it plugged in?")

    def jog(self, which, delta):
        v = max(0, min(180, self.cal[which] + delta))
        self.cal[which] = v
        self.hand.save_cal()
        ok = self.hand.send_raw(self.idx, v) is not None
        # servo is now physically at that point; snap slider to match
        self.set_slider_silent(0 if which == "start" else 100)
        self.status(ok, f"{FINGERS[self.idx]} {which} point: {v}° (saved)"
                    if ok else "Arduino not found — is it plugged in?")

    def goto_point(self, which):
        ok = self.hand.send_raw(self.idx, self.cal[which]) is not None
        self.set_slider_silent(0 if which == "start" else 100)
        self.status(ok, f"{FINGERS[self.idx]} at {which} point" if ok
                    else "Arduino not found — is it plugged in?")

    def reverse(self):
        c = self.cal
        c["start"], c["end"] = c["end"], c["start"]
        self.hand.save_cal()
        # same physical position now corresponds to the mirrored slider value
        self.set_slider_silent(100 - self.slider.value())
        self.status(True, f"{FINGERS[self.idx]} direction reversed (saved)")


class MainWindow(QWidget):
    def __init__(self, hand):
        super().__init__()
        self.hand = hand
        self.setWindowTitle("Robotic Hand Control")

        lay = QVBoxLayout(self)

        self.rows = [FingerRow(i, hand, self.set_status)
                     for i in range(len(FINGERS))]
        for r in self.rows:
            lay.addWidget(r)

        presets = QHBoxLayout()
        for label, t in (("All → start", 0), ("All → middle", 50),
                         ("All → end", 100)):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, v=t: self.set_all(v))
            presets.addWidget(b)
        lay.addLayout(presets)

        self.tracker = None
        self.game = None
        cam_row = QHBoxLayout()
        self.track_btn = QPushButton("🎥  Start hand tracking")
        self.track_btn.setCheckable(True)
        self.track_btn.toggled.connect(self.toggle_tracking)
        cam_row.addWidget(self.track_btn)
        self.game_btn = QPushButton("✊✋✌️  Rock Paper Scissors")
        self.game_btn.setCheckable(True)
        self.game_btn.toggled.connect(self.toggle_game)
        cam_row.addWidget(self.game_btn)
        self.play_btn = QPushButton("🎲  Play round")
        self.play_btn.setEnabled(False)
        self.play_btn.clicked.connect(self.play_round)
        cam_row.addWidget(self.play_btn)
        self.settings_btn = QToolButton()
        self.settings_btn.setText("⚙️")
        self.settings_btn.setCheckable(True)
        self.settings_btn.setToolTip("Settings")
        self.settings_btn.toggled.connect(self.toggle_settings)
        cam_row.addWidget(self.settings_btn)
        lay.addLayout(cam_row)

        # --- settings panel (hidden by default) ---
        self.settings = load_settings()
        self.settings_panel = QWidget()
        sp = QHBoxLayout(self.settings_panel)
        sp.setContentsMargins(8, 0, 8, 0)
        self.emote_check = QCheckBox(
            "Emote after a game  (🤟 when the robot wins, 🖕 when it loses)")
        self.emote_check.setChecked(bool(self.settings["emote"]))
        self.emote_check.toggled.connect(self.emote_changed)
        sp.addWidget(self.emote_check)
        sp.addStretch()
        self.settings_panel.setVisible(False)
        lay.addWidget(self.settings_panel)

        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setVisible(False)
        lay.addWidget(self.preview)

        self.status_label = QLabel("Ready")
        lay.addWidget(self.status_label)

    # --- hand tracking ---
    def ensure_camera_access(self):
        """Open the camera once on the main thread: macOS only shows the
        permission prompt from here, never from a background thread."""
        try:
            import cv2
        except Exception:
            return False
        cap = cv2.VideoCapture(0)
        ok = cap.isOpened()
        if ok:
            ok = cap.read()[0]
        cap.release()
        return ok

    def toggle_tracking(self, on):
        if on:
            if self.game_btn.isChecked():
                self.game_btn.setChecked(False)  # stops the game first
            self.set_status(True, "Opening camera…")
            QApplication.processEvents()
            if not self.ensure_camera_access():
                self.set_status(False,
                                "Camera not available — click Allow on the "
                                "permission popup, or enable it in System "
                                "Settings → Privacy & Security → Camera, "
                                "then try again")
                self.track_btn.setChecked(False)
                return
            self.track_btn.setText("🛑  Stop hand tracking")
            self.set_status(True, "Starting camera… show your hand")
            self.tracker = TrackerThread()
            self.tracker.values.connect(self.apply_curls)
            self.tracker.frame.connect(self.show_frame)
            self.tracker.failed.connect(self.tracking_failed)
            self.tracker.start()
            self.preview.setVisible(True)
        else:
            self.track_btn.setText("🎥  Start hand tracking")
            if self.tracker:
                self.tracker.stop()
                self.tracker.wait(3000)
                self.tracker = None
            if not self.game_btn.isChecked():
                self.preview.setVisible(False)
                self.preview.clear()
                self.adjustSize()
            self.set_status(True, "Hand tracking off")

    def toggle_game(self, on):
        if on:
            if self.track_btn.isChecked():
                self.track_btn.setChecked(False)  # stops mimic mode first
            self.set_status(True, "Opening camera…")
            QApplication.processEvents()
            if not self.ensure_camera_access():
                self.set_status(False,
                                "Camera not available — allow camera access "
                                "in System Settings → Privacy & Security → "
                                "Camera, then try again")
                self.game_btn.setChecked(False)
                return
            self.game_btn.setText("🛑  Stop game")
            self.game = GameThread(bool(self.settings["emote"]))
            self.game.values.connect(self.apply_curls)
            self.game.frame.connect(self.show_frame)
            self.game.failed.connect(self.game_failed)
            self.game.info.connect(lambda m: self.set_status(True, m))
            self.game.start()
            self.preview.setVisible(True)
            self.play_btn.setEnabled(True)
            self.set_status(True, "Press Play Round to start a game!")
        else:
            self.game_btn.setText("✊✋✌️  Rock Paper Scissors")
            self.play_btn.setEnabled(False)
            if self.game:
                self.game.stop()
                self.game.wait(3000)
                self.game = None
            self.set_all(0)  # relax to open
            if not self.track_btn.isChecked():
                self.preview.setVisible(False)
                self.preview.clear()
                self.adjustSize()
            self.set_status(True, "Game over — hand relaxed to open")

    def game_failed(self, msg):
        self.set_status(False, msg)
        self.game_btn.setChecked(False)

    # --- settings ---
    def toggle_settings(self, on):
        self.settings_panel.setVisible(on)
        if not on:
            self.adjustSize()

    def emote_changed(self, on):
        self.settings["emote"] = on
        save_settings(self.settings)
        if self.game:
            self.game.emote = on  # applies live

    def play_round(self):
        if self.game:
            self.game.request_play()

    def apply_curls(self, curls):
        for row, t in zip(self.rows, curls):
            row.slider.setValue(t)  # triggers the serial send

    def show_frame(self, qimg):
        self.preview.setPixmap(QPixmap.fromImage(qimg))

    def tracking_failed(self, msg):
        self.set_status(False, msg)
        self.track_btn.setChecked(False)

    def closeEvent(self, event):
        for t in (self.tracker, self.game):
            if t:
                t.stop()
                t.wait(3000)
        event.accept()

    def set_status(self, ok, msg):
        self.status_label.setText(msg)
        self.status_label.setStyleSheet(
            "" if ok else "color: #c00; font-weight: bold;")

    def set_all(self, t):
        for r in self.rows:
            r.slider.setValue(t)  # triggers on_slider -> sends


if __name__ == "__main__":
    hand = Hand()
    print("Calibration:",
          ", ".join(f"{FINGERS[i]} {hand.cal[i]['start']}→{hand.cal[i]['end']}°"
                    for i in range(len(FINGERS))))
    print("Connecting to Arduino...")
    if hand.connect():
        print(f"Connected on {hand.ser.port}")
    else:
        print("Arduino not found - will keep trying when you move a slider.")

    app = QApplication(sys.argv)
    win = MainWindow(hand)
    if hand.ser:
        win.set_status(True, f"Connected: {hand.ser.port}")
    else:
        win.set_status(False, "Arduino not found — is it plugged in?")
    win.resize(640, win.sizeHint().height())
    win.show()
    sys.exit(app.exec())
