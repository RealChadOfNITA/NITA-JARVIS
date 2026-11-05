"""
NITA JARVIS — Supabase-connected Smart Security Monitor

Windows environment variables required (set once in PowerShell, then reopen it):
    setx NITA_SUPABASE_URL "https://YOUR_PROJECT.supabase.co"
    setx NITA_SUPABASE_PUBLISHABLE_KEY "YOUR_PUBLISHABLE_KEY"

Requires: pip install opencv-python requests customtkinter pillow
Telegram bot credentials stay on the Supabase server; never add them to this file.
"""
import io
import math
import os
import queue
import re
import threading
import time
import ctypes
import tkinter as tk
import webbrowser

try:
    import keyring  # Windows Credential Manager backend when available
except ImportError:
    keyring = None
from datetime import datetime
from pathlib import Path

import cv2
import requests
import customtkinter as ctk
from PIL import Image, ImageTk

try:
    import winsound
except ImportError:  # not on Windows
    winsound = None


# -------------------- SETTINGS --------------------
ARM_DELAY_SECONDS = 20            # countdown after pressing Start monitoring
PHOTO_COUNT = 5
PHOTO_INTERVAL_SECONDS = 1.0
QUIET_SECONDS = 3.0               # minimum gap between two alerts
MOTION_MIN_AREA = 3000            # smallest moving blob (pixels) that counts as motion
LIGHT_CHANGE_FRACTION = 0.6       # if >60% of the frame "moves" it's a lighting change, ignore
WARMUP_FRAMES = 30                # frames the background model learns before detecting
MAX_QUEUED_PHOTOS = 50            # oldest photos are dropped beyond this (limits RAM use offline)
TELEGRAM_RETRY_START_SECONDS = 5
TELEGRAM_RETRY_MAX_SECONDS = 60
TAMPER_CONFIRM_SECONDS = 2.0
TAMPER_DIFF_THRESHOLD = 0.28
TAMPER_TEXTURE_THRESHOLD = 18.0
TAMPER_BRIGHTNESS_LOW = 22.0
SUPABASE_URL = os.environ.get("NITA_SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.environ.get("NITA_SUPABASE_PUBLISHABLE_KEY", "").strip()

# Windows: keep the system awake while monitoring (screen may still turn off).
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

# -------------------- THEME --------------------
BG = "#070b14"
SIDEBAR = "#0a1020"
PANEL = "#0f172a"
PANEL_ALT = "#131d35"
BORDER = "#1e2b4a"
TEXT = "#e6edf7"
MUTED = "#7b8bab"
CYAN = "#22d3ee"
BLUE = "#3b82f6"
GREEN = "#34d399"
RED = "#f87171"
AMBER = "#fbbf24"
VIOLET = "#a78bfa"

# OpenCV uses BGR
BGR_CYAN = (238, 211, 34)
BGR_AMBER = (36, 191, 251)
BGR_GREEN = (153, 211, 52)
BGR_RED = (113, 113, 248)

FONT = "Segoe UI"
MONO = "Consolas"


# -------------------- HELPERS --------------------
def timestamp():
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S UTC%z")


def redact(text):
    """Remove anything that looks like a Telegram bot token from log text."""
    text = re.sub(r"bot\d+:[\w-]+", "bot***", str(text))
    return text


def encode_jpeg(frame):
    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        return None
    return buffer.tobytes()


def beep(freq=1000, ms=150):
    if winsound is None:
        return
    def _run():
        try:
            winsound.Beep(freq, ms)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def put_text_outlined(img, text, org, scale, color, thickness=1):
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thickness + 3, cv2.LINE_AA)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)


def draw_hud(frame, armed, remaining, alert_active, preview=False):
    """Futuristic overlay: corner brackets, status, timestamp, arming countdown, alert banner."""
    h, w = frame.shape[:2]
    color = BGR_GREEN if armed else (BGR_CYAN if preview else BGR_AMBER)
    inset, length = 14, 34
    for (cx, cy, dx, dy) in ((inset, inset, 1, 1), (w - inset, inset, -1, 1),
                             (inset, h - inset, 1, -1), (w - inset, h - inset, -1, -1)):
        cv2.line(frame, (cx, cy), (cx + dx * length, cy), color, 2, cv2.LINE_AA)
        cv2.line(frame, (cx, cy), (cx, cy + dy * length), color, 2, cv2.LINE_AA)

    status = "ARMED" if armed else ("PREVIEW" if preview else "ARMING")
    put_text_outlined(frame, f"● {status}", (28, 48 if alert_active else 40), 0.7, color, 2)
    put_text_outlined(frame, datetime.now().strftime("%d %b %Y  %H:%M:%S"),
                      (28, h - 26), 0.55, (230, 237, 247), 1)

    if preview:
        put_text_outlined(frame, "Preview only - not monitoring. Press Start monitoring to arm.",
                          (28, 70), 0.5, (230, 237, 247), 1)

    if not armed and not preview:
        text = str(max(1, math.ceil(remaining)))
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_DUPLEX, 4.0, 6)
        put_text_outlined(frame, text, ((w - tw) // 2, (h + th) // 2), 4.0, BGR_AMBER, 6)
        sub = "Leave the room - monitoring starts soon"
        (sw, _), _ = cv2.getTextSize(sub, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
        put_text_outlined(frame, sub, ((w - sw) // 2, (h + th) // 2 + 44), 0.7, (230, 237, 247), 2)

    if alert_active:
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, 0), (w, 40), (40, 40, 200), -1)
        cv2.addWeighted(overlay, 0.75, frame, 0.25, 0, frame)
        label = "MOTION DETECTED"
        (lw, _), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.85, 2)
        cv2.putText(frame, label, ((w - lw) // 2, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.85,
                    (255, 255, 255), 2, cv2.LINE_AA)


# -------------------- ROI SELECTOR (replaces cv2.selectROI) --------------------
class RoiSelector(ctk.CTkToplevel):
    MAX_W, MAX_H = 900, 600

    def __init__(self, parent, frame, on_done):
        super().__init__(parent)
        self.title("Select doorway")
        self.configure(fg_color=BG)
        self.resizable(False, False)
        self.on_done = on_done

        fh, fw = frame.shape[:2]
        self.scale = min(self.MAX_W / fw, self.MAX_H / fh, 1.0)
        self.dw, self.dh = int(fw * self.scale), int(fh * self.scale)
        rgb = cv2.cvtColor(cv2.resize(frame, (self.dw, self.dh)), cv2.COLOR_BGR2RGB)
        self.photo = ImageTk.PhotoImage(Image.fromarray(rgb))

        ctk.CTkLabel(
            self, text="Drag a rectangle around the doorway",
            font=ctk.CTkFont(family=FONT, size=16, weight="bold"), text_color=TEXT
        ).pack(pady=(16, 2))
        ctk.CTkLabel(
            self, text="Motion is only checked inside this box. Confirm when you're happy.",
            font=ctk.CTkFont(family=FONT, size=12), text_color=MUTED
        ).pack(pady=(0, 10))

        self.canvas = tk.Canvas(self, width=self.dw, height=self.dh, highlightthickness=1,
                                highlightbackground=BORDER, bg="#000000", cursor="crosshair")
        self.canvas.pack(padx=18)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")

        self.rect_id = None
        self.start = None
        self.box = None
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(pady=14)
        ctk.CTkButton(row, text="Confirm", width=120, fg_color=CYAN, hover_color="#0891b2",
                      text_color="#04121c", font=ctk.CTkFont(family=FONT, weight="bold"),
                      command=self._confirm).pack(side="left", padx=6)
        ctk.CTkButton(row, text="Use full frame", width=130, fg_color=PANEL_ALT, hover_color=BORDER,
                      text_color=TEXT, command=self._full_frame).pack(side="left", padx=6)
        ctk.CTkButton(row, text="Cancel", width=100, fg_color="transparent", border_width=1,
                      border_color=BORDER, hover_color=PANEL_ALT, text_color=MUTED,
                      command=self.destroy).pack(side="left", padx=6)

        self.after(200, self._focus)

    def _focus(self):
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
        except tk.TclError:
            pass

    def _clamp(self, x, y):
        return max(0, min(x, self.dw)), max(0, min(y, self.dh))

    def _press(self, event):
        self.start = self._clamp(event.x, event.y)
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(*self.start, *self.start, outline=CYAN, width=2, dash=(6, 3))

    def _drag(self, event):
        if self.start and self.rect_id:
            x, y = self._clamp(event.x, event.y)
            self.canvas.coords(self.rect_id, self.start[0], self.start[1], x, y)

    def _release(self, event):
        if not self.start:
            return
        x, y = self._clamp(event.x, event.y)
        x0, x1 = sorted((self.start[0], x))
        y0, y1 = sorted((self.start[1], y))
        self.box = (x0, y0, x1 - x0, y1 - y0) if (x1 - x0) >= 10 and (y1 - y0) >= 10 else None

    def _confirm(self):
        if not self.box:
            return
        x, y, w, h = self.box
        s = self.scale
        self.on_done((int(x / s), int(y / s), int(w / s), int(h / s)))
        self.destroy()

    def _full_frame(self):
        self.on_done(None)
        self.destroy()


# -------------------- MAIN APP --------------------
class NitaJarvisApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("NITA JARVIS — Smart Security")
        self.geometry("1280x860")
        self.minsize(1060, 780)
        self.configure(fg_color=BG)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.shutdown_event = threading.Event()
        self.stop_camera_event = threading.Event()
        self.camera_thread = None

        self.latest_frame = None      # frame with overlays (for preview)
        self.latest_clean = None      # raw frame (for doorway selection)
        self.frame_counter = 0
        self._shown_counter = -1
        self.frame_lock = threading.Lock()
        self.roi = None
        self.roi_lock = threading.Lock()

        self.photo_queue = queue.Queue(maxsize=MAX_QUEUED_PHOTOS)
        self.ui_queue = queue.Queue()
        self.activity_lines = queue.Queue()

        self.supabase_access_token = ""
        self.supabase_refresh_token = ""
        self.supabase_user_email = ""
        self.supabase_authenticated = False
        self.telegram_linked = False
        self.telegram_link_polling = False
        self.telegram_poll_generation = 0
        self.auth_lock = threading.Lock()

        self.motion_events = 0
        self.last_motion_time = "—"

        # Arming timer state
        self.monitoring_active = False
        self.arm_deadline = None      # time.monotonic() value
        self._armed_ui = False
        self._last_beep_sec = None
        self.tamper_enabled = True
        self.siren_stop_event = threading.Event()
        self.siren_thread = None
        self.tamper_latched = False
        self._last_tamper_alert = 0.0

        self.hostel_name = "RNT hostel"
        self.block_number = ""
        self.room_number = ""

        self.cards = {}
        self.badge_state = "offline"
        self._pulse_on = False

        self.blank_image = ctk.CTkImage(Image.new("RGBA", (2, 2), (0, 0, 0, 0)), size=(2, 2))

        self._build_ui()
        self._set_card("camera", "Stopped", MUTED)
        self._set_card("telegram", "Not connected", MUTED)
        self._set_card("motion", "Waiting", MUTED)

        self.sender_thread = threading.Thread(target=self._telegram_sender_loop, daemon=True)
        self.sender_thread.start()

        self.after(50, self._drain_ui)
        self.after(33, self._refresh_preview)
        self.after(200, self._drain_activity)
        self.after(100, self._tick_countdown)
        self.after(600, self._pulse)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._log("Welcome to NITA Jarvis. Sign in once; this app can restore your session using Windows Credential Manager when keyring is installed.", "info")
        self.after(700, self._try_restore_session)

    # -------------------- thread-safe UI calls --------------------
    def _ui(self, fn):
        """Queue a function to run on the Tk main thread (safe from any thread)."""
        if not self.shutdown_event.is_set():
            self.ui_queue.put(fn)

    def _drain_ui(self):
        if self.shutdown_event.is_set():
            return
        while True:
            try:
                fn = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as exc:
                self._log(f"UI error: {exc}", "err")
        self.after(50, self._drain_ui)

    # -------------------- UI --------------------
    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ---------- sidebar ----------
        self.sidebar = ctk.CTkFrame(self, width=240, corner_radius=0, fg_color=SIDEBAR,
                                    border_width=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)

        logo = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        logo.pack(fill="x", padx=20, pady=(28, 2))
        ctk.CTkLabel(logo, text="◉", text_color=CYAN,
                     font=ctk.CTkFont(family=FONT, size=30, weight="bold")).pack(side="left")
        ctk.CTkLabel(logo, text=" NITA JARVIS", text_color=TEXT,
                     font=ctk.CTkFont(family=FONT, size=21, weight="bold")).pack(side="left", pady=(4, 0))
        ctk.CTkLabel(self.sidebar, text="SMART SECURITY MONITOR", text_color=MUTED,
                     font=ctk.CTkFont(family=FONT, size=10, weight="bold")).pack(anchor="w", padx=22, pady=(0, 26))

        ctk.CTkLabel(
            self.sidebar, text="▣   Dashboard", height=42, anchor="w",
            fg_color=PANEL_ALT, corner_radius=10, text_color=CYAN,
            font=ctk.CTkFont(family=FONT, size=14, weight="bold")
        ).pack(fill="x", padx=14, pady=4)

        self._sidebar_heading("MONITORING")

        self.start_button = ctk.CTkButton(
            self.sidebar, text="▶   Start monitoring", height=44, corner_radius=10,
            fg_color=CYAN, hover_color="#0891b2", text_color="#04121c",
            text_color_disabled="#3b4a68", font=ctk.CTkFont(family=FONT, size=14, weight="bold"),
            command=self.start_monitoring
        )
        self.start_button.pack(fill="x", padx=14, pady=5)

        self.stop_button = ctk.CTkButton(
            self.sidebar, text="■   Stop monitoring", height=44, corner_radius=10,
            fg_color=PANEL_ALT, hover_color="#7f1d1d", text_color=RED,
            text_color_disabled="#3b4a68", font=ctk.CTkFont(family=FONT, size=14, weight="bold"),
            command=self.stop_monitoring, state="disabled"
        )
        self.stop_button.pack(fill="x", padx=14, pady=5)

        self.roi_button = ctk.CTkButton(
            self.sidebar, text="⌖   Select doorway", height=40, corner_radius=10,
            fg_color="transparent", hover_color=PANEL_ALT, text_color=TEXT,
            border_width=1, border_color=BORDER, font=ctk.CTkFont(family=FONT, size=13),
            command=self.select_doorway
        )
        self.roi_button.pack(fill="x", padx=14, pady=5)

        self._sidebar_heading("STUDENT LOGIN")
        self.email_entry = ctk.CTkEntry(self.sidebar, placeholder_text="Email address", height=34)
        self.email_entry.pack(fill="x", padx=14, pady=4)
        self.password_entry = ctk.CTkEntry(self.sidebar, placeholder_text="Password", show="●", height=34)
        self.password_entry.pack(fill="x", padx=14, pady=4)
        self.login_button = ctk.CTkButton(
            self.sidebar, text="Log in", height=36, corner_radius=9,
            fg_color=BLUE, hover_color="#2563eb", text_color="white",
            font=ctk.CTkFont(family=FONT, size=12, weight="bold"), command=self.handle_login)
        self.login_button.pack(fill="x", padx=14, pady=4)
        self._sidebar_heading("TELEGRAM ALERTS")
        ctk.CTkLabel(self.sidebar,
            text="Log in first. Then link your Telegram account using the button below.",
            justify="left", wraplength=196, text_color=MUTED,
            font=ctk.CTkFont(family=FONT, size=11)).pack(anchor="w", padx=20, pady=(0, 8))
        self.connect_button = ctk.CTkButton(
            self.sidebar, text="Link Telegram", height=36, corner_radius=9,
            fg_color=PANEL_ALT, hover_color=BORDER, text_color=TEXT,
            font=ctk.CTkFont(family=FONT, size=12), command=self.create_telegram_link)
        self.connect_button.pack(fill="x", padx=14, pady=4)
        self.status_button = ctk.CTkButton(
            self.sidebar, text="Check Telegram status", height=34, corner_radius=9,
            fg_color="transparent", hover_color=PANEL_ALT, text_color=CYAN,
            border_width=1, border_color=BORDER,
            font=ctk.CTkFont(family=FONT, size=11), command=self.check_telegram_link)
        self.status_button.pack(fill="x", padx=14, pady=4)

        ctk.CTkLabel(self.sidebar, text=f"Arming delay: {ARM_DELAY_SECONDS}s", text_color=MUTED,
                     font=ctk.CTkFont(family=FONT, size=11)).pack(side="bottom", anchor="w", padx=20, pady=18)

        # ---------- main ----------
        self.main = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self.main.grid(row=0, column=1, sticky="nsew")
        self.main.grid_columnconfigure(0, weight=1)
        self.main.grid_rowconfigure(3, weight=1)

        header = ctk.CTkFrame(self.main, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=28, pady=(24, 10))
        header.grid_columnconfigure(0, weight=1)
        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(title_box, text="Command center", text_color=TEXT,
                     font=ctk.CTkFont(family=FONT, size=28, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(title_box, text="Motion-triggered photo alerts straight to your Telegram.",
                     text_color=MUTED, font=ctk.CTkFont(family=FONT, size=12)).pack(anchor="w")
        self.clock_label = ctk.CTkLabel(header, text="", text_color=CYAN,
                                        font=ctk.CTkFont(family=MONO, size=14, weight="bold"))
        self.clock_label.grid(row=0, column=1, sticky="e")
        self._update_clock()

        # Large, quick-action control: normal daily workflow is one click to arm.
        self.quick_arm_button = ctk.CTkButton(
            header, text="⚡  ARM MY ROOM", height=46, corner_radius=12,
            fg_color=CYAN, hover_color="#0891b2", text_color="#04121c",
            font=ctk.CTkFont(family=FONT, size=16, weight="bold"),
            command=self.start_monitoring
        )
        self.quick_arm_button.grid(row=0, column=2, sticky="e", padx=(16, 0))

        # location panel
        location_panel = ctk.CTkFrame(self.main, fg_color=PANEL, corner_radius=14,
                                      border_width=1, border_color=BORDER)
        location_panel.grid(row=1, column=0, sticky="ew", padx=22, pady=(0, 6))
        location_panel.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(location_panel, text="⌂  HOSTEL LOCATION", text_color=MUTED,
                     font=ctk.CTkFont(family=FONT, size=10, weight="bold")
                     ).grid(row=0, column=0, sticky="w", padx=16, pady=(10, 4), columnspan=4)

        self.hostel_picker = ctk.CTkSegmentedButton(
            location_panel, values=["RNT hostel", "Aryabhatta hostel", "Gargi hostel"],
            selected_color=BLUE, selected_hover_color="#2563eb",
            unselected_color=PANEL_ALT, unselected_hover_color=BORDER,
            text_color=TEXT, fg_color=PANEL_ALT, font=ctk.CTkFont(family=FONT, size=12),
            command=self._hostel_selection_changed
        )
        self.hostel_picker.set("RNT hostel")
        self.hostel_picker.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 10))

        entry_style = dict(fg_color="#0b1222", border_color=BORDER, text_color=TEXT,
                           placeholder_text_color=MUTED, corner_radius=8, height=32,
                           font=ctk.CTkFont(family=FONT, size=12))
        self.block_entry = ctk.CTkEntry(location_panel, placeholder_text="Block (e.g. A or 2)",
                                        width=170, **entry_style)
        self.block_entry.grid(row=1, column=1, padx=(0, 8), pady=(0, 10))
        self.room_entry = ctk.CTkEntry(location_panel, placeholder_text="Room (e.g. 204)",
                                       width=170, **entry_style)
        self.room_entry.grid(row=1, column=2, padx=(0, 8), pady=(0, 10))
        self.apply_location_button = ctk.CTkButton(
            location_panel, text="Apply", width=100, height=32, corner_radius=8,
            fg_color=CYAN, hover_color="#0891b2", text_color="#04121c",
            font=ctk.CTkFont(family=FONT, size=12, weight="bold"), command=self._apply_location
        )
        self.apply_location_button.grid(row=1, column=3, padx=(0, 14), pady=(0, 10))

        self.location_status = ctk.CTkLabel(
            location_panel, text="Alert location: RNT hostel  |  Block: not set  |  Room: not set",
            text_color=MUTED, font=ctk.CTkFont(family=FONT, size=11), anchor="w"
        )
        self.location_status.grid(row=2, column=0, columnspan=4, sticky="ew", padx=16, pady=(0, 10))

        # status cards
        cards = ctk.CTkFrame(self.main, fg_color="transparent")
        cards.grid(row=2, column=0, sticky="ew", padx=16, pady=6)
        for col in range(4):
            cards.grid_columnconfigure(col, weight=1)
        self._status_card(cards, 0, "camera", "CAMERA", "◉", "Stopped")
        self._status_card(cards, 1, "motion", "MOTION", "≋", "Waiting")
        self._status_card(cards, 2, "telegram", "TELEGRAM", "✈", "Not connected")
        self._status_card(cards, 3, "tamper", "TAMPER GUARD", "⌑", "Standby")

        # content
        content = ctk.CTkFrame(self.main, fg_color="transparent")
        content.grid(row=3, column=0, sticky="nsew", padx=22, pady=(8, 8))
        content.grid_columnconfigure(0, weight=3)
        content.grid_columnconfigure(1, weight=2)
        content.grid_rowconfigure(0, weight=1)

        camera_panel = ctk.CTkFrame(content, fg_color=PANEL, corner_radius=16,
                                    border_width=1, border_color=BORDER)
        camera_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        camera_panel.grid_columnconfigure(0, weight=1)
        camera_panel.grid_rowconfigure(2, weight=1)

        camera_header = ctk.CTkFrame(camera_panel, fg_color="transparent")
        camera_header.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 8))
        ctk.CTkLabel(camera_header, text="Live camera", text_color=TEXT,
                     font=ctk.CTkFont(family=FONT, size=17, weight="bold")).pack(side="left")
        self.live_badge = ctk.CTkLabel(camera_header, text="● OFFLINE", text_color=MUTED,
                                       font=ctk.CTkFont(family=FONT, size=11, weight="bold"))
        self.live_badge.pack(side="right")
        self.countdown_label = ctk.CTkLabel(camera_header, text="", text_color=AMBER,
                                            font=ctk.CTkFont(family=MONO, size=13, weight="bold"))
        self.countdown_label.pack(side="right", padx=14)

        self.arm_progress = ctk.CTkProgressBar(camera_panel, height=6, corner_radius=3,
                                               fg_color=BORDER, progress_color=AMBER)
        self.arm_progress.set(0)
        self.arm_progress.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 10))
        self.arm_progress.grid_remove()

        self.preview_holder = ctk.CTkFrame(camera_panel, fg_color="#05080f", corner_radius=12,
                                           width=480, height=300)
        self.preview_holder.grid(row=2, column=0, sticky="nsew", padx=14, pady=(0, 14))
        self.preview_label = ctk.CTkLabel(self.preview_holder, text="", fg_color="transparent",
                                          text_color=MUTED, font=ctk.CTkFont(family=FONT, size=15))
        self.preview_label.place(relx=0.5, rely=0.5, anchor="center")
        self._show_placeholder()

        right_panel = ctk.CTkFrame(content, fg_color=PANEL, corner_radius=16,
                                   border_width=1, border_color=BORDER)
        right_panel.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        right_panel.grid_columnconfigure(0, weight=1)
        right_panel.grid_rowconfigure(2, weight=1)

        act_header = ctk.CTkFrame(right_panel, fg_color="transparent")
        act_header.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 2))
        ctk.CTkLabel(act_header, text="Activity", text_color=TEXT,
                     font=ctk.CTkFont(family=FONT, size=17, weight="bold")).pack(side="left")
        self.tamper_switch = ctk.CTkSwitch(
            act_header, text="Tamper guard", command=self._toggle_tamper,
            text_color=CYAN, font=ctk.CTkFont(family=FONT, size=11)
        )
        self.tamper_switch.select()
        self.tamper_switch.pack(side="right", padx=(6, 10))
        ctk.CTkButton(act_header, text="Clear", width=56, height=24, corner_radius=8,
                      fg_color=PANEL_ALT, hover_color=BORDER, text_color=MUTED,
                      font=ctk.CTkFont(family=FONT, size=11), command=self._clear_log).pack(side="right")

        self.activity_summary = ctk.CTkLabel(
            right_panel, text="Motion events: 0     Last event: —",
            text_color=MUTED, font=ctk.CTkFont(family=FONT, size=11))
        self.activity_summary.grid(row=1, column=0, sticky="w", padx=18, pady=(0, 8))

        self.activity_box = ctk.CTkTextbox(
            right_panel, fg_color="#0b1222", text_color="#94a3b8", corner_radius=10,
            font=ctk.CTkFont(family=MONO, size=12), wrap="word", border_width=1, border_color=BORDER
        )
        self.activity_box.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        for level, color in (("ok", GREEN), ("warn", AMBER), ("err", RED),
                             ("alert", VIOLET), ("info", "#94a3b8")):
            self.activity_box.tag_config(level, foreground=color)
        self.activity_box.configure(state="disabled")

        ctk.CTkLabel(
            self.main,
            text="Privacy: use this camera only where you have permission. Alerts need internet access.",
            text_color=MUTED, font=ctk.CTkFont(family=FONT, size=10)
        ).grid(row=4, column=0, sticky="w", padx=28, pady=(0, 10))

    def _sidebar_heading(self, text):
        ctk.CTkLabel(self.sidebar, text=text, text_color=MUTED,
                     font=ctk.CTkFont(family=FONT, size=11, weight="bold")
                     ).pack(anchor="w", padx=22, pady=(26, 8))

    def _status_card(self, parent, column, key, title, icon, initial):
        card = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=14, border_width=1, border_color=BORDER)
        card.grid(row=0, column=column, sticky="ew", padx=6, pady=4)
        bar = ctk.CTkFrame(card, width=4, corner_radius=2, fg_color=MUTED)
        bar.pack(side="left", fill="y", padx=(12, 0), pady=14)
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(side="left", fill="both", expand=True, padx=12, pady=10)
        ctk.CTkLabel(body, text=f"{icon}  {title}", text_color=MUTED,
                     font=ctk.CTkFont(family=FONT, size=10, weight="bold")).pack(anchor="w")
        value = ctk.CTkLabel(body, text=initial, text_color=TEXT,
                             font=ctk.CTkFont(family=FONT, size=18, weight="bold"))
        value.pack(anchor="w", pady=(2, 0))
        self.cards[key] = (value, bar)

    def _set_card(self, key, text, color):
        value, bar = self.cards[key]
        value.configure(text=text, text_color=color)
        bar.configure(fg_color=color)

    def _set_badge(self, state):
        self.badge_state = state
        texts = {"offline": ("● OFFLINE", MUTED), "preview": ("● PREVIEW", CYAN),
                 "arming": ("● ARMING", AMBER), "armed": ("● ARMED  ·  LIVE", GREEN)}
        text, color = texts[state]
        self.live_badge.configure(text=text, text_color=color)

    def _pulse(self):
        if self.shutdown_event.is_set():
            return
        self._pulse_on = not self._pulse_on
        if self.badge_state == "armed":
            self.live_badge.configure(text_color=GREEN if self._pulse_on else "#14633f")
        elif self.badge_state == "arming":
            self.live_badge.configure(text_color=AMBER if self._pulse_on else "#7a5a10")
        self.after(600, self._pulse)

    def _show_placeholder(self):
        self.preview_label.configure(
            image=self.blank_image,
            text=f"◉\n\nCamera offline\n\nPress “Start monitoring” — you'll get {ARM_DELAY_SECONDS} seconds to leave the room."
        )

    # -------------------- location --------------------
    def _hostel_selection_changed(self, value):
        # The chosen hostel is applied when the user presses Apply.
        return

    def _apply_location(self):
        self.hostel_name = self.hostel_picker.get().strip() or "RNT hostel"
        self.block_number = self.block_entry.get().strip()
        self.room_number = self.room_entry.get().strip()
        block = self.block_number or "not set"
        room = self.room_number or "not set"
        self.location_status.configure(
            text=f"Alert location: {self.hostel_name}  |  Block: {block}  |  Room: {room}"
        )
        self._log(f"Alert location updated: {self.hostel_name}, Block {block}, Room {room}.", "ok")

    def _location_for_alert(self):
        block = self.block_number or "Not set"
        room = self.room_number or "Not set"
        return f"Hostel: {self.hostel_name}\nBlock: {block}\nRoom: {room}"

    # -------------------- logging / clock --------------------
    def _log(self, message, level="info"):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {redact(message)}"
        self.activity_lines.put((line, level))

    def _clear_log(self):
        self.activity_box.configure(state="normal")
        self.activity_box.delete("1.0", "end")
        self.activity_box.configure(state="disabled")

    def _drain_activity(self):
        if self.shutdown_event.is_set():
            return
        items = []
        while True:
            try:
                items.append(self.activity_lines.get_nowait())
            except queue.Empty:
                break
        if items:
            self.activity_box.configure(state="normal")
            for line, level in items:
                self.activity_box.insert("end", line + "\n", level)
            self.activity_box.see("end")
            self.activity_box.configure(state="disabled")
        self.after(200, self._drain_activity)

    def _update_clock(self):
        if self.shutdown_event.is_set():
            return
        self.clock_label.configure(text=datetime.now().strftime("%a %d %b  •  %I:%M:%S %p"))
        self.after(1000, self._update_clock)

    # -------------------- arming countdown --------------------
    def _tick_countdown(self):
        if self.shutdown_event.is_set():
            return
        if self.monitoring_active and self.arm_deadline is not None:
            remaining = self.arm_deadline - time.monotonic()
            if remaining > 0:
                secs = math.ceil(remaining)
                self.countdown_label.configure(text=f"ARMS IN {secs:02d}s", text_color=AMBER)
                self.arm_progress.set(max(0.0, min(1.0, 1 - remaining / ARM_DELAY_SECONDS)))
                self._set_card("motion", f"Arming in {secs}s", AMBER)
                if self.badge_state != "arming":
                    self._set_badge("arming")
                if secs <= 3 and secs != self._last_beep_sec:
                    self._last_beep_sec = secs
                    beep(800, 90)
            elif not self._armed_ui:
                self._armed_ui = True
                self.arm_progress.grid_remove()
                self.countdown_label.configure(text="ARMED", text_color=GREEN)
                self._set_card("motion", "Armed — watching", GREEN)
                self._set_badge("armed")
                beep(1400, 250)
        self.after(100, self._tick_countdown)

    # -------------------- Supabase authentication and Telegram linking --------------------
    def _auth_headers(self):
        with self.auth_lock:
            access = self.supabase_access_token
        return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {access}",
                "Content-Type": "application/json"}

    def _try_restore_session(self):
        """Restore a refresh token from the OS credential vault, if keyring is installed."""
        if keyring is None or not SUPABASE_URL or not SUPABASE_KEY:
            return
        # keyring does not expose account enumeration; use the last account hint only if stored.
        try:
            email = keyring.get_password("NITA_JARVIS_ACCOUNT", "last_email")
            refresh = keyring.get_password("NITA_JARVIS", email) if email else None
            if not email or not refresh:
                return
            def worker():
                try:
                    response = requests.post(
                        f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token",
                        headers={"apikey": SUPABASE_KEY, "Content-Type": "application/json"},
                        json={"refresh_token": refresh}, timeout=20
                    )
                    if not response.ok:
                        return
                    data = response.json()
                    with self.auth_lock:
                        self.supabase_access_token = data["access_token"]
                        self.supabase_refresh_token = data.get("refresh_token", refresh)
                        self.supabase_user_email = email
                        self.supabase_authenticated = True
                    if keyring is not None:
                        keyring.set_password("NITA_JARVIS", email, self.supabase_refresh_token)
                    self._ui(lambda: self._login_success_ui(email))
                    self._ui(self.check_telegram_link)
                except Exception as exc:
                    self._log(f"Saved sign-in could not be restored: {type(exc).__name__}", "warn")
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            return

    def _toggle_tamper(self):
        self.tamper_enabled = bool(self.tamper_switch.get())
        self._log("Camera tamper protection " + ("enabled." if self.tamper_enabled else "disabled."), "info")
        if not self.tamper_enabled:
            self._stop_siren()
            self._set_card("tamper", "Disabled", MUTED)

    def _start_siren(self):
        if winsound is None or (self.siren_thread and self.siren_thread.is_alive()):
            return
        self.siren_stop_event.clear()
        def alarm_loop():
            # Repeating two-tone alarm; user can stop it by disarming.
            while not self.siren_stop_event.is_set():
                try:
                    winsound.Beep(1500, 450)
                    if self.siren_stop_event.wait(0.08): break
                    winsound.Beep(950, 550)
                except Exception:
                    break
        self.siren_thread = threading.Thread(target=alarm_loop, daemon=True)
        self.siren_thread.start()

    def _stop_siren(self):
        self.siren_stop_event.set()
        self.tamper_latched = False

    def _tamper_detected(self, frame, reason):
        if self.tamper_latched:
            return
        self.tamper_latched = True
        self._start_siren()
        self._ui(lambda: self._set_card("tamper", "TAMPER ALERT", RED))
        self._log(f"CAMERA TAMPER DETECTED: {reason}. Local siren activated.", "err")
        payload = encode_jpeg(frame)
        if payload:
            caption = (f"NITA JARVIS — CAMERA TAMPER ALERT\n{self._location_for_alert()}\n"
                       f"Possible camera covering detected.\nTime: {timestamp()}")
            self._enqueue_photo((payload, caption))

    def handle_login(self):
        email = self.email_entry.get().strip()
        password = self.password_entry.get()
        if not SUPABASE_URL or not SUPABASE_KEY:
            self._log("Supabase settings are missing. Check NITA_SUPABASE_URL and NITA_SUPABASE_PUBLISHABLE_KEY in Windows environment variables.", "err")
            return
        if not email or not password:
            self._log("Enter your email and password first.", "warn")
            return
        self.login_button.configure(state="disabled", text="Signing in…")
        threading.Thread(target=self._login_worker, args=(email, password), daemon=True).start()

    def _login_worker(self, email, password):
        try:
            response = requests.post(f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
                headers={"apikey": SUPABASE_KEY, "Content-Type": "application/json"},
                json={"email": email, "password": password}, timeout=20)
            if not response.ok:
                raise RuntimeError(response.json().get("msg") or response.json().get("message") or f"Login failed (HTTP {response.status_code})")
            data = response.json()
            with self.auth_lock:
                self.supabase_access_token = data["access_token"]
                self.supabase_refresh_token = data.get("refresh_token", "")
                self.supabase_user_email = email
                self.supabase_authenticated = True
            if keyring is not None:
                try:
                    keyring.set_password("NITA_JARVIS_ACCOUNT", "last_email", email)
                    keyring.set_password("NITA_JARVIS", email, data.get("refresh_token", ""))
                except Exception:
                    pass
            self._ui(lambda: self._login_success_ui(email))
        except Exception as exc:
            msg = str(exc)
            self._ui(lambda m=msg: self._login_failed_ui(m))

    def _login_success_ui(self, email):
        self.login_button.configure(state="normal", text="Account ready")
        self._set_card("telegram", "Link Telegram", AMBER)
        self._log(f"Signed in as {email}. Now click Link Telegram.", "ok")

    def _login_failed_ui(self, message):
        self.login_button.configure(state="normal", text="Log in")
        self._log(f"Login failed: {message}", "err")

    def refresh_supabase_session(self):
        with self.auth_lock:
            refresh = self.supabase_refresh_token
        if not refresh:
            return False
        try:
            response = requests.post(f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token",
                headers={"apikey": SUPABASE_KEY, "Content-Type": "application/json"},
                json={"refresh_token": refresh}, timeout=20)
            if not response.ok:
                return False
            data = response.json()
            with self.auth_lock:
                self.supabase_access_token = data["access_token"]
                self.supabase_refresh_token = data.get("refresh_token", refresh)
            return True
        except Exception:
            return False

    def create_telegram_link(self):
        if not self.supabase_authenticated:
            self._log("Please log in before linking Telegram.", "warn")
            return
        self.connect_button.configure(state="disabled", text="Creating link…")
        threading.Thread(target=self._create_link_worker, daemon=True).start()

    def _create_link_worker(self):
        try:
            response = requests.post(f"{SUPABASE_URL}/functions/v1/create-telegram-link",
                headers=self._auth_headers(), json={}, timeout=20)
            if response.status_code == 401 and self.refresh_supabase_session():
                response = requests.post(f"{SUPABASE_URL}/functions/v1/create-telegram-link",
                    headers=self._auth_headers(), json={}, timeout=20)
            if not response.ok:
                raise RuntimeError(f"Could not create Telegram link (HTTP {response.status_code}): {response.text[:250]}")
            data = response.json()
            link = data.get("bot_url") or data.get("telegram_url") or data.get("url")
            if not link:
                raise RuntimeError("The function response did not contain bot_url. Check create-telegram-link response format.")
            self._ui(lambda u=link: self._open_telegram_link(u))
        except Exception as exc:
            msg = str(exc)
            self._ui(lambda m=msg: self._telegram_action_failed(m))

    def _open_telegram_link(self, url):
        self.connect_button.configure(state="normal", text="Open Telegram link again")
        webbrowser.open(url)
        self._log(
            "Telegram link opened. Press START once in Telegram. "
            "NITA JARVIS will check the connection automatically.", "info"
        )
        self._begin_telegram_auto_check()

    def _begin_telegram_auto_check(self):
        """Automatically detect Telegram linking after the user starts the bot."""
        if self.telegram_linked or self.telegram_link_polling:
            return
        self.telegram_link_polling = True
        self.telegram_poll_generation += 1
        generation = self.telegram_poll_generation
        self._log("Waiting for Telegram authorization (checking every 3 seconds)…", "info")
        threading.Thread(
            target=self._telegram_auto_check_worker,
            args=(generation,),
            daemon=True,
        ).start()

    def _telegram_auto_check_worker(self, generation):
        attempts = 0
        max_attempts = 100  # about five minutes
        try:
            while attempts < max_attempts and not self.shutdown_event.is_set():
                if generation != self.telegram_poll_generation or self.telegram_linked:
                    return
                try:
                    response = requests.post(
                        f"{SUPABASE_URL}/functions/v1/telegram-link-status",
                        headers=self._auth_headers(), json={}, timeout=15
                    )
                    if response.status_code == 401 and self.refresh_supabase_session():
                        response = requests.post(
                            f"{SUPABASE_URL}/functions/v1/telegram-link-status",
                            headers=self._auth_headers(), json={}, timeout=15
                        )
                    if not response.ok:
                        self._ui(lambda code=response.status_code: self._log(
                            f"Automatic Telegram check failed (HTTP {code}).", "warn"
                        ))
                        return
                    if bool(response.json().get("linked", False)):
                        self._ui(lambda: self._telegram_status_ui(True))
                        self._ui(lambda: self._log(
                            "Telegram connected automatically. You can now arm your room.", "ok"
                        ))
                        return
                except requests.RequestException as exc:
                    if attempts % 10 == 0:
                        self._ui(lambda kind=type(exc).__name__: self._log(
                            f"Telegram check will retry ({kind}).", "warn"
                        ))
                attempts += 1
                if self.shutdown_event.wait(3):
                    return
            if not self.shutdown_event.is_set() and not self.telegram_linked:
                self._ui(lambda: self._log(
                    "Still waiting for Telegram. Open the bot and press START once; "
                    "the app cannot press START on your behalf.", "warn"
                ))
        finally:
            if generation == self.telegram_poll_generation:
                self.telegram_link_polling = False

    def check_telegram_link(self):
        if not self.supabase_authenticated:
            self._log("Please log in first.", "warn")
            return
        self.status_button.configure(state="disabled", text="Checking…")
        threading.Thread(target=self._check_link_worker, daemon=True).start()

    def _check_link_worker(self):
        try:
            response = requests.post(f"{SUPABASE_URL}/functions/v1/telegram-link-status",
                headers=self._auth_headers(), json={}, timeout=20)
            if response.status_code == 401 and self.refresh_supabase_session():
                response = requests.post(f"{SUPABASE_URL}/functions/v1/telegram-link-status",
                    headers=self._auth_headers(), json={}, timeout=20)
            if not response.ok:
                raise RuntimeError(f"Status check failed (HTTP {response.status_code}). Deploy telegram-link-status first.")
            linked = bool(response.json().get("linked"))
            self._ui(lambda value=linked: self._telegram_status_ui(value))
        except Exception as exc:
            msg = str(exc)
            self._ui(lambda m=msg: self._telegram_action_failed(m))

    def _telegram_status_ui(self, linked):
        self.status_button.configure(state="normal", text="Check Telegram status")
        self.telegram_linked = linked
        if linked:
            self.telegram_poll_generation += 1
            self.telegram_link_polling = False
            self._set_card("telegram", "Connected", GREEN)
            self._log("Telegram is linked. You can start monitoring.", "ok")
        else:
            self._set_card("telegram", "Not linked", AMBER)
            self._log("Telegram is not linked yet. Open Link Telegram and press Start in the bot.", "warn")

    def _telegram_action_failed(self, message):
        self.connect_button.configure(state="normal", text="Link Telegram")
        self.status_button.configure(state="normal", text="Check Telegram status")
        self._log(f"Telegram setup error: {message}", "err")

    def _enqueue_photo(self, item):
        try:
            self.photo_queue.put_nowait(item)
        except queue.Full:
            try:
                self.photo_queue.get_nowait()   # drop the oldest photo
            except queue.Empty:
                pass
            try:
                self.photo_queue.put_nowait(item)
            except queue.Full:
                pass
            self._log("Upload queue full (offline?) — dropped the oldest photo.", "warn")

    def _telegram_sender_loop(self):
        session = requests.Session()
        while not self.shutdown_event.is_set():
            try:
                payload, caption = self.photo_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            delay = TELEGRAM_RETRY_START_SECONDS
            while not self.shutdown_event.is_set():
                if not self.supabase_authenticated or not self.telegram_linked:
                    self.shutdown_event.wait(1)
                    continue
                try:
                    headers = self._auth_headers()
                    headers.pop("Content-Type", None)  # requests sets multipart boundary
                    files = {"photo": ("motion.jpg", io.BytesIO(payload), "image/jpeg")}
                    response = session.post(f"{SUPABASE_URL}/functions/v1/send-motion-alert",
                        headers=headers, data={"caption": caption}, files=files, timeout=35)
                    if response.status_code == 401 and self.refresh_supabase_session():
                        headers = self._auth_headers()
                        headers.pop("Content-Type", None)
                        files = {"photo": ("motion.jpg", io.BytesIO(payload), "image/jpeg")}
                        response = session.post(f"{SUPABASE_URL}/functions/v1/send-motion-alert",
                            headers=headers, data={"caption": caption}, files=files, timeout=35)
                    if response.ok:
                        self._ui(lambda: self._set_card("telegram", "Connected", GREEN))
                        self._log("Photo sent through secure Telegram service.", "ok")
                        break
                    if response.status_code in (400, 403, 404):
                        self._log(f"Alert rejected (HTTP {response.status_code}); photo dropped: {response.text[:180]}", "err")
                        break
                    if response.status_code == 401:
                        self._log("Session expired. Please log in again; queued photo is waiting.", "warn")
                        self.supabase_authenticated = False
                        self._ui(lambda: self.login_button.configure(state="normal", text="Log in"))
                        self.shutdown_event.wait(2)
                        continue
                    raise RuntimeError(f"Supabase HTTP {response.status_code}: {response.text[:180]}")
                except Exception as exc:
                    self._ui(lambda: self._set_card("telegram", "Retrying…", AMBER))
                    self._log(f"Photo upload failed; retrying in {delay}s. ({type(exc).__name__}: {exc})", "warn")
                    self.shutdown_event.wait(delay)
                    delay = min(delay * 2, TELEGRAM_RETRY_MAX_SECONDS)
        session.close()

    # -------------------- Camera / motion --------------------
    def _camera_alive(self):
        return self.camera_thread is not None and self.camera_thread.is_alive()

    def _start_camera(self):
        self.stop_camera_event.clear()
        self.camera_thread = threading.Thread(target=self._camera_loop, daemon=True)
        self.camera_thread.start()
        self.stop_button.configure(state="normal")

    def start_monitoring(self):
        if not self.supabase_authenticated:
            self._log("Log in before starting monitoring.", "warn")
            return
        if not self.telegram_linked:
            self._log("Link Telegram and check its status before starting monitoring.", "warn")
            return
        alive = self._camera_alive()
        if alive and self.stop_camera_event.is_set():
            self._log("Camera is still stopping — try again in a moment.", "warn")
            return
        if alive and self.monitoring_active:
            return
        # If the camera is already open in preview (from Select doorway), just arm it.
        self.monitoring_active = True
        self._armed_ui = False
        self._last_beep_sec = None
        self._stop_siren()
        self._last_tamper_alert = 0.0
        self.arm_deadline = time.monotonic() + ARM_DELAY_SECONDS
        self.arm_progress.set(0)
        self.arm_progress.grid()
        self._set_card("camera", "Running" if alive else "Starting…", GREEN if alive else AMBER)
        self._set_badge("arming")
        if not alive:
            self._start_camera()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._log(f"Monitoring requested — arming in {ARM_DELAY_SECONDS}s. Leave the room now.", "warn")

    def stop_monitoring(self):
        self._stop_siren()
        self.stop_camera_event.set()
        self.stop_button.configure(state="disabled")
        # Start is re-enabled only once the camera thread has really finished.
        self._log("Stopping camera…", "info")

    def select_doorway(self):
        # Already have a selector open? Just bring it to the front.
        win = getattr(self, "roi_window", None)
        try:
            if win is not None and win.winfo_exists():
                win.lift()
                win.focus_force()
                return
        except tk.TclError:
            pass

        alive = self._camera_alive()
        if alive and self.stop_camera_event.is_set():
            self._log("Camera is still stopping — try again in a moment.", "warn")
            return
        if not alive:
            # Open the camera in preview mode only: no monitoring, no alerts.
            self.monitoring_active = False
            self.arm_deadline = None
            self._set_card("camera", "Starting…", AMBER)
            self._set_badge("preview")
            self._start_camera()
            self._log("Opening the camera so you can pick the doorway (monitoring stays off).", "info")
        self._open_roi_when_ready()

    def _open_roi_when_ready(self, tries=0):
        if self.shutdown_event.is_set():
            return
        with self.frame_lock:
            frame = None if self.latest_clean is None else self.latest_clean.copy()
        if frame is not None:
            self.roi_window = RoiSelector(self, frame, self._set_roi)
            return
        if (not self._camera_alive() and tries > 3) or tries > 150:
            self._log("Could not get an image from the camera to select the doorway.", "err")
            return
        self.after(100, lambda: self._open_roi_when_ready(tries + 1))

    def _set_roi(self, roi):
        with self.roi_lock:
            self.roi = roi
        if roi:
            self._log("Doorway area saved. Motion detection now focuses on that rectangle.", "ok")
        else:
            self._log("Monitoring the full frame.", "info")

    @staticmethod
    def _new_subtractor():
        return cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=40, detectShadows=False)

    def _camera_loop(self):
        camera = None
        keep_awake_enabled = False
        failed = False
        try:
            camera = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            if not camera.isOpened():
                camera.release()
                camera = cv2.VideoCapture(0)
            if not camera.isOpened():
                self._log("Could not open the webcam. Close other apps using it and try again.", "err")
                failed = True
                return

            try:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
                keep_awake_enabled = True
                self._log("Windows sleep prevention enabled while monitoring.", "info")
            except Exception:
                self._log("Windows keep-awake request was unavailable on this computer.", "warn")

            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
            subtractor = self._new_subtractor()
            last_roi = None
            frames_seen = 0
            was_armed = False
            last_event_time = 0.0
            alert_until = 0.0
            burst_remaining = 0
            next_burst_time = 0.0
            tamper_reference = None
            tamper_started = None
            last_tamper_notify = 0.0

            def _camera_ready():
                if self.monitoring_active:
                    self._set_card("camera", "Running", GREEN)
                else:
                    self._set_card("camera", "Preview", CYAN)
                    self._set_badge("preview")
            self._ui(_camera_ready)
            self._log("Camera is running. The background model learns the empty room.", "ok")

            while not self.stop_camera_event.is_set():
                success, frame = camera.read()
                if not success:
                    self._log("Could not read a camera frame; stopping.", "err")
                    failed = True
                    break

                frame = cv2.flip(frame, 1)
                display_frame = frame.copy()
                now = time.time()
                mono = time.monotonic()

                with self.roi_lock:
                    roi = self.roi
                if roi != last_roi:           # doorway changed: relearn the background
                    subtractor = self._new_subtractor()
                    frames_seen = 0
                    last_roi = roi

                fh, fw = frame.shape[:2]
                if roi:
                    x, y, w, h = roi
                    rx = max(0, min(x, fw - 1))
                    ry = max(0, min(y, fh - 1))
                    rw = max(1, min(w, fw - rx))
                    rh = max(1, min(h, fh - ry))
                    region = frame[ry:ry + rh, rx:rx + rw]
                    cv2.rectangle(display_frame, (rx, ry), (rx + rw, ry + rh), BGR_CYAN, 2)
                else:
                    rx = ry = 0
                    region = frame

                mask = subtractor.apply(cv2.GaussianBlur(region, (5, 5), 0))
                mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
                mask = cv2.dilate(mask, kernel, iterations=2)
                frames_seen += 1

                armed = self.arm_deadline is not None and mono >= self.arm_deadline
                if armed and not was_armed:
                    was_armed = True
                    # Capture a reference scene once the exit countdown has finished.
                    tamper_reference = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    self._log("ARMED — monitoring is live.", "ok")
                    self._ui(lambda: self._set_card("tamper", "Guard active", GREEN))

                # Physical-cover detection: compare against the room scene captured at arming.
                # It looks for sustained scene replacement plus a major loss of image texture.
                if armed and self.tamper_enabled and tamper_reference is not None and not self.tamper_latched:
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    small = cv2.resize(gray, (160, 120), interpolation=cv2.INTER_AREA)
                    ref_small = cv2.resize(tamper_reference, (160, 120), interpolation=cv2.INTER_AREA)
                    diff_fraction = float((cv2.absdiff(small, ref_small) > 35).mean())
                    texture = float(cv2.Laplacian(gray, cv2.CV_64F).var())
                    brightness = float(gray.mean())
                    dark_cover = brightness < TAMPER_BRIGHTNESS_LOW
                    replaced_scene = diff_fraction > TAMPER_DIFF_THRESHOLD
                    low_detail_cover = texture < TAMPER_TEXTURE_THRESHOLD and replaced_scene
                    suspected = dark_cover or low_detail_cover
                    if suspected:
                        if tamper_started is None:
                            tamper_started = mono
                        elif mono - tamper_started >= TAMPER_CONFIRM_SECONDS:
                            reason = "dark image" if dark_cover else "scene covered or replaced"
                            self._tamper_detected(frame, reason)
                    else:
                        tamper_started = None
                        # Slowly adapt reference only when the scene is not suspected as covered.
                        tamper_reference = cv2.addWeighted(tamper_reference, 0.995,
                            cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), 0.005, 0)

                if self.tamper_latched:
                    put_text_outlined(display_frame, "!! CAMERA TAMPER - SIREN ACTIVE !!",
                                      (24, 88), 0.7, BGR_RED, 2)

                contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                big = [c for c in contours if cv2.contourArea(c) >= MOTION_MIN_AREA]
                lighting_shift = cv2.countNonZero(mask) > LIGHT_CHANGE_FRACTION * mask.size

                motion = False
                if big and not lighting_shift and armed:
                    for c in big:
                        bx, by, bw, bh = cv2.boundingRect(c)
                        cv2.rectangle(display_frame, (rx + bx, ry + by), (rx + bx + bw, ry + by + bh),
                                      BGR_GREEN, 2)
                    motion = frames_seen > WARMUP_FRAMES

                if motion and now - last_event_time >= QUIET_SECONDS and burst_remaining == 0:
                    last_event_time = now
                    alert_until = now + 2.0
                    burst_remaining = PHOTO_COUNT
                    next_burst_time = now
                    event_stamp = timestamp()
                    self.motion_events += 1
                    self.last_motion_time = event_stamp
                    count = self.motion_events
                    self._ui(lambda: self._set_card("motion", "MOTION DETECTED", RED))
                    self._ui(lambda c=count, s=event_stamp: self.activity_summary.configure(
                        text=f"Motion events: {c}     Last event: {s}"))
                    self._log(f"Motion detected at {event_stamp}. Capturing {PHOTO_COUNT} photos.", "alert")

                # One photo per second for the burst, kept in RAM only.
                if burst_remaining > 0 and now >= next_burst_time:
                    payload = encode_jpeg(frame)
                    index = PHOTO_COUNT - burst_remaining + 1
                    if payload:
                        caption = (
                            f"NITA JARVIS — MOTION ALERT\n"
                            f"{self._location_for_alert()}\n"
                            f"Photo {index}/{PHOTO_COUNT}\n"
                            f"Time: {timestamp()}"
                        )
                        self._enqueue_photo((payload, caption))
                        self._log(f"Photo {index}/{PHOTO_COUNT} queued in RAM.", "info")
                    else:
                        self._log("Could not encode a motion photo.", "err")
                    burst_remaining -= 1
                    next_burst_time = now + PHOTO_INTERVAL_SECONDS
                    if burst_remaining == 0:
                        self._ui(lambda: self._set_card("motion", "Armed — watching", GREEN))

                remaining = (self.arm_deadline - mono) if self.arm_deadline is not None else 0
                draw_hud(display_frame, armed, remaining, now < alert_until or burst_remaining > 0,
                         preview=self.arm_deadline is None)

                with self.frame_lock:
                    self.latest_frame = display_frame
                    self.latest_clean = frame
                    self.frame_counter += 1

            self._log("Camera stopped.", "info")
        except Exception as exc:
            self._log(f"Camera error: {exc}", "err")
            failed = True
        finally:
            if camera is not None:
                camera.release()
            if keep_awake_enabled:
                try:
                    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
                except Exception:
                    pass
            self._ui(lambda f=failed: self._camera_stopped_ui(f))

    def _camera_stopped_ui(self, failed=False):
        self._stop_siren()
        self.monitoring_active = False
        self.arm_deadline = None
        self._armed_ui = False
        with self.frame_lock:
            self.latest_frame = None
            self.latest_clean = None
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self._set_card("camera", "Camera error" if failed else "Stopped", RED if failed else MUTED)
        self._set_card("motion", "Waiting", MUTED)
        self._set_card("tamper", "Standby", MUTED)
        self._set_badge("offline")
        self.arm_progress.grid_remove()
        self.countdown_label.configure(text="")
        self._show_placeholder()

    def _refresh_preview(self):
        if self.shutdown_event.is_set():
            return
        with self.frame_lock:
            frame = self.latest_frame
            counter = self.frame_counter
        if frame is not None and counter != self._shown_counter:
            self._shown_counter = counter
            try:
                try:
                    scaling = self._get_window_scaling()
                except Exception:
                    scaling = 1.0
                # Size from the holder (not the label) so the preview can shrink again.
                avail_w = (self.preview_holder.winfo_width() - 8) / scaling
                avail_h = (self.preview_holder.winfo_height() - 8) / scaling
                if avail_w > 50 and avail_h > 50:
                    fh, fw = frame.shape[:2]
                    scale = min(avail_w / fw, avail_h / fh)
                    nw, nh = max(1, int(fw * scale)), max(1, int(fh * scale))
                    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
                    small = cv2.resize(frame, (nw, nh), interpolation=interp)
                    image = Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
                    ctk_image = ctk.CTkImage(light_image=image, dark_image=image, size=(nw, nh))
                    self.preview_label.configure(image=ctk_image, text="")
                    self.preview_label.image = ctk_image
            except Exception:
                pass
        self.after(33, self._refresh_preview)

    def _on_close(self):
        self._stop_siren()
        self.shutdown_event.set()
        self.stop_camera_event.set()
        if self.camera_thread and self.camera_thread.is_alive():
            self.camera_thread.join(timeout=3)
        self.destroy()


if __name__ == "__main__":
    app = NitaJarvisApp()
    app.mainloop()
