import socket
import struct
import threading
import time
import math
from collections import deque
import queue
import numpy as np
import tkinter as tk
from tkinter import messagebox
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import json
import os
from PIL import Image, ImageDraw, ImageFont, ImageTk
import re


# =========================
# JSON Machine Config (single-file run)
# =========================
CONFIG_PATH = "config457_thk.json"

def load_config_json(path: str) -> dict:
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Config JSON not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# =========================
# עזר: פריסת חבילת PUSH
# =========================
def parse_push_packet(data: bytes):
    if len(data) < 18:
        return None
    # Hardware timestamp of the first measurement value (bytes 6..9, uint32 BE,
    # nanoseconds, resets every second per OD5000 spec table 47). Using this
    # instead of time.time() is what makes the sample timing immune to CPU
    # bursts — otherwise multiple packets that arrive close together end up
    # tagged with nearly identical host timestamps, and the result graph shows
    # samples clumped to within microseconds of each other.
    ts_ns_first = struct.unpack(">I", data[6:10])[0]
    payload = data[14:]
    if len(payload) < 4 or (len(payload) % 4) != 0:
        return None
    total_vals = len(payload) // 4
    vals_nm = struct.unpack(">" + "i" * total_vals, payload)
    return ts_ns_first, [v / 1_000_000.0 for v in vals_nm]  # (ns, [mm,...])


# =========================
# PIL Fonts Helper
# =========================
_FONT_CACHE = {}

def get_fonts():
    """Single source of truth for PIL fonts used by this module."""
    key = "v1"
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]

    win_dir = os.environ.get("WINDIR", r"C:\Windows")
    win_fonts = os.path.join(win_dir, "Fonts")

    def ttf(fname: str, size: int):
        try:
            return ImageFont.truetype(os.path.join(win_fonts, fname), size=size)
        except Exception:
            return ImageFont.load_default()

    fonts = {
        "label":  ttf("arial.ttf", 12),
        "value":  ttf("arial.ttf", 14),
        "status": ttf("arial.ttf", 18),
        "symbol": ttf("seguisym.ttf", 20),
        "emoji":  ttf("seguiemj.ttf", 18),
    }

    _FONT_CACHE[key] = fonts
    return fonts


# =========================
# RTL text helpers (Hebrew)
# =========================
_HEB_RE = re.compile(r"[\u0590-\u05FF]")
_NUM_RE = re.compile(r"[-+]?(?:\d+(?:\.\d+)?)")
_LAT_RE = re.compile(r"[A-Za-z]+")


def _rtl_visual(text: str) -> str:
    if text is None:
        return ""
    s = str(text)
    if not _HEB_RE.search(s):
        return s

    # Reverse full string
    rs = s[::-1]

    # Swap bracket directions after reversal
    swap = str.maketrans({
        "(": ")", ")": "(",
        "[": "]", "]": "[",
        "{": "}", "}": "{",
        "<": ">", ">": "<",
    })
    rs = rs.translate(swap)

    # Reverse digit sequences back
    def _rev(m):
        return m.group(0)[::-1]

    rs = _NUM_RE.sub(_rev, rs)

    # Reverse latin runs back (e.g. mm, Time, PLC)
    rs = _LAT_RE.sub(_rev, rs)
    return rs


# =========================
# Icon loader
# =========================
def _load_icon_png(rel_path: str, size_px: int):
    """Load PNG icon and resize. Returns PIL Image or None."""
    try:
        base = os.path.dirname(os.path.abspath(__file__))
        p = os.path.join(base, rel_path)
        if not os.path.isfile(p):
            return None
        ico = Image.open(p).convert("RGBA")
        if size_px and size_px > 0:
            ico = ico.resize((size_px, size_px), Image.LANCZOS)
        return ico
    except Exception:
        return None


# =========================
# Gauge rendering with PIL
# =========================
def _smooth_counts(counts, sigma_bins=1.0):
    """Simple Gaussian smoothing for histogram bell curve."""
    if not counts or sigma_bins <= 0:
        return counts
    n = len(counts)
    k = int(3 * sigma_bins)
    if k < 1:
        return counts

    def gauss(x, sig):
        return math.exp(-0.5 * (x / sig) ** 2)

    kernel = [gauss(i, sigma_bins) for i in range(-k, k + 1)]
    kernel_sum = sum(kernel)
    if kernel_sum == 0:
        return counts

    kernel = [v / kernel_sum for v in kernel]
    smooth = [0.0] * n
    for i in range(n):
        s = 0.0
        for j, kv in enumerate(kernel):
            idx = i + (j - k)
            if 0 <= idx < n:
                s += counts[idx] * kv
        smooth[i] = s
    return smooth


def _gauge_pil(total_um=20, green_half_um=3, yellow_um=1, tick_um=1,
               pointer_um=None, err_hist_um=None, size_px=(400, 150)):
    """
    Render a gauge bar with:
    - Red/Yellow/Green zones
    - Bell curve from err_hist_um
    - Black pointer triangle
    - Tick marks and labels
    """
    W, H = size_px
    im = Image.new("RGB", (W, H), "white")
    dr = ImageDraw.Draw(im)

    half = float(total_um) / 2.0

    bar_h = 30
    bell_h = 60
    margin_top = 10
    margin_bot = 35

    bell_y0 = margin_top
    bell_y1 = bell_y0 + bell_h
    bar_y0 = bell_y1 + 5
    bar_y1 = bar_y0 + bar_h

    pad_x = 40
    bar_x0 = pad_x
    bar_x1 = W - pad_x

    def x_of(um_val):
        frac = (float(um_val) + half) / (2.0 * half)
        return bar_x0 + frac * (bar_x1 - bar_x0)

    # zones
    g0, g1 = -float(green_half_um), +float(green_half_um)
    y0, y1 = g0 - float(yellow_um), g1 + float(yellow_um)
    # red base
    dr.rectangle([x_of(-half), bar_y0, x_of(+half), bar_y1], fill=(192, 57, 43))
    # yellow
    dr.rectangle([x_of(y0), bar_y0, x_of(g0), bar_y1], fill=(241, 196, 15))
    dr.rectangle([x_of(g1), bar_y0, x_of(y1), bar_y1], fill=(241, 196, 15))
    # green
    dr.rectangle([x_of(g0), bar_y0, x_of(g1), bar_y1], fill=(39, 174, 96))

    # center line
    cx = x_of(0.0)
    dr.line([cx, bar_y0 - 2, cx, bar_y1 + 2], fill=(0, 0, 0), width=2)

    # bell curve (smoothed histogram)
    vals = [float(v) for v in (err_hist_um or []) if v is not None]
    if len(vals) >= 3 and (bell_y1 - bell_y0) >= 20:
        bins = int(max(10, min(80, 30)))
        step = (2.0 * half) / bins
        counts = [0] * bins
        for v in vals:
            if v < -half or v > +half:
                continue
            i = int((v + half) / (2.0 * half) * bins)
            if i >= bins:
                i = bins - 1
            counts[i] += 1
        smooth = _smooth_counts(counts, sigma_bins=1.2)
        m = max(smooth) if smooth else 0.0
        if m > 0:
            bell_height = float(bell_y1 - bell_y0)
            dr.line([x_of(-half), bell_y1, x_of(+half), bell_y1], fill=(0, 0, 0), width=1)
            pts = []
            for i, sv in enumerate(smooth):
                um = -half + (i + 0.5) * step
                x = x_of(um)
                y = bell_y1 - (float(sv) / m) * (bell_height - 4)
                pts.append((x, y))
            for a, b in zip(pts[:-1], pts[1:]):
                dr.line([a, b], fill=(0, 0, 0), width=2)

    fonts = get_fonts()
    font = fonts["label"]

    tick_top = bar_y1
    tick_minor = bar_y1 + 6
    tick_major = bar_y1 + 10
    label_y = bar_y1 + 12
    um = -half
    while um <= half + 1e-9:
        x = x_of(um)
        is_major = (abs(um) < 1e-9) or (int(round(um)) % 5 == 0)
        dr.line([x, tick_top, x, tick_major if is_major else tick_minor], fill=(0, 0, 0), width=2 if is_major else 1)
        if is_major:
            dr.text((x, label_y), f"{int(round(um))}µm", fill=(0, 0, 0), anchor="ma", font=font)
        um += float(tick_um)

    dr.rectangle([x_of(-half), bar_y0, x_of(+half), bar_y1], outline=(51, 51, 51), width=1)

    # pointer
    if pointer_um is None:
        pointer_um = 0.0
    try:
        pointer_um = float(pointer_um)
    except Exception:
        pointer_um = 0.0
    pointer_um = max(-half, min(+half, pointer_um))
    px = x_of(pointer_um)

    tri_h, tri_w = 16, 22
    shaft_h, shaft_w = 14, 5
    tip_y = bar_y0 - 1
    tri_top_y = tip_y - tri_h
    shaft_top_y = tri_top_y - shaft_h

    dr.rectangle([px - shaft_w / 2, shaft_top_y, px + shaft_w / 2, tri_top_y], fill=(17, 17, 17))
    dr.polygon([(px, tip_y), (px - tri_w / 2, tri_top_y), (px + tri_w / 2, tri_top_y)], fill=(17, 17, 17))

    return im


# =========================
# Config + Runtime State (camera-style)
# =========================

class ThicknessConfig(object):
    """Fixed parameters loaded from JSON (no in-code defaults)."""

    def __init__(self, cfg_dict: dict):
        if cfg_dict is None:
            raise ValueError("ThicknessConfig requires cfg_dict loaded from JSON")

        d = cfg_dict

        def req(key: str):
            if key not in d:
                raise KeyError(f"Missing required config key: {key}")
            return d[key]

        # Sensors
        self.SENSORS = req("sensors")  # dict: {"TOP": {"ip": "...", "port": 2112}, "BOTTOM": {...}}

        # Timing
        self.SOCKET_TIMEOUT = float(req("socket_timeout_sec"))
        self.FRAME_INTERVAL_SEC = float(req("frame_interval_sec"))
        self.TIME_SYNC_THRESHOLD = float(req("time_sync_threshold_sec"))
        self.LIVE_UPDATE_HZ = float(req("live_update_hz"))

        # Sensor-reading threshold for "no part" (empty gap) detection.
        self.ERROR_THRESHOLD_MM = float(req("error_threshold_mm"))
        self.AXIAL_SPAN_MM = float(req("axial_span_mm"))

        # Thickness limits / end-of-pass
        self.THICKNESS_MIN = float(req("thickness_min_mm"))
        self.THICKNESS_MAX = float(req("thickness_max_mm"))
        self.MAX_ERROR_COUNT = int(req("max_error_count"))   # legacy, retained for config compatibility (unused)
        # Time-based end-of-part gap. A pass is closed only when there has been at
        # least this much wall-clock time with no in-range sample. Replaces the old
        # sample-count threshold (MAX_ERROR_COUNT), which was sensitive to the
        # sensor's per-packet rate and could split a single part into multiple
        # passes whenever a brief surface feature (groove/scratch) carried the
        # reading out of [THICKNESS_MIN, THICKNESS_MAX] for a few samples.
        self.ERROR_GAP_SEC = float(d.get("error_gap_sec", 0.05))

        # Processing
        self.TARGET_POINTS = int(req("target_points"))
        self.TRIM_LO = float(req("trim_lo"))
        self.TRIM_HI = float(req("trim_hi"))
        self.P2P_THRESH_UM = float(req("p2p_thresh_um"))

        # Conicity threshold
        self.CONICITY_THRESH_UM = float(d.get("conicity_thresh_um", 5.0))

        # Operator defaults
        self.NOMINAL_THICKNESS_MM_DEFAULT = float(req("nominal_thickness_mm_default"))
        self.TOL_UM_DEFAULT = float(req("tol_um_default"))

        # --- Two-gauge linearization (the ONLY source of absolute thickness) ---
        # Thickness is defined exclusively by   thickness_mm = slope * (top_v + bot_v) + offset
        # where (slope, offset) come from measuring two reference gauges of known
        # thickness (T1, T2). Without a valid fit the processor produces nothing.
        cal = d.get("calibration", {}) or {}
        self.CAL_T1_MM = float(cal["gauge_small_known_mm"]) if cal.get("gauge_small_known_mm") is not None else None
        self.CAL_T2_MM = float(cal["gauge_large_known_mm"]) if cal.get("gauge_large_known_mm") is not None else None
        self.CAL_WINDOW_SEC = float(cal.get("window_sec", 1.0))
        self.CAL_STABILITY_MAX_STD_UM = float(cal.get("stability_max_std_um", 5.0))
        # Physically slope ≈ -1 (sum decreases as thickness grows).
        self.CAL_SLOPE_MIN = float(cal.get("slope_min", -1.10))
        self.CAL_SLOPE_MAX = float(cal.get("slope_max", -0.90))
        self.CAL_MAX_PARTS_BEFORE_RECAL = int(cal.get("max_parts_before_recal", 100))
        # Persisted last-successful calibration (loaded on startup).
        self.CAL_S1_MM = float(cal["S1_mm"]) if cal.get("S1_mm") is not None else None
        self.CAL_S2_MM = float(cal["S2_mm"]) if cal.get("S2_mm") is not None else None
        self.CAL_SLOPE = float(cal["slope"]) if cal.get("slope") is not None else None
        self.CAL_OFFSET = float(cal["offset"]) if cal.get("offset") is not None else None
        self.CAL_LAST_ISO = str(cal.get("last_calibration_iso", "")) or None




class ThicknessRuntimeState(object):
    """Holds all runtime (dynamic) variables that used to be globals."""
    def __init__(self, cfg: ThicknessConfig):
        self.cfg = cfg
        # Shared synchronization / lifecycle
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

        # Sensor buffers
        self.samples = {"TOP": deque(maxlen=10000), "BOTTOM": deque(maxlen=10000)}
        self.last_ts = {"TOP": 0.0, "BOTTOM": 0.0}

        # Current measurement state
        self.object_in_measurement = False
        self.valid_measurement_detected = False
        self.start_time = None
        self.sample_count = 0
        self.error_count = 0      # legacy, retained but no longer drives end-of-part
        # Time of the most recent in-range sample. The end-of-part decision is the
        # wall-clock gap between this and the current out-of-range sample.
        self.last_in_range_time = 0.0

        # Current pass series
        self.timestamps_list = []
        self.thickness_values = []

        # History
        self.passes = []

        # UI communication
        self.ui_queue = queue.Queue(maxsize=50)

        # Operator-history: last N part mean errors (signed) for bell-curve/gauge.
        self.err_hist_um = deque(maxlen=60)

        # Live nominal/tolerance (editable from UI)
        self.nominal_lock = threading.Lock()
        self.nominal_thickness_mm = float(cfg.NOMINAL_THICKNESS_MM_DEFAULT)
        self.tol_um = float(cfg.TOL_UM_DEFAULT)

        # UI throttling
        self._last_live_push = 0.0

        # --- Calibration runtime state ---
        # The active linear map  thickness_mm = slope * S + offset  where S = top_v + bot_v.
        # Loaded from JSON on startup; replaced only by a successful 2-gauge fit.
        self.cal_lock = threading.Lock()
        self.slope = (float(cfg.CAL_SLOPE) if cfg.CAL_SLOPE is not None else None)
        self.offset = (float(cfg.CAL_OFFSET) if cfg.CAL_OFFSET is not None else None)
        self.S1_mm = (float(cfg.CAL_S1_MM) if cfg.CAL_S1_MM is not None else None)
        self.S2_mm = (float(cfg.CAL_S2_MM) if cfg.CAL_S2_MM is not None else None)
        self.cal_last_iso = cfg.CAL_LAST_ISO
        self.parts_since_cal = 0
        # Collection state used while a calibration gauge sits between the sensors.
        self.cal_collecting = False
        self.cal_buffer = []

    def reset_pass(self):
        self.object_in_measurement = False
        self.valid_measurement_detected = False
        self.start_time = None
        self.sample_count = 0
        self.error_count = 0
        self.last_in_range_time = 0.0
        self.timestamps_list.clear()
        self.thickness_values.clear()

    def snapshot_nominal_tol(self):
        with self.nominal_lock:
            return float(self.nominal_thickness_mm), float(self.tol_um)

    # --- Calibration helpers ---
    def is_calibration_valid(self):
        """Valid only when slope/offset come from a successful 2-gauge fit AND the
        parts-since-cal counter has not yet hit its limit."""
        with self.cal_lock:
            if self.slope is None or self.offset is None:
                return False
            if self.parts_since_cal >= int(self.cfg.CAL_MAX_PARTS_BEFORE_RECAL):
                return False
            return True

    def compute_thickness(self, sum_v_mm):
        """Apply the active two-gauge linearization. Returns None if not calibrated."""
        with self.cal_lock:
            slope, offset = self.slope, self.offset
        if slope is None or offset is None:
            return None
        return slope * sum_v_mm + offset

    def set_calibration(self, slope, offset, S1_mm, S2_mm, last_iso):
        with self.cal_lock:
            self.slope = float(slope)
            self.offset = float(offset)
            self.S1_mm = float(S1_mm)
            self.S2_mm = float(S2_mm)
            self.cal_last_iso = str(last_iso) if last_iso else None
            self.parts_since_cal = 0

    def invalidate_calibration(self):
        with self.cal_lock:
            self.slope = None
            self.offset = None

    def increment_parts_since_cal(self):
        with self.cal_lock:
            self.parts_since_cal += 1
            return int(self.parts_since_cal)

    def cal_begin_collect(self):
        with self.lock:
            self.cal_buffer = []
            self.cal_collecting = True

    def cal_add_sample(self, sum_v_mm):
        self.cal_buffer.append(float(sum_v_mm))

    def cal_end_collect(self):
        with self.lock:
            self.cal_collecting = False
            data = list(self.cal_buffer)
            self.cal_buffer = []
        return data


# =========================
# Reader + Processor (Threads)
# =========================

def setup_socket(port: int, timeout_sec: float):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("", port))
    s.settimeout(timeout_sec)
    return s


class SensorReader(object):
    def __init__(self, cfg: ThicknessConfig, state: ThicknessRuntimeState, name: str, port: int):
        self.cfg = cfg
        self.state = state
        self.name = name
        self.port = int(port)
        self.thread = threading.Thread(target=self.run, daemon=True)
        # Hardware-timestamp tracking. The sensor's ts_ns counter resets every
        # second, so we anchor it to host time once and count one-second wraps.
        # The result is monotonic wall-clock time derived from the sensor itself
        # — no jitter even when packets arrive in a burst.
        self._ts_anchor = None   # host wall-time corresponding to wraps=0, ts_ns=0
        self._ts_wraps = 0
        self._ts_last_ns = -1

    def start(self):
        self.thread.start()

    def join(self, timeout=None):
        self.thread.join(timeout=timeout)

    def run(self):
        sock = setup_socket(self.port, self.cfg.SOCKET_TIMEOUT)
        while not self.state.stop_event.is_set():
            while True:
                try:
                    data, _ = sock.recvfrom(4096)
                    parsed = parse_push_packet(data)
                    if not parsed:
                        continue
                    ts_ns_first, vals_mm = parsed
                    num = len(vals_mm)
                    if num <= 0:
                        continue

                    # Map the sensor's 1-second-wrapping ns counter to monotonic wall time.
                    if self._ts_anchor is None:
                        self._ts_anchor = time.time() - (ts_ns_first / 1e9)
                    elif self._ts_last_ns >= 0 and ts_ns_first < self._ts_last_ns - 500_000_000:
                        # ts went backwards by >0.5s -> one-second wrap
                        self._ts_wraps += 1
                    self._ts_last_ns = ts_ns_first

                    t_first = self._ts_anchor + self._ts_wraps + ts_ns_first / 1e9
                    dt = float(self.cfg.FRAME_INTERVAL_SEC) / num

                    with self.state.lock:
                        for i, v_mm in enumerate(vals_mm):
                            t_samp = t_first + i * dt
                            # Store the raw sensor reading directly. Any constant
                            # offset is absorbed by the two-gauge fit downstream.
                            self.state.samples[self.name].append((t_samp, v_mm))
                        self.state.last_ts[self.name] = t_first + (num - 1) * dt

                except socket.timeout:
                    break

            time.sleep(0.0005)


class ThicknessProcessor(object):
    def __init__(self, cfg: ThicknessConfig, state: ThicknessRuntimeState):
        self.cfg = cfg
        self.state = state
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def join(self, timeout=None):
        self.thread.join(timeout=timeout)

    def run(self):
        while not self.state.stop_event.is_set():
            t_samp = b_samp = None

            with self.state.lock:
                if self.state.samples["TOP"] and self.state.samples["BOTTOM"]:
                    t_t, _ = self.state.samples["TOP"][0]
                    b_t, _ = self.state.samples["BOTTOM"][0]
                    dt = abs(t_t - b_t)

                    if dt <= self.cfg.TIME_SYNC_THRESHOLD:
                        t_samp = self.state.samples["TOP"].popleft()
                        b_samp = self.state.samples["BOTTOM"].popleft()
                    else:
                        if t_t < b_t:
                            self.state.samples["TOP"].popleft()
                        else:
                            self.state.samples["BOTTOM"].popleft()

            if t_samp is None or b_samp is None:
                time.sleep(0.0005)
                continue

            t_time, top_v_mm = t_samp
            b_time, bot_v_mm = b_samp

            # The raw sum S = top + bot has NO physical meaning on its own.
            # Thickness exists only through the two-gauge linearization.
            S_mm = top_v_mm + bot_v_mm

            # Calibration capture mode: collect S samples for the gauge currently
            # between the sensors. No part/pass logic runs in this mode.
            if self.state.cal_collecting:
                self.state.cal_add_sample(S_mm)
                continue

            # Without a valid two-gauge calibration the processor produces NOTHING.
            # No parts open, no UI live updates, no history entries.
            thickness = self.state.compute_thickness(S_mm)
            if thickness is None or not self.state.is_calibration_valid():
                continue

            # End-of-part is decided by a WALL-CLOCK gap, not by a sample count.
            # Each in-range sample updates last_in_range_time; when the current
            # sample falls out of [THICKNESS_MIN, THICKNESS_MAX], we check how long
            # it has been since we last saw an in-range sample. Crossing the
            # ERROR_GAP_SEC threshold finalizes the part. Brief features (grooves,
            # scratches, single-sample noise) that take the reading out of range
            # for less than the threshold are silently skipped, regardless of how
            # fast the sensor is sampling.
            t_pair = (t_time + b_time) / 2.0

            if self.cfg.THICKNESS_MIN <= thickness <= self.cfg.THICKNESS_MAX:
                self.state.error_count = 0
                self.state.last_in_range_time = t_pair
                if not self.state.object_in_measurement:
                    self.state.object_in_measurement = True
                    self.state.valid_measurement_detected = True
                    self.state.start_time = t_pair

                self.state.sample_count += 1
                t_rel = t_pair - self.state.start_time
                self.state.timestamps_list.append(t_rel)
                self.state.thickness_values.append(thickness)

                # Live UI throttled
                now = time.time()
                if now - self.state._last_live_push >= 1.0 / max(1.0, float(self.cfg.LIVE_UPDATE_HZ)):
                    self.state._last_live_push = now
                    nominal_used, _tol_used = self.state.snapshot_nominal_tol()
                    err_um = (thickness - nominal_used) * 1000.0
                    try:
                        self.state.ui_queue.put_nowait({"type": "live", "thickness_mm": thickness, "err_um": err_um})
                    except queue.Full:
                        pass
            else:
                # Out of range. If we are not in a measurement at all, there is
                # nothing to close — just ignore. If we are mid-part, finalize
                # only when the wall-clock gap since the last in-range sample
                # exceeds ERROR_GAP_SEC.
                if self.state.object_in_measurement:
                    if (t_pair - self.state.last_in_range_time) >= float(self.cfg.ERROR_GAP_SEC):
                        self.process_end_of_measurement()

    def process_end_of_measurement(self):
        # snapshot nominal/tol at end of measurement
        nominal_used, tol_used_um = self.state.snapshot_nominal_tol()

        if self.state.valid_measurement_detected and self.state.sample_count > 0 and self.state.start_time is not None:
            # Trim 15–85%
            t = self.state.timestamps_list
            y = self.state.thickness_values
            m = len(t)
            i_lo = int(m * self.cfg.TRIM_LO)
            i_hi = int(m * self.cfg.TRIM_HI)

            if i_hi - i_lo >= 2:
                t_trim = t[i_lo:i_hi]
                y_trim = y[i_lo:i_hi]
            else:
                t_trim, y_trim = t[:], y[:]

            raw_t = t_trim[:]
            raw_y = y_trim[:]

            # RAW sample count + sample rate (Hz) on trimmed data
            raw_trim_count = len(raw_t)
            if raw_trim_count >= 2:
                raw_trim_duration = max(1e-9, raw_t[-1] - raw_t[0])
                raw_trim_rate_hz = raw_trim_count / raw_trim_duration
            else:
                raw_trim_rate_hz = 0.0

            # AVG: average to N points
            if len(t_trim) > self.cfg.TARGET_POINTS:
                avg_t, avg_y = [], []
                n = len(t_trim)
                for i in range(self.cfg.TARGET_POINTS):
                    s = (i * n) // self.cfg.TARGET_POINTS
                    e = ((i + 1) * n) // self.cfg.TARGET_POINTS
                    if e <= s:
                        continue
                    seg_t = t_trim[s:e]
                    seg_y = y_trim[s:e]
                    avg_t.append(sum(seg_t) / len(seg_t))
                    avg_y.append(sum(seg_y) / len(seg_y))
            else:
                avg_t, avg_y = t_trim[:], y_trim[:]

            mean_thickness = float(np.mean(np.array(y_trim, float))) if len(y_trim) else None
            mean_err_um = (mean_thickness - nominal_used) * 1000.0 if mean_thickness is not None else None

            # P2P on averaged series (µm)
            p2p_um = None
            if len(avg_y) >= 2:
                arr_um = (np.array(avg_y, float) - nominal_used) * 1000.0
                p2p_um = float(np.max(arr_um) - np.min(arr_um))

            # Conicity (µm): between point #19 and #2 (index 18 and 1)
            conicity_um = None
            conicity_signed_um = None
            conicity_delta_mm = None
            conicity_span_mm = None
            if len(avg_y) >= 19:
                err_avg_um = (np.array(avg_y, float) - nominal_used) * 1000.0
                conicity_signed_um = float(err_avg_um[18] - err_avg_um[1])
                conicity_um = abs(conicity_signed_um)
                conicity_delta_mm = float(np.array(avg_y, float)[18] - np.array(avg_y, float)[1])
                conicity_span_mm = float(self.cfg.AXIAL_SPAN_MM)

            ok_nominal = (abs(mean_err_um) <= tol_used_um) if mean_err_um is not None else False
            ok_p2p = (p2p_um is not None and p2p_um <= self.cfg.P2P_THRESH_UM)
            ok_conicity = (conicity_um is not None and conicity_um <= self.cfg.CONICITY_THRESH_UM)
            status = "תקין" if (ok_nominal and ok_p2p and ok_conicity) else "פסול"

            p = {
                "raw_t": raw_t, "raw_y": raw_y,
                "avg_t": avg_t, "avg_y": avg_y,
                "nominal_used": nominal_used,
                "tol_used_um": tol_used_um,
                "mean_thickness": mean_thickness,
                "mean_err_um": mean_err_um,
                "p2p_um": p2p_um,
                "conicity_um": conicity_um,
                "conicity_signed_um": conicity_signed_um,
                "conicity_delta_mm": conicity_delta_mm,
                "conicity_span_mm": conicity_span_mm,
                "conicity_thresh_um": self.cfg.CONICITY_THRESH_UM,
                "status": status,
                "ok_nominal": ok_nominal,
                "ok_p2p": ok_p2p,
                "ok_conicity": ok_conicity,
                "raw_samples": self.state.sample_count,
                "duration": (self.state.timestamps_list[-1] if self.state.timestamps_list else 0.0),
                "raw_trim_count": raw_trim_count,
                "raw_trim_rate_hz": float(raw_trim_rate_hz),
            }

            self.state.passes.append(p)

            # Add mean error to history for gauge bell curve
            if mean_err_um is not None:
                self.state.err_hist_um.append(mean_err_um)

            # Age the calibration: one more measured part since the last 2-gauge fit.
            # Once parts_since_cal hits CAL_MAX_PARTS_BEFORE_RECAL, is_calibration_valid()
            # returns False and the processor stops accepting new parts until recal.
            self.state.increment_parts_since_cal()

            try:
                self.state.ui_queue.put_nowait({"type": "pass", "idx": len(self.state.passes), "pass": p})
            except queue.Full:
                pass

        # reset
        self.state.reset_pass()


# =========================
# Calibration API (standalone — drives the two-gauge linearization)
# =========================
# Operator flow (driven entirely from this app's UI buttons):
#   1. Operator positions the small reference gauge between the sensors (via HMI).
#   2. Operator clicks "מדוד מדיד קטן"  → measure_gauge(cfg, state, "small")
#   3. Operator positions the large gauge.
#   4. Operator clicks "מדוד מדיד גדול" → measure_gauge(cfg, state, "large")
#   5. Once both S1 and S2 are present, fit_and_apply_calibration runs automatically
#      and the persisted JSON gets the new (slope, offset, S1, S2, T1, T2).
def measure_gauge(cfg: "ThicknessConfig", state: "ThicknessRuntimeState",
                  which: str, config_path: str,
                  window_sec: float | None = None):
    """Measure raw sum S = top_v + bot_v for the gauge currently between the sensors.

    `which` must be 'small' (→ stored as S1) or 'large' (→ stored as S2). Collects
    samples for window_sec, validates stability, stores the median on state. If both
    S1 and S2 are now present the fit is computed and applied automatically.
    """
    which = str(which).lower().strip()
    if which not in ("small", "large"):
        return {"ok": False, "which": which, "reason": "which must be 'small' or 'large'",
                "n": 0, "S_median_mm": None, "std_um": None,
                "calibration_applied": False, "slope": None, "offset": None}

    win = float(window_sec) if window_sec else float(cfg.CAL_WINDOW_SEC)
    state.reset_pass()
    state.cal_begin_collect()
    time.sleep(max(0.5, win))
    data = state.cal_end_collect()

    out = {"ok": False, "which": which, "reason": "", "n": len(data),
           "S_median_mm": None, "std_um": None,
           "calibration_applied": False, "slope": None, "offset": None}

    if len(data) < 5:
        out["reason"] = "too few samples (no sensor packets?)"
        return out

    arr = np.array(data, dtype=float)
    S_med = float(np.median(arr))
    std_um = float(np.std(arr) * 1000.0)
    out["S_median_mm"] = S_med
    out["std_um"] = std_um

    if std_um > float(cfg.CAL_STABILITY_MAX_STD_UM):
        out["reason"] = f"unstable: std {std_um:.2f}µm > {cfg.CAL_STABILITY_MAX_STD_UM:.2f}µm"
        return out

    # Stage the measurement on state; attempt the fit if both gauges are present.
    with state.cal_lock:
        if which == "small":
            state.S1_mm = S_med
        else:
            state.S2_mm = S_med
        S1, S2 = state.S1_mm, state.S2_mm

    out["ok"] = True

    if S1 is not None and S2 is not None:
        ok_fit, fit_info = fit_and_apply_calibration(cfg, state, S1, S2, config_path)
        out["calibration_applied"] = ok_fit
        out["slope"] = fit_info.get("slope")
        out["offset"] = fit_info.get("offset")
        out["reason"] = fit_info.get("reason", "")
    else:
        out["reason"] = f"{which} gauge captured; awaiting the other gauge"
    return out


def fit_and_apply_calibration(cfg: "ThicknessConfig", state: "ThicknessRuntimeState",
                              S1_mm: float, S2_mm: float, config_path: str):
    """Two-point fit:  thickness_mm = slope * S + offset
        slope  = (T2 - T1) / (S2 - S1)
        offset = T1 - slope * S1
    Validates slope sign/magnitude, applies live, and persists to JSON.
    """
    info = {"slope": None, "offset": None, "reason": ""}
    T1, T2 = cfg.CAL_T1_MM, cfg.CAL_T2_MM
    if T1 is None or T2 is None:
        info["reason"] = ("known gauge thicknesses not configured "
                          "(set 'calibration.gauge_small_known_mm' and 'gauge_large_known_mm' in JSON)")
        return False, info

    denom = (S2_mm - S1_mm)
    if abs(denom) < 1e-6:
        info["reason"] = "gauge S readings identical (cannot fit)"
        return False, info

    slope = (T2 - T1) / denom
    offset = T1 - slope * S1_mm
    info["slope"], info["offset"] = slope, offset

    if not (cfg.CAL_SLOPE_MIN <= slope <= cfg.CAL_SLOPE_MAX):
        info["reason"] = (f"slope {slope:.4f} out of bounds "
                          f"[{cfg.CAL_SLOPE_MIN}, {cfg.CAL_SLOPE_MAX}]")
        return False, info

    iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    state.set_calibration(slope, offset, S1_mm, S2_mm, iso)
    cfg.CAL_SLOPE = slope
    cfg.CAL_OFFSET = offset
    cfg.CAL_S1_MM = float(S1_mm)
    cfg.CAL_S2_MM = float(S2_mm)
    cfg.CAL_LAST_ISO = iso
    _save_calibration_to_json(config_path, cfg, slope, offset, S1_mm, S2_mm, iso)
    info["reason"] = "ok"
    return True, info


def _save_calibration_to_json(config_path: str, cfg: "ThicknessConfig",
                              slope, offset, S1_mm, S2_mm, iso):
    """Persist the calibration block into the JSON file, preserving everything else.
    Failures are swallowed — a runtime save error must never crash the test app."""
    try:
        try:
            data = load_config_json(config_path)
        except Exception:
            data = {}
        cal = data.get("calibration", {}) or {}
        cal["S1_mm"] = float(S1_mm)
        cal["S2_mm"] = float(S2_mm)
        cal["slope"] = float(slope)
        cal["offset"] = float(offset)
        if cfg.CAL_T1_MM is not None:
            cal["gauge_small_known_mm"] = float(cfg.CAL_T1_MM)
        if cfg.CAL_T2_MM is not None:
            cal["gauge_large_known_mm"] = float(cfg.CAL_T2_MM)
        cal["last_calibration_iso"] = iso
        data["calibration"] = cal
        os.makedirs(os.path.dirname(config_path) or ".", exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# =========================
# PASS סיכום ושמירה
# =========================
# =========================
# UI
# =========================
def build_calibration_panel(parent, cfg: "ThicknessConfig", state: "ThicknessRuntimeState",
                            config_path: str):
    """Build a calibration controls strip into `parent`:
        - Validity banner (green when calibrated, red when not).
        - Two buttons: "מדוד מדיד קטן" / "מדוד מדיד גדול".
        - One-line summary: slope, offset, S1, S2, parts_since_cal.
        - Status line that fills in after each measurement attempt.

    Returns a dict with the widgets so the caller can keep refreshing the validity
    banner via `panel["refresh"]()` from the UI loop.
    """
    frame = tk.Frame(parent, relief="ridge", borderwidth=2)
    frame.pack(fill="x", padx=12, pady=(10, 6))

    # Row 1 — big validity banner.
    banner = tk.Label(frame, text="", font=("Arial", 13, "bold"),
                      padx=8, pady=6, anchor="center")
    banner.pack(fill="x")

    # Row 2 — two big measurement buttons + a status line on the right.
    btn_row = tk.Frame(frame)
    btn_row.pack(fill="x", padx=8, pady=(4, 4))

    status_var = tk.StringVar(value="")

    # Threading discipline: each measurement blocks for ~1 second, so it runs on a
    # worker thread. The buttons get disabled for the duration to prevent double-
    # firing and to make the running state visible.
    def _disable(disabled: bool):
        s = "disabled" if disabled else "normal"
        try:
            btn_small.config(state=s)
            btn_large.config(state=s)
        except Exception:
            pass

    def _trigger(which: str):
        _disable(True)
        status_var.set(f"מודד מדיד {'קטן' if which == 'small' else 'גדול'}…")

        def _worker():
            try:
                res = measure_gauge(cfg, state, which, config_path)
            except Exception as e:
                res = {"ok": False, "reason": f"exception: {e}",
                       "calibration_applied": False}
            try:
                parent.after(0, lambda: _on_done(which, res))
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _on_done(which, res):
        _disable(False)
        if not res.get("ok"):
            status_var.set(f"כשל: {res.get('reason', '')}")
            return
        if res.get("calibration_applied"):
            slope = res.get("slope"); offset = res.get("offset")
            status_var.set(f"כיול הוחל ✓   slope={slope:+.4f}  offset={offset:+.4f} mm")
        else:
            S = res.get("S_median_mm"); std = res.get("std_um")
            status_var.set(f"{'מדיד קטן' if which == 'small' else 'מדיד גדול'} נמדד: "
                           f"S={S:.4f} mm  std={std:.2f} µm   ({res.get('reason', '')})")
        panel["refresh"]()

    btn_small = tk.Button(btn_row, text="מדוד מדיד קטן",
                          font=("Arial", 13, "bold"), width=18, height=2,
                          command=lambda: _trigger("small"))
    btn_small.pack(side="left", padx=(0, 6))
    btn_large = tk.Button(btn_row, text="מדוד מדיד גדול",
                          font=("Arial", 13, "bold"), width=18, height=2,
                          command=lambda: _trigger("large"))
    btn_large.pack(side="left", padx=(0, 12))

    tk.Label(btn_row, textvariable=status_var, font=("Arial", 10), anchor="w",
             justify="left", wraplength=420).pack(side="left", fill="x", expand=True)

    # Row 3 — one-line summary of the active calibration.
    summary_var = tk.StringVar(value="")
    tk.Label(frame, textvariable=summary_var, font=("Consolas", 10),
             anchor="w", justify="left").pack(fill="x", padx=8, pady=(0, 6))

    def _refresh():
        valid = state.is_calibration_valid()
        with state.cal_lock:
            slope = state.slope
            offset = state.offset
            S1 = state.S1_mm
            S2 = state.S2_mm
            iso = state.cal_last_iso
            parts = state.parts_since_cal
        cap = int(cfg.CAL_MAX_PARTS_BEFORE_RECAL)

        if valid:
            banner.config(text=f"כיול תקף ✓   ({parts}/{cap} חלקים)",
                          bg="#cfe9d6", fg="#0f5a1c")
        else:
            if slope is None or offset is None:
                msg = "אין כיול — חובה למדוד את שני המדידים לפני שמתחילים"
            else:
                msg = f"פג תוקף הכיול ({parts}/{cap} חלקים) — נדרשת מדידה מחדש"
            banner.config(text=msg, bg="#f4cccc", fg="#7a1414")

        def _f(v, fmt):
            return fmt.format(v) if v is not None else "—"

        T1 = cfg.CAL_T1_MM; T2 = cfg.CAL_T2_MM
        summary_var.set(
            f"T1={_f(T1, '{:.4f}')} mm  T2={_f(T2, '{:.4f}')} mm   |   "
            f"S1={_f(S1, '{:.4f}')} mm  S2={_f(S2, '{:.4f}')} mm   |   "
            f"slope={_f(slope, '{:+.4f}')}  offset={_f(offset, '{:+.4f}')} mm   |   "
            f"last cal: {iso or '—'}"
        )

    panel = {"frame": frame, "refresh": _refresh,
             "btn_small": btn_small, "btn_large": btn_large}
    _refresh()
    return panel


def make_card(parent, title):
    frame = tk.Frame(parent, relief="ridge", borderwidth=2, padx=10, pady=6)
    title_lbl = tk.Label(frame, text=title, font=("Arial", 10))
    value_lbl = tk.Label(frame, text="—", font=("Arial", 18, "bold"))
    title_lbl.pack()
    value_lbl.pack()
    frame.pack(side="left", padx=6, pady=4)
    return value_lbl



def make_card_with_icon(parent, title, icon_path=None, icon_text=""):
    """
    Create a card with optional PNG icon and/or text/emoji indicator.
    Returns (value_lbl, icon_lbl, icon_label_widget)
    """
    frame = tk.Frame(parent, relief="ridge", borderwidth=2, padx=10, pady=6)
    title_lbl = tk.Label(frame, text=title, font=("Arial", 10))
    title_lbl.pack()

    row = tk.Frame(frame)
    
    # Icon image label (for PNG icons)
    icon_img_lbl = tk.Label(row, text="")
    
    # If icon path provided, try to load it
    if icon_path:
        try:
            ico = _load_icon_png(icon_path, 24)
            if ico:
                # Convert PIL to PhotoImage
                ico_tk = ImageTk.PhotoImage(ico)
                icon_img_lbl.config(image=ico_tk)
                icon_img_lbl.image = ico_tk  # keep reference
        except Exception:
            pass
    
    icon_img_lbl.pack(side="left", padx=(0, 4))
    
    # Text/emoji indicator label
    icon_lbl = tk.Label(row, text=icon_text, font=("Arial", 22, "bold"))
    icon_lbl.pack(side="left", padx=(0, 8))
    
    value_lbl = tk.Label(row, text="—", font=("Arial", 18, "bold"))
    value_lbl.pack(side="left")
    row.pack()

    frame.pack(side="left", padx=6, pady=4)
    return value_lbl, icon_lbl, icon_img_lbl

def start_ui_full(cfg, state, parent=None):

    # Aliases to mimic the previous global-style code (camera-style readability)
    stop_event = state.stop_event
    ui_queue = state.ui_queue
    passes = state.passes
    nominal_lock = state.nominal_lock

    # Runtime nominal/tolerance live values are stored in `state`
    def _get_nominal_tol():
        with nominal_lock:
            return float(state.nominal_thickness_mm), float(state.tol_um)

    def _set_nominal_tol(nominal_mm=None, tol_um=None):
        with nominal_lock:
            if nominal_mm is not None:
                state.nominal_thickness_mm = float(nominal_mm)
            if tol_um is not None:
                state.tol_um = float(tol_um)


    root = parent if parent is not None else tk.Tk()

    toplevel = root.winfo_toplevel()
    if hasattr(toplevel, "title"):
        toplevel.title("Bearing UI — עם Gauge וסימונים")

    # Controls
    ctrl = tk.Frame(root)
    ctrl.pack(fill="x", padx=12, pady=(10, 6))

    tk.Label(ctrl, text="Nominal [mm]:").pack(side="left")
    n0, t0 = _get_nominal_tol()
    nominal_var = tk.StringVar(value=f"{n0:.3f}")
    nominal_entry = tk.Entry(ctrl, textvariable=nominal_var, width=8)
    nominal_entry.pack(side="left", padx=(6, 14))

    tk.Label(ctrl, text="Tol [µm] ±:").pack(side="left")
    n0, t0 = _get_nominal_tol()
    tol_var = tk.StringVar(value=f"{t0:.1f}")
    tol_entry = tk.Entry(ctrl, textvariable=tol_var, width=6)
    tol_entry.pack(side="left", padx=(6, 14))

    def apply_settings():
        try:
            n = float(nominal_var.get().strip())
            t = float(tol_var.get().strip())
            if not (1.0 <= n <= 4.0):
                raise ValueError("Nominal out of expected range")
            if not (0.1 <= t <= 5000.0):
                raise ValueError("Tol out of expected range")
            _set_nominal_tol(nominal_mm=n, tol_um=t)
        except Exception as e:
            messagebox.showerror("Invalid input", f"Cannot apply: {e}")

    tk.Button(ctrl, text="Apply", command=apply_settings).pack(side="left")

    # --- Calibration panel (two-gauge linearization). Mirror of the compact UI's
    # panel — same backing state, so a measurement triggered here is identical to
    # one triggered there.
    cal_panel = build_calibration_panel(root, cfg, state, CONFIG_PATH)

    # --- History (last 10 passes) ---
    def open_history_window():
        """
        Show the last 10 completed measurements (full pass dict) in a separate window.
        Left: list of passes. Right: averaged-curve plot + details.
        """
        top = root.winfo_toplevel()
        win = tk.Toplevel(top)
        win.title("Last 10 measurements")

        # Layout: left list, right plot+details
        left = tk.Frame(win)
        left.pack(side="left", fill="y", padx=(10, 6), pady=10)

        right = tk.Frame(win)
        right.pack(side="right", fill="both", expand=True, padx=(6, 10), pady=10)

        tk.Label(left, text="Measurements (latest 10):").pack(anchor="w")
        lb = tk.Listbox(left, height=14, width=30)
        lb.pack(fill="y", expand=False)

        # --- Plot area (AVG curve) ---
        plot_frame = tk.Frame(right)
        plot_frame.pack(fill="both", expand=True)

        try:
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        except Exception:
            Figure = None
            FigureCanvasTkAgg = None

        if Figure is not None and FigureCanvasTkAgg is not None:
            fig = Figure(figsize=(5, 3.5), dpi=100)
            ax = fig.add_subplot(111)
            canvas = FigureCanvasTkAgg(fig, master=plot_frame)
            canvas.get_tk_widget().pack(fill="both", expand=True)
        else:
            fig, ax, canvas = None, None, None
            tk.Label(plot_frame, text="Matplotlib not available.").pack()

        # --- Details (right bottom) ---
        detail_frame = tk.Frame(right, relief="sunken", borderwidth=2)
        detail_frame.pack(fill="x", padx=(0, 0), pady=(8, 0))

        txt = tk.Text(detail_frame, height=12, wrap="word", state="disabled", font=("Courier", 9))
        txt.pack(fill="both", expand=True)

        def refresh_list(select_last=False):
            lb.delete(0, tk.END)
            with state.lock:
                last10 = passes[-10:]
            for idx, p in enumerate(last10, start=max(1, len(passes) - 9)):
                st = p.get("status", "—")
                mt = p.get("mean_thickness", None)
                line = f"Pass {idx}: {st}"
                if mt is not None:
                    line += f" | mean={mt:.5f}"
                lb.insert(tk.END, line)
            if select_last and lb.size() > 0:
                lb.selection_clear(0, tk.END)
                lb.selection_set(lb.size() - 1)
                lb.event_generate("<<ListboxSelect>>")

        def _update_plot(p):
            if ax is None or fig is None:
                return
            avg_t = np.array(p.get("avg_t", []), float)
            avg_y = np.array(p.get("avg_y", []), float)
            nom = p.get("nominal_used", None)
            tol_um = p.get("tol_used_um", None)
            if len(avg_t) == 0 or nom is None or tol_um is None:
                ax.clear()
                ax.set_title("No data")
                canvas.draw_idle()
                return

            err_um = (avg_y - nom) * 1000.0
            tol_f = float(tol_um)
            ax.clear()
            ax.axhspan(-tol_f, +tol_f, alpha=0.18)
            ax.axhline(0, lw=1.4)
            ax.axhline(+tol_f, lw=1.0, linestyle="--")
            ax.axhline(-tol_f, lw=1.0, linestyle="--")
            ok = np.abs(err_um) <= tol_f
            ax.plot(avg_t, err_um, "-", lw=1.0, alpha=0.35)
            ax.plot(avg_t[ok], err_um[ok], "o", ms=3.5, color="green")
            if np.any(~ok):
                ax.plot(avg_t[~ok], err_um[~ok], "o", ms=3.5, color="red")
            ax.set_title("AVG (20 pts) — error vs nominal")
            ax.set_xlabel("Time [s]")
            ax.set_ylabel("Error [µm]")
            ax.grid(True)
            fig.tight_layout()
            canvas.draw_idle()

        def show_selected(event):
            sel = lb.curselection()
            if not sel:
                return
            i = sel[0]
            with state.lock:
                last10 = passes[-10:]
            if i < 0 or i >= len(last10):
                return
            p = last10[i]
            idx = len(passes) - len(last10) + i + 1

            _update_plot(p)

            # details
            lines = []
            lines.append(f"Pass #{idx}\n")
            lines.append(f"Status: {p.get('status', '—')}\n")
            lines.append(f"Nominal: {p.get('nominal_used', None):.3f} mm | Tol: ±{p.get('tol_used_um', None):.1f} µm\n")
            lines.append(f"Mean: {p.get('mean_thickness', None):.5f} mm\n")
            lines.append(f"Mean error: {p.get('mean_err_um', None):+.2f} µm\n")
            lines.append(f"P2P: {p.get('p2p_um', None):.2f} µm\n")
            lines.append(f"Conicity: {p.get('conicity_um', None):.2f} µm (signed={p.get('conicity_signed_um', None):+.2f})\n")
            lines.append(f"Raw samples (total): {p.get('raw_samples', 0)}\n")
            lines.append(f"Duration: {p.get('duration', 0.0):.3f} s\n")
            lines.append("")
            lines.append(f"Stored curves: raw({len(p.get('raw_y', []))} pts), avg({len(p.get('avg_y', []))} pts)")

            txt.configure(state="normal")
            txt.delete("1.0", tk.END)
            txt.insert("1.0", "".join(lines))
            txt.configure(state="disabled")

            # store last selected for export
            win._last_selected_pass = (idx, p)

        def export_selected_png():
            if ax is None or fig is None:
                messagebox.showinfo("Export", "Matplotlib not available; cannot export plot.")
                return
            item = getattr(win, "_last_selected_pass", None)
            if not item:
                messagebox.showinfo("Export", "Select a measurement first.")
                return
            idx, p = item

            import os, time
            out_dir = os.path.join(os.getcwd(), "thickness_last10_plots")
            os.makedirs(out_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d-%H%M%S")
            out_path = os.path.join(out_dir, f"pass_{idx:03d}_{ts}.png")

            # redraw plot (ensures current pass is shown)
            _update_plot(p)
            try:
                fig.savefig(out_path, dpi=150)
                messagebox.showinfo("Export", f"Saved:{out_path}")
            except Exception as e:
                messagebox.showerror("Export", f"Failed to save:{e}")

        lb.bind("<<ListboxSelect>>", show_selected)

        btns = tk.Frame(left)
        btns.pack(fill="x", pady=(8, 0))
        tk.Button(btns, text="Refresh", command=lambda: refresh_list(select_last=False)).pack(side="left")
        tk.Button(btns, text="Export PNG", command=export_selected_png).pack(side="left", padx=(6, 0))
        tk.Button(btns, text="Close", command=win.destroy).pack(side="right")

        refresh_list(select_last=True)

    tk.Button(ctrl, text="Last 10", command=open_history_window).pack(side="left", padx=(10, 0))

    # Status + live
    status_var = tk.StringVar(value="ממתין לנפילת עצם ראשונה…")
    status_lbl = tk.Label(root, textvariable=status_var, font=("Arial", 26, "bold"))
    status_lbl.pack(padx=12, pady=(4, 2), anchor="w")

    live_var = tk.StringVar(value="עובי נוכחי: --- mm   (Δ --- µm)")
    live_lbl = tk.Label(root, textvariable=live_var, font=("Arial", 18, "bold"))
    live_lbl.pack(padx=12, pady=(2, 8), anchor="w")

    # ===== Cards area (top) =====
    cards = tk.Frame(root)
    cards.pack(fill="x", padx=12, pady=(0, 10))

    lbl_nominal = make_card(cards, "Nominal [mm]")
    lbl_tol = make_card(cards, "Tol [µm] ±")
    lbl_mean = make_card(cards, "Avg.Mean(th) [mm]")
    lbl_delta = make_card(cards, "Δ from nominal [µm]")
    lbl_p2p = make_card(cards, "P2P [µm]")
    lbl_conicity = make_card(cards, "קוניות [µm]")

    # NEW: Cards with icons for parallel and conicity guidance
    lbl_adj_h, lbl_adj_h_icon, lbl_adj_h_img = make_card_with_icon(
        cards, "מקבילות", 
        icon_path="images457/icons/parallel.png",
        icon_text="—"
    )
    lbl_adj_ang, lbl_adj_ang_icon, lbl_adj_ang_img = make_card_with_icon(
        cards, "קוניות",
        icon_path="images457/icons/cone.png", 
        icon_text="—"
    )
    
    # ===== Gauge area =====
    gauge_frame = tk.Frame(root)
    gauge_frame.pack(fill="x", padx=12, pady=(0, 10))
    
    gauge_lbl = tk.Label(gauge_frame, text="")
    gauge_lbl.pack()
    
# Single plot with selectable view (keep all other UI identical)
    mode_frame = tk.Frame(root)
    mode_frame.pack(fill="x", padx=12, pady=(0, 6))
    tk.Label(mode_frame, text="תצוגת גרף:", font=("Arial", 11)).pack(side="left")
    plot_mode = tk.StringVar(value="AVG (single)")

    # Three visible selectors (big buttons)
    rb_kwargs = dict(
        variable=plot_mode,
        indicatoron=0,
        padx=18, pady=10,
        font=("Arial", 12, "bold"),
        width=16,
        relief="raised",
        bd=2
    )
    tk.Radiobutton(mode_frame, text="AVG (single)", value="AVG (single)", **rb_kwargs).pack(side="left", padx=(8, 6))
    tk.Radiobutton(mode_frame, text="RAW (single)", value="RAW (single)", **rb_kwargs).pack(side="left", padx=(0, 6))
    tk.Radiobutton(mode_frame, text="AVG (last 3)", value="AVG (last 3)", **rb_kwargs).pack(side="left")

    fig = Figure(figsize=(7.2, 6.0), dpi=100)
    ax_main = fig.add_subplot(111)

    canvas = FigureCanvasTkAgg(fig, master=root)
    canvas.get_tk_widget().pack(padx=12, pady=(0, 8), fill="both", expand=True)

    # >>> NEW: Cards under RAW plot (sample rate + count)
    raw_stats_frame = tk.Frame(root)
    raw_stats_frame.pack(fill="x", padx=12, pady=(0, 10))

    lbl_raw_rate = make_card(raw_stats_frame, "RAW F [Hz] (1/Cycle-time)")
    lbl_raw_count = make_card(raw_stats_frame, "RAW Samples [#]")

    # keep last rendered pass so switching modes redraws without waiting for a new measurement
    _last_render = {"idx": None, "pass": None}

    def _on_mode_change(*_):
        if _last_render["pass"] is not None:
            render_pass(_last_render["idx"], _last_render["pass"])

    try:
        plot_mode.trace_add("write", _on_mode_change)
    except Exception:
        plot_mode.trace("w", _on_mode_change)

    def render_pass(pass_idx: int, p: dict):
        _last_render["idx"] = pass_idx
        _last_render["pass"] = p
        nominal = float(p["nominal_used"])
        tol = float(p["tol_used_um"])

        avg_t = np.array(p["avg_t"], float) if p["avg_t"] else np.array([], float)
        avg_y = np.array(p["avg_y"], float) if p["avg_y"] else np.array([], float)

        raw_t = np.array(p["raw_t"], float) if p["raw_t"] else np.array([], float)
        raw_y = np.array(p["raw_y"], float) if p["raw_y"] else np.array([], float)

        status_txt = p.get("status", "לא נקבע")
        is_ok = (status_txt == "תקין")

        status_var.set(f"Pass {pass_idx}: {status_txt}")
        status_lbl.config(fg=("green" if is_ok else "red"))

        mean_th = p.get("mean_thickness", None)
        mean_err = p.get("mean_err_um", None)
        p2p = p.get("p2p_um", None)

        con = p.get("conicity_um", None)

        # Update cards (top)
        lbl_nominal.config(text=f"{nominal:.3f}")
        lbl_tol.config(text=f"±{tol:.1f}")

        lbl_mean.config(text=f"{mean_th:.5f}" if mean_th is not None else "—")

        lbl_delta.config(
            text=f"{mean_err:+.2f}" if mean_err is not None else "—",
            fg=("green" if p.get("ok_nominal") else "red")
        )

        lbl_p2p.config(
            text=f"{p2p:.2f}" if p2p is not None else "—",
            fg=("green" if p.get("ok_p2p") else "red")
        )

        lbl_conicity.config(
            text=f"{con:.2f}" if con is not None else "—",
            fg=("green" if p.get("ok_conicity") else "red")
        )

        

        # === Parallel (height) guidance ===
        if mean_err is None or tol is None:
            lbl_adj_h.config(text="—")
            lbl_adj_h_icon.config(text="—")
        else:
            try:
                err_f = float(mean_err)
                tol_f = float(tol)
                if abs(err_f) <= tol_f:
                    lbl_adj_h.config(text="")
                    lbl_adj_h_icon.config(text="👍")
                elif err_f > 0:
                    lbl_adj_h.config(text="")
                    lbl_adj_h_icon.config(text="↓")
                else:
                    lbl_adj_h.config(text="")
                    lbl_adj_h_icon.config(text="↑")
            except Exception:
                lbl_adj_h.config(text="—")
                lbl_adj_h_icon.config(text="—")

        # === Conicity (angle) guidance ===
        c_signed = p.get("conicity_signed_um", None)
        c_thr = p.get("conicity_thresh_um", cfg.CONICITY_THRESH_UM)
        if c_signed is None or c_thr is None:
            lbl_adj_ang.config(text="—")
            lbl_adj_ang_icon.config(text="—")
        else:
            try:
                c_f = float(c_signed)
                thr_f = float(c_thr)
                if abs(c_f) <= thr_f:
                    lbl_adj_ang.config(text="")
                    lbl_adj_ang_icon.config(text="👍")
                elif c_f > 0:
                    lbl_adj_ang.config(text="")
                    lbl_adj_ang_icon.config(text="↻")
                else:
                    lbl_adj_ang.config(text="")
                    lbl_adj_ang_icon.config(text="↺")
            except Exception:
                lbl_adj_ang.config(text="—")
                lbl_adj_ang_icon.config(text="—")

        # === Render Gauge ===
        try:
            gauge_pil = _gauge_pil(
                total_um=20,
                green_half_um=3,
                yellow_um=1,
                tick_um=1,
                pointer_um=mean_err,
                err_hist_um=list(state.err_hist_um),
                size_px=(700, 150)
            )
            gauge_tk = ImageTk.PhotoImage(gauge_pil)
            gauge_lbl.config(image=gauge_tk)
            gauge_lbl.image = gauge_tk  # keep reference
        except Exception as e:
            gauge_lbl.config(text=f"Gauge error: {e}")

# >>> NEW: Update RAW stats cards (under plots)
        raw_rate = p.get("raw_trim_rate_hz", None)
        raw_cnt = p.get("raw_trim_count", None)
        lbl_raw_rate.config(text=f"{raw_rate:,.0f}" if raw_rate is not None else "—")
        lbl_raw_count.config(text=f"{raw_cnt:,d}" if raw_cnt is not None else "—")

        # Errors (absolute vs nominal) in µm
        err_avg = (avg_y - nominal) * 1000.0 if len(avg_y) else np.array([], float)
        err_raw = (raw_y - nominal) * 1000.0 if len(raw_y) else np.array([], float)
        # Plot according to selected mode (single axis)
        mode = plot_mode.get()

        def _plot_avg(ax, t, err, tol_um, title):
            ax.clear()
            ax.axhspan(-tol_um, +tol_um, alpha=0.18)
            ax.axhline(0, lw=1.4)
            ax.axhline(+tol_um, lw=1.0, linestyle="--")
            ax.axhline(-tol_um, lw=1.0, linestyle="--")
            if len(t):
                ok = np.abs(err) <= tol_um
                ax.plot(t, err, "-", lw=1.0, alpha=0.35)
                ax.plot(t[ok], err[ok], "o", ms=3.5, color="green")
                if np.any(~ok):
                    ax.plot(t[~ok], err[~ok], "o", ms=3.5, color="red")
            ax.set_title(title)
            ax.set_ylabel("Error [µm]")
            ax.grid(True)

        def _plot_raw(ax, t, err, tol_um, title):
            ax.clear()
            ax.axhspan(-tol_um, +tol_um, alpha=0.18)
            ax.axhline(0, lw=1.4)
            ax.axhline(+tol_um, lw=1.0, linestyle="--")
            ax.axhline(-tol_um, lw=1.0, linestyle="--")
            if len(t):
                okr = np.abs(err) <= tol_um
                ax.plot(t, err, "-", lw=0.9, alpha=0.35)
                if np.any(~okr):
                    ax.plot(t[~okr], err[~okr], "o", ms=2.8, color="red")
            ax.set_title(title)
            ax.set_xlabel("Time [s]")
            ax.set_ylabel("Error [µm]")
            ax.grid(True)

        if mode == "RAW (single)":
            _plot_raw(
                ax_main,
                raw_t,
                err_raw,
                tol,
                "RAW trimmed (15–85%) — error vs nominal (no averaging in code)",
            )
            shown_err = err_raw

        elif mode == "AVG (last 3)":
            # Average the AVG-traces over the last up to 3 completed passes (including
            # current). Each pass has its own absolute duration (different part traversal
            # times), so we cannot interpolate on absolute seconds — np.interp would
            # clamp every pass past its own t_max to a flat tail at the last value, and
            # the average would show artificial plateaus unrelated to the actual data.
            # Instead, normalize every pass's time axis to [0, 1] ("fraction of the
            # part"), interpolate onto a common normalized grid, average, then map back
            # to the current pass's time axis for display.
            last3 = passes[-3:] if len(passes) >= 1 else []
            if last3 and avg_t.size >= 2:
                N = avg_t.size
                u_common = np.linspace(0.0, 1.0, N)

                ys = []
                for pp in last3:
                    tt = np.asarray(pp.get("avg_t", []), dtype=float)
                    yy = np.asarray(pp.get("avg_y", []), dtype=float)
                    if tt.size < 2 or yy.size != tt.size:
                        continue
                    span = float(tt[-1] - tt[0])
                    if span <= 0.0:
                        continue
                    tt_norm = (tt - tt[0]) / span
                    ys.append(np.interp(u_common, tt_norm, yy))

                if ys:
                    y_mean = np.mean(np.vstack(ys), axis=0)
                    err3 = (y_mean - nominal) * 1000.0
                    # Map normalized axis back to the current pass's actual time range
                    # so the x-axis stays in seconds and is consistent with the other modes.
                    t_disp = avg_t[0] + u_common * float(avg_t[-1] - avg_t[0])
                    _plot_avg(
                        ax_main,
                        t_disp,
                        err3,
                        tol,
                        f"AVG (last {len(ys)} passes, normalized) — error vs nominal",
                    )
                    shown_err = err3
                else:
                    ax_main.clear()
                    ax_main.set_title("AVG (last 3 passes) — no data")
                    shown_err = np.array([])
            else:
                ax_main.clear()
                ax_main.set_title("AVG (last 3 passes) — no data")
                shown_err = np.array([])

        else:
            _plot_avg(
                ax_main,
                avg_t,
                err_avg,
                tol,
                "AVG (20 points) — error vs nominal",
            )
            shown_err = err_avg


        fig.tight_layout()
        canvas.draw_idle()

    def poll_queue():
        if state.stop_event.is_set():
            root.destroy()
            return

        last_pass = None
        last_live = None

        try:
            while True:
                msg = ui_queue.get_nowait()
                if isinstance(msg, dict) and msg.get("type") == "live":
                    last_live = msg
                elif isinstance(msg, dict) and msg.get("type") == "pass":
                    last_pass = msg
        except queue.Empty:
            pass

        # עדכון רובריקת live
        if last_live is not None:
            th = last_live.get("thickness_mm", None)
            er = last_live.get("err_um", None)
            if th is not None and er is not None:
                live_var.set(f"עובי נוכחי: {th:.5f} mm   (Δ {er:+.2f} µm)")

        # עדכון PASS מלא (גרפים + סטטוס + שאר המדדים)
        if last_pass is not None:
            render_pass(last_pass["idx"], last_pass["pass"])

        # Keep the calibration banner live so parts_since_cal / validity stay current.
        try:
            cal_panel["refresh"]()
        except Exception:
            pass

        root.after(50, poll_queue)

    def on_close():
        stop_event.set()
        root.destroy()

    # When embedding, `root` may be a Frame. Attach close handler to the actual toplevel window.
    try:
        top = root.winfo_toplevel()
        top.protocol("WM_DELETE_WINDOW", on_close)
    except Exception:
        pass
    poll_queue()
    if parent is None:
        root.mainloop()



def start_ui_compact(cfg, state, parent=None):
    """Compact entry screen (like COMPACT) with a Settings button that opens the full UI.

    IMPORTANT: This view does NOT consume ui_queue (so the full UI keeps working normally).
    It simply watches state.passes for the newest completed pass.
    """
    import tkinter as tk
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure

    stop_event = state.stop_event

    root = parent if parent is not None else tk.Tk()
    toplevel = root.winfo_toplevel()
    try:
        toplevel.title("Thickness — Compact")
    except Exception:
        pass

    # --- Calibration panel (two-gauge linearization). This MUST appear and be
    # operable before any measurement is meaningful: until the two reference gauges
    # have been measured, the processor will not produce any thickness values.
    cal_panel = build_calibration_panel(root, cfg, state, CONFIG_PATH)

    # --- Top cards (3 equal boxes) ---
    cards = tk.Frame(root)
    cards.pack(fill="x", padx=12, pady=(10, 6))
    for i in range(3):
        cards.grid_columnconfigure(i, weight=1, uniform="cards")

    def make_box(title):
        f = tk.Frame(cards, relief="ridge", borderwidth=2, padx=10, pady=6)
        tk.Label(f, text=title, font=("Arial", 10)).pack()
        v = tk.Label(f, text="—", font=("Arial", 18, "bold"))
        v.pack()
        return f, v

    box_h, lbl_h = make_box("כוון גובה")
    box_a, lbl_a = make_box("כוון זווית")
    box_m, lbl_m = make_box("עובי ממוצע [mm]")

    box_h.grid(row=0, column=0, sticky="nsew", padx=4)
    box_a.grid(row=0, column=1, sticky="nsew", padx=4)
    box_m.grid(row=0, column=2, sticky="nsew", padx=4)

    # --- Settings button row ---
    btn_bar = tk.Frame(root)
    btn_bar.pack(fill="x", padx=12, pady=(0, 6))

    def open_settings():
        win = tk.Toplevel(toplevel)
        try:
            win.title("Thickness — Full")
        except Exception:
            pass
        # Build the full UI in this toplevel.
        # IMPORTANT: The full UI's default WM_DELETE handler sets state.stop_event.
        # For a settings window we want an independent close, so we override WM_DELETE.
        start_ui_full(cfg, state, parent=win)
        try:
            win.protocol("WM_DELETE_WINDOW", win.destroy)
        except Exception:
            pass

    tk.Button(
        btn_bar,
        text="⚙ הגדרות",
        font=("Arial", 14, "bold"),
        command=open_settings
    ).pack(anchor="e")

    # --- Plot (AVG curve) ---
    fig = Figure(figsize=(7.2, 3.4), dpi=100)
    ax = fig.add_subplot(111)
    ax.set_title("AVG curve")
    ax.set_xlabel("t [s]")
    ax.set_ylabel("Thickness (mm)")
    canvas = FigureCanvasTkAgg(fig, master=root)
    canvas.get_tk_widget().pack(fill="both", expand=True, padx=12, pady=(0, 10))
    canvas.draw()

    last_seen = 0

    def set_icon(lbl, text_):
        try:
            lbl.config(text=text_)
        except Exception:
            pass

    def poll():
        nonlocal last_seen
        if stop_event.is_set():
            try:
                if parent is None:
                    toplevel.destroy()
            except Exception:
                pass
            return

        p = None
        try:
            with state.lock:
                if len(state.passes) > last_seen:
                    last_seen = len(state.passes)
                    p = state.passes[-1]
        except Exception:
            p = None

        if p is not None:
            # mean thickness
            mt = p.get("mean_thickness", None)
            if mt is not None:
                lbl_m.config(text=f"{mt:.5f}")

            # height guidance from mean_err_um vs tol_used_um (already computed)
            try:
                mean_err_um = p.get("mean_err_um", None)
                tol_used_um = p.get("tol_used_um", None)
                if mean_err_um is None or tol_used_um is None:
                    set_icon(lbl_h, "—")
                else:
                    if abs(float(mean_err_um)) <= float(tol_used_um):
                        set_icon(lbl_h, "✓")
                    elif float(mean_err_um) > 0:
                        set_icon(lbl_h, "↓")
                    else:
                        set_icon(lbl_h, "↑")
            except Exception:
                set_icon(lbl_h, "—")

            # angle guidance from conicity (already stored)
            try:
                cd = p.get("conicity_delta_mm", None)
                span = p.get("conicity_span_mm", None)
                if cd is None or not span:
                    set_icon(lbl_a, "—")
                else:
                    import math as _math
                    slope = float(cd) / float(span)
                    theta_deg = -_math.degrees(_math.atan(slope))
                    if abs(theta_deg) < 0.01:
                        set_icon(lbl_a, "✓")
                    elif theta_deg > 0:
                        set_icon(lbl_a, "↑")
                    else:
                        set_icon(lbl_a, "↓")
            except Exception:
                set_icon(lbl_a, "—")

            # plot avg curve (stored)
            try:
                ax.clear()
                ax.set_title("AVG curve")
                ax.set_xlabel("t [s]")
                ax.set_ylabel("Thickness (mm)")
                ax.plot(p.get("avg_t", []), p.get("avg_y", []), marker="o")
                canvas.draw()
            except Exception:
                pass

        # Keep the calibration banner live: parts_since_cal grows even without a
        # button press, and we want the operator to see the counter approaching
        # the recalibration limit.
        try:
            cal_panel["refresh"]()
        except Exception:
            pass

        root.after(100, poll)

    root.after(100, poll)

    if parent is None:
        root.mainloop()


def start_ui(cfg, state, parent=None):
    """Entry point: start with the compact screen."""
    return start_ui_compact(cfg, state, parent)

def main():
    cfg_dict = load_config_json(CONFIG_PATH)
    cfg = ThicknessConfig(cfg_dict)
    state = ThicknessRuntimeState(cfg)

    readers = []
    for name, info in cfg.SENSORS.items():
        r = SensorReader(cfg, state, name, info["port"])
        readers.append(r)
        r.start()

    proc = ThicknessProcessor(cfg, state)
    proc.start()

    try:
        start_ui(cfg, state)
    finally:
        state.stop_event.set()
        for r in readers:
            r.join(timeout=0.5)
        proc.join(timeout=0.5)

        # If UI closed mid-pass, try to finalize once (same behavior as old main)
        if state.object_in_measurement and state.timestamps_list:
            try:
                proc.process_end_of_measurement()
            except Exception:
                pass


if __name__ == "__main__":
    main()
