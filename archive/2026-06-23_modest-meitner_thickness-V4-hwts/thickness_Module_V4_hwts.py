import socket
import struct
import threading
import time
import queue
from collections import deque
import numpy as np
import json
import os
from PIL import Image, ImageDraw, ImageFont
import re
import matplotlib
matplotlib.use("Agg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from pathlib import Path

def _load_icon_png(rel_path: str, size_px: int):

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
# Fonts (single, simple, cached)
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


# -------------------------
# RTL text helpers (Hebrew)
# -------------------------
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
# JSON Machine Config
# =========================
def load_config_json(path: str) -> dict:
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Config JSON not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# =========================
# Helpers: parse PUSH packet (hardware timestamp + measurement counter)
# Datasheet OD5000 (Table 47, p.69) PUSH frame layout:
#   bytes 0..1  : push identifier (0xD) + frame length
#   bytes 2..5  : sensor status
#   bytes 6..9  : hardware time stamp of the FIRST value [ns] (wraps every 1 s)
#   bytes 10..13: measurement counter of the FIRST value (per-sample, +1 each)
#   bytes 14..  : measured values, signed 32-bit big-endian, in nm
# =========================
def parse_push_packet(data: bytes):
    """Return (counter, ts_ns, status, [values_mm]) or None.

    Keeps the device measurement counter and hardware time stamp so the reader
    can build a drop-robust, real-period sample timeline.
    """
    if len(data) < 18:
        return None
    status  = struct.unpack(">I", data[2:6])[0]
    ts_ns   = struct.unpack(">I", data[6:10])[0]
    counter = struct.unpack(">I", data[10:14])[0]
    payload = data[14:]
    if len(payload) < 4 or (len(payload) % 4) != 0:
        return None
    total_vals = len(payload) // 4
    vals_nm = struct.unpack(">" + "i" * total_vals, payload)
    vals_mm = [v / 1_000_000.0 for v in vals_nm]
    return counter, ts_ns, status, vals_mm

# =========================
# Config + Runtime State
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

        self.SENSORS = req("sensors")
        self.SOCKET_TIMEOUT = float(req("socket_timeout_sec"))
        self.FRAME_INTERVAL_SEC = float(req("frame_interval_sec"))
        self.LIVE_UPDATE_HZ = float(req("live_update_hz"))

        self.REFERENCE_HEIGHT_MM = float(req("reference_height_mm"))
        self.SENSOR_DISTANCE_MM = float(req("sensor_distance_mm"))
        self.ERROR_THRESHOLD_MM = float(req("error_threshold_mm"))
        self.AXIAL_SPAN_MM = float(req("axial_span_mm"))

        self.THICKNESS_MIN = float(req("thickness_min_mm"))
        self.THICKNESS_MAX = float(req("thickness_max_mm"))

        self.TARGET_POINTS = int(req("target_points"))
        self.TRIM_LO = float(req("trim_lo"))
        self.TRIM_HI = float(req("trim_hi"))
        self.P2P_THRESH_UM = float(req("p2p_thresh_um"))

        # Conicity threshold [µm] (operator spec): default 5µm unless provided in JSON
        self.CONICITY_THRESH_UM = float(d.get("conicity_thresh_um", 5.0))

        self.NOMINAL_THICKNESS_MM_DEFAULT = float(req("nominal_thickness_mm_default"))
        self.TOL_UM_DEFAULT = float(req("tol_um_default"))

        # --- Two-gauge linear calibration (all optional; defaults = no-op a=1,b=0) ---
        # thickness_corrected = a * thickness_raw + b
        cal = d.get("calibration", {}) or {}
        self.CAL_GAUGE_1_MM = float(cal["gauge_1_known_mm"]) if cal.get("gauge_1_known_mm") is not None else None
        self.CAL_GAUGE_2_MM = float(cal["gauge_2_known_mm"]) if cal.get("gauge_2_known_mm") is not None else None
        self.CAL_WINDOW_SEC = float(cal.get("window_sec", 3.0))
        self.CAL_STABILITY_MAX_STD_UM = float(cal.get("stability_max_std_um", 5.0))
        self.CAL_GAIN_MIN = float(cal.get("gain_min", 0.95))
        self.CAL_GAIN_MAX = float(cal.get("gain_max", 1.05))
        self.CAL_A = float(cal.get("a", 1.0))
        self.CAL_B = float(cal.get("b", 0.0))

        # --- Hardware-timestamp + measurement-counter pairing ---
        # Each sample's time is reconstructed from the device measurement counter
        # (drop-robust) and the real detection period (estimated from the hardware
        # timestamps). A lost UDP packet does not shift the timeline.
        # TOP<->BOTTOM pairing tolerance [sec].
        self.PAIRING_MAX_SKEW_SEC = float(req("pairing_max_skew_sec"))
        # EMA smoothing for the per-sensor sample-period estimate (0 = freeze at nominal).
        self.PAIRING_PERIOD_EMA_ALPHA = float(d.get("pairing_period_ema_alpha", 0.05))
        # If the counter jumps by more than this between packets, treat it as a
        # discontinuity and re-anchor the timeline instead of extrapolating a huge gap.
        self.PAIRING_MAX_DROP_RESYNC = int(d.get("pairing_max_drop_resync", 50))

        # --- Feature-aware measurement (holes / grooves) ---
        # A short region of out-of-band thickness (a through-hole, or a groove deeper than
        # the band) does NOT end the part; the pass stays open and the feature samples are
        # excluded. End-of-part fires only after the out-of-band condition persists longer
        # than END_OF_PART_GAP_SEC. Set it above (max feature length / min part speed).
        self.END_OF_PART_GAP_SEC = float(req("end_of_part_gap_sec"))
        # In-band groove rejection depth [µm -> mm]: a sample whose thickness is below the
        # robust (median) surface baseline by more than this is treated as a surface
        # feature and dropped from the statistics. Must sit ABOVE real surface variation
        # (noise + true conicity) and BELOW the shallowest groove you want to ignore.
        self.FEATURE_DEPTH_MM = float(req("feature_depth_um")) / 1000.0
        # Feature EDGE threshold [µm -> mm]: defines the EXTENT of a confirmed feature.
        # Once a feature is confirmed (a deep core below FEATURE_DEPTH_MM, or a time-gap
        # where the bottom was dropped live), the rejection grows outward over every
        # contiguous sample still below baseline by more than this, i.e. until the surface
        # recovers. This swallows the sloped (chamfered) entry/exit of the feature.
        # Set just ABOVE real surface variation (noise + conicity), e.g. ~30 µm.
        self.FEATURE_EDGE_MM = float(d.get("feature_edge_um", 30.0)) / 1000.0
        # Minimum surviving samples required before computing the median baseline.
        self.FEATURE_MIN_BASELINE_SAMPLES = int(d.get("feature_min_baseline_samples", 10))

class ThicknessRuntimeState(object):
    def __init__(self, cfg: ThicknessConfig):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

        self.samples = {"TOP": deque(maxlen=10000), "BOTTOM": deque(maxlen=10000)}
        self.last_ts = {"TOP": 0.0, "BOTTOM": 0.0}

        # --- Pairing diagnostics (per sensor) ---
        self.hw_dropped = {"TOP": 0, "BOTTOM": 0}   # samples missing per counter gaps
        self.hw_resyncs = {"TOP": 0, "BOTTOM": 0}   # timeline re-anchors (large discontinuities)
        self.hw_last_counter = {"TOP": None, "BOTTOM": None}

        self.object_in_measurement = False
        self.valid_measurement_detected = False
        self.start_time = None
        self.sample_count = 0
        self.error_count = 0
        self.error_run_start_t = None   # rel-time when the current out-of-band run began

        self.timestamps_list = []
        self.thickness_values = []

        self.passes = []
        self.ui_queue = queue.Queue(maxsize=50)

        # Operator-history: last N part mean errors (signed) for bell-curve/gauge.
        self.err_hist_um = deque(maxlen=60)

        self.nominal_lock = threading.Lock()
        self.nominal_thickness_mm = float(cfg.NOMINAL_THICKNESS_MM_DEFAULT)
        self.tol_um = float(cfg.TOL_UM_DEFAULT)

        self._last_live_push = 0.0

        # --- Calibration runtime state ---
        self.cal_lock = threading.Lock()
        self.cal_a = float(cfg.CAL_A)          # active gain  (applied to every measurement)
        self.cal_b = float(cfg.CAL_B)          # active offset [mm]
        self.cal_collecting = False            # when True, run() captures raw thickness instead of processing parts
        self.cal_buffer = []                   # raw thickness samples gathered during a gauge measurement

    def reset_pass(self):
        self.object_in_measurement = False
        self.valid_measurement_detected = False
        self.start_time = None
        self.sample_count = 0
        self.error_count = 0
        self.error_run_start_t = None
        self.timestamps_list.clear()
        self.thickness_values.clear()

    def snapshot_nominal_tol(self):
        with self.nominal_lock:
            return float(self.nominal_thickness_mm), float(self.tol_um)

    # --- Calibration helpers ---
    def apply_calibration(self, thk_raw_mm):
        """Apply the active linear calibration to a raw thickness sample."""
        with self.cal_lock:
            a, b = self.cal_a, self.cal_b
        return a * thk_raw_mm + b

    def set_calibration(self, a, b):
        with self.cal_lock:
            self.cal_a = float(a)
            self.cal_b = float(b)

    def cal_begin_collect(self):
        with self.lock:
            self.cal_buffer = []
            self.cal_collecting = True

    def cal_add_sample(self, v_mm):
        self.cal_buffer.append(float(v_mm))

    def cal_end_collect(self):
        with self.lock:
            self.cal_collecting = False
            data = list(self.cal_buffer)
            self.cal_buffer = []
        return data

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

        # --- Hardware-timestamp + measurement-counter per-sensor timeline state ---
        self._hw_anchor_counter = None                     # device counter at the anchor sample
        self._hw_anchor_wall = 0.0                         # host wall-time assigned to the anchor
        self._period_sec = float(cfg.FRAME_INTERVAL_SEC)   # estimated sample period (refined from hw ts)
        self._prev_counter = None
        self._prev_ts_ns = None
        self._prev_num = 0

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
                    self._handle_packet(data)
                except socket.timeout:
                    break
            time.sleep(0.0005)

    # ------------------------------------------------------------------
    # Reconstruct each sample's time from the device measurement counter
    # (drop-robust) and the real detection period (estimated from the
    # hardware timestamps). A lost UDP packet no longer shifts this sensor's
    # timeline permanently — the counter carries the true sample index, so the
    # gap is bridged correctly instead of skewing.
    # ------------------------------------------------------------------
    def _handle_packet(self, data):
        parsed = parse_push_packet(data)
        if not parsed:
            return
        counter, ts_ns, _status, vals_mm = parsed
        num = len(vals_mm)
        if num <= 0:
            return

        now = time.time()
        interval = float(self.cfg.FRAME_INTERVAL_SEC)
        nominal_period = interval / max(1, num)

        if self._hw_anchor_counter is None:
            # First packet: establish the anchor.
            self._anchor(counter, ts_ns, num, now, nominal_period)
        else:
            dcount = (counter - self._prev_counter) & 0xFFFFFFFF
            dropped = dcount - self._prev_num   # samples missing since the previous packet
            if dcount == 0 or dropped < 0 or dropped > int(self.cfg.PAIRING_MAX_DROP_RESYNC):
                # Duplicate, reordered, or a gap too large to bridge: re-anchor rather
                # than extrapolate a bogus gap. NOTE: the test is on DROPPED samples, not
                # on dcount itself — dcount normally equals the packet's sample count (num),
                # which can legitimately be large, so testing dcount would re-anchor every
                # packet and destroy the timeline.
                with self.state.lock:
                    self.state.hw_resyncs[self.name] += 1
                self._anchor(counter, ts_ns, num, now, nominal_period)
            else:
                if dropped > 0:
                    with self.state.lock:
                        self.state.hw_dropped[self.name] += int(dropped)
                # Refine the period estimate from the real hardware timestamps
                # (ts_ns wraps every 1 s, so take the delta modulo 1e9).
                dts_ns = (ts_ns - self._prev_ts_ns) % 1_000_000_000
                p = (dts_ns / 1e9) / dcount
                if 0.2 * nominal_period <= p <= 5.0 * nominal_period:
                    a = float(self.cfg.PAIRING_PERIOD_EMA_ALPHA)
                    self._period_sec = (1.0 - a) * self._period_sec + a * p

        # Emit samples for this packet, timed by absolute counter offset.
        with self.state.lock:
            for j, v_mm in enumerate(vals_mm):
                c = (counter + j) & 0xFFFFFFFF
                idx = (c - self._hw_anchor_counter) & 0xFFFFFFFF
                if idx > 0x7FFFFFFF:        # counter wrapped backwards -> signed
                    idx -= 0x100000000
                t = self._hw_anchor_wall + idx * self._period_sec
                abs_mm = self.cfg.REFERENCE_HEIGHT_MM + v_mm
                self.state.samples[self.name].append((t, abs_mm))
            self.state.last_ts[self.name] = t
            self.state.hw_last_counter[self.name] = (counter + num - 1) & 0xFFFFFFFF

        self._prev_counter = counter
        self._prev_ts_ns = ts_ns
        self._prev_num = num

    def _anchor(self, counter, ts_ns, num, now, nominal_period):
        """(Re)set this sensor's reconstructed timeline to the current packet."""
        self._hw_anchor_counter = counter
        # The first value of a packet was collected in the previous frame window.
        self._hw_anchor_wall = now - float(self.cfg.FRAME_INTERVAL_SEC)
        self._period_sec = nominal_period
        self._prev_counter = counter
        self._prev_ts_ns = ts_ns
        self._prev_num = num

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

                    if dt <= self.cfg.PAIRING_MAX_SKEW_SEC:
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

            t_time, top_abs_mm = t_samp
            b_time, bot_abs_mm = b_samp

            t_pair = (t_time + b_time) / 2.0
            both_empty = (top_abs_mm >= self.cfg.ERROR_THRESHOLD_MM and bot_abs_mm >= self.cfg.ERROR_THRESHOLD_MM)
            thickness_raw = self.cfg.SENSOR_DISTANCE_MM - (top_abs_mm + bot_abs_mm)

            # Calibration capture mode: gather raw thickness, skip all part/pass logic
            # (so no verdict is produced to the PLC while a gauge is being measured).
            if self.state.cal_collecting:
                if not both_empty:
                    self.state.cal_add_sample(thickness_raw)
                continue

            # Apply the active linear calibration, then classify in-band vs out-of-band.
            thickness = self.state.apply_calibration(thickness_raw)
            in_band = (not both_empty) and (self.cfg.THICKNESS_MIN <= thickness <= self.cfg.THICKNESS_MAX)

            # An out-of-band sample is EITHER inside a surface feature (through-hole /
            # deep groove) OR the real end of the part. We tell them apart by TIME:
            # a short out-of-band run is a feature (skip it, keep the pass open); only a
            # run longer than END_OF_PART_GAP_SEC ends the part. In-band samples on the
            # uniform surface are the only ones accumulated.
            if in_band:
                self.state.error_count = 0
                self.state.error_run_start_t = None
                self._accumulate_sample(t_pair, thickness)
            else:
                rel_t = (t_pair - self.state.start_time) if self.state.start_time is not None else 0.0
                if self.state.error_count == 0:
                    self.state.error_run_start_t = rel_t
                self.state.error_count += 1
                if (self.state.object_in_measurement
                        and self.state.error_run_start_t is not None
                        and (rel_t - self.state.error_run_start_t) >= self.cfg.END_OF_PART_GAP_SEC):
                    self.process_end_of_measurement()

    def _accumulate_sample(self, t_pair, thickness):
        """Record one accepted (uniform-surface) sample and emit a throttled live update."""
        if not self.state.object_in_measurement:
            self.state.object_in_measurement = True
            self.state.valid_measurement_detected = True
            self.state.start_time = t_pair

        self.state.sample_count += 1
        t_rel = t_pair - self.state.start_time
        self.state.timestamps_list.append(t_rel)
        self.state.thickness_values.append(thickness)

        now = time.time()
        if now - self.state._last_live_push >= 1.0 / max(1.0, float(self.cfg.LIVE_UPDATE_HZ)):
            self.state._last_live_push = now
            nominal_used, _ = self.state.snapshot_nominal_tol()
            err_um = (thickness - nominal_used) * 1000.0
            try:
                self.state.ui_queue.put_nowait({"type": "live", "thickness_mm": thickness, "err_um": err_um})
            except queue.Full:
                pass

    def process_end_of_measurement(self):
        nominal_used, tol_used_um = self.state.snapshot_nominal_tol()

        if self.state.valid_measurement_detected and self.state.sample_count > 0 and self.state.start_time is not None:
            t = self.state.timestamps_list
            y = self.state.thickness_values

            # ---- Post-hoc feature rejection (handles chamfered grooves) ----
            # Through-holes / deep groove bottoms were already excluded live (out-of-band),
            # leaving a TIME-GAP in the series there. What remains can still contain the
            # sloped (chamfered) entry/exit of a feature and shallow in-band groove bottoms.
            # Strategy: scan contiguous runs of samples that sit below the surface baseline
            # by more than FEATURE_EDGE_MM ("suspect"). Reject a run only if it is CONFIRMED
            # as a real feature — either it contains a deep core (below FEATURE_DEPTH_MM) or
            # it is adjacent to a time-gap (the dropped feature bottom). The run is rejected
            # in full, so the whole chamfer (down to where the surface recovers) is removed,
            # whatever its length. Genuine shallow surface waviness (no core, no gap) is kept.
            if (self.cfg.FEATURE_DEPTH_MM > 0
                    and len(y) >= self.cfg.FEATURE_MIN_BASELINE_SAMPLES):
                yv = np.array(y, float)
                baseline = float(np.median(yv))
                edge = self.cfg.FEATURE_EDGE_MM
                depth = self.cfg.FEATURE_DEPTH_MM
                n = len(y)
                diffs = [t[k + 1] - t[k] for k in range(n - 1)]
                nominal_dt = float(np.median(diffs)) if diffs else 0.0
                gap_thresh = max(4.0 * nominal_dt, 1e-9)
                gap_after = [diffs[k] > gap_thresh for k in range(n - 1)]   # gap between k and k+1
                suspect = yv < (baseline - edge)
                core = yv < (baseline - depth)
                reject = [False] * n
                k = 0
                while k < n:
                    if not suspect[k]:
                        k += 1
                        continue
                    j = k
                    while j + 1 < n and suspect[j + 1]:
                        j += 1
                    has_core = bool(np.any(core[k:j + 1]))
                    gap_before = (k > 0 and gap_after[k - 1])
                    gap_inside = any(gap_after[p] for p in range(k, j))
                    gap_after_run = (j < n - 1 and gap_after[j])
                    if has_core or gap_before or gap_inside or gap_after_run:
                        for p in range(k, j + 1):
                            reject[p] = True
                    k = j + 1
                kept = [(t[p], y[p]) for p in range(n) if not reject[p]]
                if len(kept) >= 2:
                    t = [p[0] for p in kept]
                    y = [p[1] for p in kept]

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

            raw_trim_count = len(raw_t)
            if raw_trim_count >= 2:
                raw_trim_duration = max(1e-9, raw_t[-1] - raw_t[0])
                raw_trim_rate_hz = raw_trim_count / raw_trim_duration
            else:
                raw_trim_rate_hz = 0.0

            if len(t_trim) > self.cfg.TARGET_POINTS:
                # Time-based bucketing: split the trimmed time span into TARGET_POINTS
                # equal-time bins. After feature removal the series has time gaps, so
                # index-based bins would mis-place the first/last points used by conicity;
                # binning by actual time keeps each averaged point at its true axial
                # position. Empty bins are skipped.
                avg_t, avg_y = [], []
                t0, t1 = t_trim[0], t_trim[-1]
                span = max(1e-9, t1 - t0)
                bins_t = [[] for _ in range(self.cfg.TARGET_POINTS)]
                bins_y = [[] for _ in range(self.cfg.TARGET_POINTS)]
                for tt, yy in zip(t_trim, y_trim):
                    bi = int((tt - t0) / span * self.cfg.TARGET_POINTS)
                    if bi >= self.cfg.TARGET_POINTS:
                        bi = self.cfg.TARGET_POINTS - 1
                    bins_t[bi].append(tt)
                    bins_y[bi].append(yy)
                for bt, by in zip(bins_t, bins_y):
                    if bt:
                        avg_t.append(sum(bt) / len(bt))
                        avg_y.append(sum(by) / len(by))
            else:
                avg_t, avg_y = t_trim[:], y_trim[:]

            mean_thickness = float(np.mean(np.array(y_trim, float))) if len(y_trim) else None
            mean_err_um = (mean_thickness - nominal_used) * 1000.0 if mean_thickness is not None else None

            p2p_um = None
            if len(avg_y) >= 2:
                arr_um = (np.array(avg_y, float) - nominal_used) * 1000.0
                p2p_um = float(np.max(arr_um) - np.min(arr_um))

            conicity_um = None
            conicity_signed_um = None
            conicity_delta_mm = None
            conicity_span_mm = None
            # Conicity (operator definition):
            #   mean of first 3 averaged points minus mean of last 3 averaged points (signed)
            #   sign: (last_mean - first_mean) in µm
            if len(avg_y) >= 6:
                first_mean_mm = float(np.mean(np.array(avg_y[:3], float)))
                last_mean_mm  = float(np.mean(np.array(avg_y[-3:], float)))
                conicity_delta_mm = float(last_mean_mm - first_mean_mm)
                conicity_signed_um = conicity_delta_mm * 1000.0
                conicity_um = abs(conicity_signed_um)
                conicity_span_mm = float(self.cfg.AXIAL_SPAN_MM)

            ok_nominal = (abs(mean_err_um) <= tol_used_um) if mean_err_um is not None else False
            ok_p2p = (p2p_um is not None and p2p_um <= self.cfg.P2P_THRESH_UM)

            # Conicity OK if within threshold (or if conicity couldn't be computed for this pass)
            if conicity_signed_um is None:
                ok_conicity = True
            else:
                ok_conicity = (abs(conicity_signed_um) <= self.cfg.CONICITY_THRESH_UM)

            status = "תקין" if (ok_nominal and ok_conicity) else "פסול"
            overall_ok = (status == "תקין")

            fail_reason = None
            if not overall_ok:
                if mean_err_um is not None and tol_used_um is not None and abs(mean_err_um) > tol_used_um:
                    fail_reason = "גבוה מהנומינלי" if mean_err_um > 0 else "נמוך מהנומינלי"
                elif conicity_signed_um is not None and abs(conicity_signed_um) > float(self.cfg.CONICITY_THRESH_UM):
                    fail_reason = "קוניות גבוהה"
                else:
                    fail_reason = None

            # PLC verdict for register 5 (the main app sends it via RegsToPlc; this module never writes the PLC):
            #   1 = OK, 2 = thickness below nominal, 3 = thickness above nominal, 4 = conicity too high
            if overall_ok:
                plc_code = 1
            elif mean_err_um is not None and tol_used_um is not None and abs(mean_err_um) > tol_used_um:
                plc_code = 3 if mean_err_um > 0 else 2
            elif conicity_signed_um is not None and abs(conicity_signed_um) > float(self.cfg.CONICITY_THRESH_UM):
                plc_code = 4
            else:
                plc_code = 2

            p = {
                "raw_t": raw_t, "raw_y": raw_y,
                "avg_t": avg_t, "avg_y": avg_y,
                "nominal_used": nominal_used,
                "tol_used_um": tol_used_um,
                "conicity_signed_um": (float(conicity_signed_um) if conicity_signed_um is not None else None),
                "mean_thickness": mean_thickness,
                "mean_err_um": mean_err_um,
                "p2p_um": p2p_um,
                "conicity_um": conicity_um,
                "conicity_delta_mm": conicity_delta_mm,
                "conicity_span_mm": conicity_span_mm,
                "conicity_thresh_um": float(self.cfg.CONICITY_THRESH_UM),
                "status": status,
                "ok_nominal": ok_nominal,
                "ok_p2p": ok_p2p,
                "ok_conicity": ok_conicity,
                "raw_samples": self.state.sample_count,
                "duration": (self.state.timestamps_list[-1] if self.state.timestamps_list else 0.0),
                "raw_trim_count": raw_trim_count,
                "raw_trim_rate_hz": float(raw_trim_rate_hz),
                "overall_ok": overall_ok,
                "fail_reason": fail_reason,
                "plc_code": plc_code,

            }

            self.state.passes.append(p)

            # Keep signed mean error history for operator view (bell curve / gauge).
            try:
                if mean_err_um is not None:
                    self.state.err_hist_um.append(float(mean_err_um))
            except Exception:
                pass

            try:
                self.state.ui_queue.put_nowait({"type": "pass", "idx": len(self.state.passes), "pass": p})
            except queue.Full:
                pass

        self.state.reset_pass()

# =========================
# Headless public API
# =========================
class ThicknessModule(object):
    """Convenience wrapper to start/stop readers+processor without the built-in tkinter UI."""

    def __init__(self, config_path: str | None = None):
        if config_path is None:
            config_path = str(Path(__file__).parent / "config" / "config457_thk_hwts.json")
        self.config_path = config_path
        cfg_dict = load_config_json(config_path)
        self.cfg = ThicknessConfig(cfg_dict)
        self.state = ThicknessRuntimeState(self.cfg)
        self.readers = []
        for name, info in self.cfg.SENSORS.items():
            r = SensorReader(self.cfg, self.state, name, info["port"])
            self.readers.append(r)
        self.proc = ThicknessProcessor(self.cfg, self.state)
        self._last_reported_pass = 0

    def start(self):
        for r in self.readers:
            r.start()
        self.proc.start()

    def stop(self):
        self.state.stop_event.set()
        for r in self.readers:
            r.join(timeout=0.5)
        self.proc.join(timeout=0.5)

        # if closed mid-pass, finalize once
        if self.state.object_in_measurement and self.state.timestamps_list:
            try:
                self.proc.process_end_of_measurement()
            except Exception:
                pass

    def poll_new_result_code(self):
        """Return the PLC verdict code (1/2/3/4) for a newly-finished pass, or None if none since the last call."""
        with self.state.lock:
            n = len(self.state.passes)
            if n <= self._last_reported_pass:
                return None
            self._last_reported_pass = n
            return int(self.state.passes[-1].get("plc_code", 0))

    def get_pairing_diag(self):
        """Pairing health snapshot: dropped-sample counts, timeline re-anchors,
        last device counters, and the current TOP<->BOTTOM head time skew [sec].
        Useful to confirm the hardware-timestamp pairing is staying in sync."""
        with self.state.lock:
            top = self.state.samples["TOP"]
            bot = self.state.samples["BOTTOM"]
            head_skew_sec = None
            if top and bot:
                head_skew_sec = float(top[0][0] - bot[0][0])
            return {
                "dropped": dict(self.state.hw_dropped),
                "resyncs": dict(self.state.hw_resyncs),
                "last_counter": dict(self.state.hw_last_counter),
                "head_skew_sec": head_skew_sec,
                "queued": {"TOP": len(top), "BOTTOM": len(bot)},
            }

    # =====================================================================
    # Calibration API (two precision gauges of known thickness).
    # Motion is driven externally (PLC/HMI); these methods only measure & fit.
    # =====================================================================
    def measure_gauge(self, window_sec: float | None = None):
        """Static measurement of a calibration gauge currently sitting between the sensors.

        Collects RAW thickness (uncalibrated) for a fixed time window, then returns
        a robust summary. Used once per gauge during a calibration sequence.

        Returns dict: {ok, reason, n, raw_median_mm, std_um}.
        """
        win = float(window_sec) if window_sec else float(self.cfg.CAL_WINDOW_SEC)
        self.state.reset_pass()            # make sure we are not mid-part
        self.state.cal_begin_collect()
        time.sleep(max(0.5, win))
        data = self.state.cal_end_collect()

        if len(data) < 5:
            return {"ok": False, "reason": "too few samples", "n": len(data),
                    "raw_median_mm": None, "std_um": None}

        arr = np.array(data, dtype=float)
        med = float(np.median(arr))
        std_um = float(np.std(arr) * 1000.0)
        ok = std_um <= float(self.cfg.CAL_STABILITY_MAX_STD_UM)
        return {"ok": ok, "reason": ("" if ok else "unstable (std too high)"),
                "n": len(data), "raw_median_mm": med, "std_um": std_um}

    def compute_and_apply_calibration(self, raw_g1_mm, raw_g2_mm):
        """Two-point linear fit from the two gauge raw readings.

        Pairs (raw_g1 -> CAL_GAUGE_1_MM) and (raw_g2 -> CAL_GAUGE_2_MM), solves
        thickness_corrected = a*raw + b, validates the gain bounds, and on success
        applies it live and persists it to the config JSON.

        Returns (ok: bool, info: dict{a, b, reason}).
        """
        info = {"a": None, "b": None, "reason": ""}
        k1, k2 = self.cfg.CAL_GAUGE_1_MM, self.cfg.CAL_GAUGE_2_MM
        if k1 is None or k2 is None:
            info["reason"] = "gauge known values not configured"
            return False, info
        if raw_g1_mm is None or raw_g2_mm is None:
            info["reason"] = "missing gauge measurement"
            return False, info

        denom = (raw_g2_mm - raw_g1_mm)
        if abs(denom) < 1e-6:
            info["reason"] = "gauge raw readings identical (cannot fit gain)"
            return False, info

        a = (k2 - k1) / denom
        b = k1 - a * raw_g1_mm
        info["a"], info["b"] = a, b

        if not (self.cfg.CAL_GAIN_MIN <= a <= self.cfg.CAL_GAIN_MAX):
            info["reason"] = f"gain {a:.4f} out of bounds [{self.cfg.CAL_GAIN_MIN}, {self.cfg.CAL_GAIN_MAX}]"
            return False, info

        # Apply live and remember on the loaded cfg, then persist.
        self.state.set_calibration(a, b)
        self.cfg.CAL_A, self.cfg.CAL_B = a, b
        self._save_calibration(a, b, raw_g1_mm, raw_g2_mm)
        info["reason"] = "ok"
        return True, info

    def get_calibration(self):
        """Return the currently active calibration {a, b}."""
        with self.state.cal_lock:
            return {"a": float(self.state.cal_a), "b": float(self.state.cal_b)}

    def _save_calibration(self, a, b, raw_g1, raw_g2):
        """Persist calibration results into the 'calibration' block of the config JSON,
        preserving every other key. Failures here never crash the run."""
        try:
            try:
                data = load_config_json(self.config_path)
            except Exception:
                data = {}
            cal = data.get("calibration", {}) or {}
            cal["a"] = float(a)
            cal["b"] = float(b)
            cal["raw_g1_mm"] = float(raw_g1)
            cal["raw_g2_mm"] = float(raw_g2)
            cal["last_calibration_iso"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            data["calibration"] = cal
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    # --- UI-facing API (image only) ---
    def get_display_image(self, last_n: int = 3, size_px=(420, 550)):
        """Return a single PIL.Image (RGB) representing the full thickness panel."""
        return render_thickness_panel_pil(self.state, last_n=last_n, size_px=size_px)


    # --- UI-facing API (image + 3 metrics as one synced packet) ---
    def get_ui_packet(self, last_n: int = 3, size_px=(420, 550)):
        """Return (image, mean_mm, p2p_um, conicity_signed_um) as a single synced packet."""
        payload = get_display_payload(self.state, last_n=last_n)
        img = render_thickness_panel_pil(self.state, last_n=last_n, size_px=size_px)
        mean_mm = payload.get("mean_thickness_mm", None)
        p2p_um = payload.get("p2p_um", None)
        conicity_um = payload.get("conicity_signed_um", None)
        return img, mean_mm, p2p_um, conicity_um

def _safe_float_list(v):
    try:
        return [float(x) for x in v]
    except Exception:
        return []

def get_display_payload(state: ThicknessRuntimeState, last_n: int = 3):
    """
    What the Main UI needs (minimal, operator-friendly):
      - profile_t: list[float] length ~20  (time axis)
      - profile_th_mm: list[float] length ~20 (thickness axis)
      - mean_thickness_mm
      - mean_err_um
      - status ("תקין"/"פסול") + ok flags
      - nominal_used, tol_used_um
    Strategy:
      - Use the last pass as reference for t-axis.
      - Average last_n passes on thickness vs time (interpolate if needed).
    """
    with state.lock:
        passes = list(state.passes[-max(1, int(last_n)):]) if state.passes else []

    if not passes:
        nominal, tol = state.snapshot_nominal_tol()
        return {
            "profile_t": [],
            "profile_th_mm": [],
            "mean_thickness_mm": None,
            "mean_err_um": None,
            "status": "ממתין",
            "ok_nominal": None,
            "ok_p2p": None,
            "nominal_used": nominal,
            "tol_used_um": tol,
            "conicity_signed_um": None,
        "p2p_um": None,
            "conicity_thresh_um": float(getattr(state.cfg, "CONICITY_THRESH_UM", 5.0)),
        }

    ref = passes[-1]
    t_common = np.asarray(_safe_float_list(ref.get("avg_t", [])), dtype=float)
    if t_common.size == 0:
        # fallback: just show last pass raw (still operator view)
        t_common = np.asarray(_safe_float_list(ref.get("raw_t", [])), dtype=float)

    ys = []
    for p in passes:
        tt = np.asarray(_safe_float_list(p.get("avg_t", [])), dtype=float)
        yy = np.asarray(_safe_float_list(p.get("avg_y", [])), dtype=float)
        if tt.size == 0 or yy.size == 0:
            tt = np.asarray(_safe_float_list(p.get("raw_t", [])), dtype=float)
            yy = np.asarray(_safe_float_list(p.get("raw_y", [])), dtype=float)
        if tt.size == 0 or yy.size == 0 or t_common.size == 0:
            continue
        if tt.size == t_common.size and np.allclose(tt, t_common):
            ys.append(yy)
        else:
            ys.append(np.interp(t_common, tt, yy))

    if ys:
        y_mean = np.mean(np.vstack(ys), axis=0)
    else:
        y_mean = np.asarray(_safe_float_list(ref.get("avg_y", [])), dtype=float)

    nominal_used = float(ref.get("nominal_used", state.snapshot_nominal_tol()[0]))
    tol_used_um = float(ref.get("tol_used_um", state.snapshot_nominal_tol()[1]))
    mean_th = ref.get("mean_thickness", None)
    mean_err_um = ref.get("mean_err_um", None)

    return {
        "profile_t": [float(x) for x in t_common.tolist()] if t_common.size else [],
        "profile_th_mm": [float(x) for x in y_mean.tolist()] if y_mean.size else [],
        "mean_thickness_mm": float(mean_th) if mean_th is not None else None,
        "mean_err_um": float(mean_err_um) if mean_err_um is not None else None,
        "status": str(ref.get("status", "—")),
        "ok_nominal": ref.get("ok_nominal", None),
        "ok_p2p": ref.get("ok_p2p", None),
        "ok_conicity": ref.get("ok_conicity", None),
        "conicity_span_mm": (float(ref.get("conicity_span_mm")) if ref.get("conicity_span_mm") is not None else None),
        "nominal_used": nominal_used,
        "tol_used_um": tol_used_um,
        "conicity_signed_um": float(ref.get("conicity_signed_um")) if ref.get("conicity_signed_um") is not None else None,
        "conicity_thresh_um": float(ref.get("conicity_thresh_um", getattr(state.cfg, "CONICITY_THRESH_UM", 5.0))),
        "p2p_um": (float(ref.get("p2p_um")) if ref.get("p2p_um") is not None else None),
        "overall_ok": ref.get("overall_ok", None),
        "fail_reason": ref.get("fail_reason", None),

    }


# =========================
# Rendering API (UI should receive ONLY images)
# =========================

def _profile_plot_pil(profile_t, profile_th_mm, nominal_mm=None, tol_um=None, size_px=(420, 250)):
    """Render the thickness profile to a PIL RGB image (headless)."""
    w_px, h_px = size_px
    dpi = 100
    fig_w = max(1.0, float(w_px) / dpi)
    fig_h = max(1.0, float(h_px) / dpi)
    fig = Figure(figsize=(fig_w, fig_h), dpi=dpi)
    ax = fig.add_subplot(111)
    ax.set_xlabel("Time [s]")
    ax.set_ylabel("Thickness [mm]")
    ax.grid(True, alpha=0.3)

    if profile_t and profile_th_mm and len(profile_t) == len(profile_th_mm):
        ax.plot(profile_t, profile_th_mm, marker="o", markersize=3, linewidth=1.2)
        if nominal_mm is not None and tol_um is not None:
            try:
                tol_mm = float(tol_um) / 1000.0
                nom = float(nominal_mm)
                ax.axhline(nom, linewidth=1.0)
                ax.axhline(nom + tol_mm, linestyle="--", linewidth=0.8)
                ax.axhline(nom - tol_mm, linestyle="--", linewidth=0.8)
                ax.set_ylim(nom - 1.5 * tol_mm, nom + 1.5 * tol_mm)
            except Exception:
                pass
    else:
        ax.text(0.5, 0.5, "Waiting for first part…", ha="center", va="center", transform=ax.transAxes)

    fig.tight_layout(pad=0.6)
    canvas = FigureCanvasAgg(fig)
    canvas.draw()
    buf = canvas.buffer_rgba()
    im = Image.frombuffer("RGBA", (w_px, h_px), buf, "raw", "RGBA", 0, 1)
    return im.convert("RGB")


def _smooth_counts(counts, sigma_bins=1.2):
    sigma = float(max(0.01, sigma_bins))
    k_half = int(max(2, round(3.0 * sigma)))
    kernel = []
    for k in range(-k_half, k_half + 1):
        kernel.append(float(np.exp(-0.5 * (k / sigma) ** 2)))
    s = float(sum(kernel)) or 1.0
    kernel = [v / s for v in kernel]

    out = []
    n = len(counts)
    for i in range(n):
        acc = 0.0
        for ki, kv in enumerate(kernel):
            jj = i + (ki - k_half)
            if 0 <= jj < n:
                acc += float(counts[jj]) * kv
        out.append(acc)
    return out


def _gauge_pil(total_um=20, green_half_um=3, yellow_um=1, tick_um=1,
               pointer_um=None, err_hist_um=None, size_px=(420, 150)):
    """Draw a gauge (bell curve on top + colored bar + pointer) as a PIL image."""
    w, h = size_px
    im = Image.new("RGB", (w, h), "white")
    dr = ImageDraw.Draw(im)

    pad_x, pad_y = 12, 8
    x0, x1 = pad_x, w - pad_x
    half = float(total_um) / 2.0

    def x_of(um):
        um = max(-half, min(+half, float(um)))
        return x0 + (um + half) / (2.0 * half) * (x1 - x0)

    # layout
    bar_h = 22
    tick_area_h = 26
    gap = 6
    bell_y0 = pad_y
    bell_y1 = max(bell_y0 + 30, h - pad_y - (bar_h + tick_area_h + gap))
    bar_y0 = bell_y1 + gap
    bar_y1 = bar_y0 + bar_h

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
            bell_h = float(bell_y1 - bell_y0)
            dr.line([x_of(-half), bell_y1, x_of(+half), bell_y1], fill=(0, 0, 0), width=1)
            pts = []
            for i, sv in enumerate(smooth):
                um = -half + (i + 0.5) * step
                x = x_of(um)
                y = bell_y1 - (float(sv) / m) * (bell_h - 4)
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


def _compute_big_status_lines(payload):
    """Replicates the *meaning* of the current UI big status logic (Hebrew)."""
    ok = payload.get("overall_ok", None)
    if ok is True:
        return [_rtl_visual("תקין")], (0, 128, 0)
    if ok is None:
        return [], (0, 0, 0)

    # NOK
    lines = []
    err = payload.get("mean_err_um", None)
    tol = payload.get("tol_used_um", None)
    if err is not None and tol is not None:
        try:
            err_f = float(err)
            tol_f = float(tol)
            if abs(err_f) > tol_f:
                d_mm = abs(err_f) / 1000.0
                if err_f > 0:
                    lines.append(f"גבוה מהערך הנומינלי. יש להוריד  {d_mm:.3f} מ''מ")
                else:
                    lines.append(f"נמוך מהנומינלי. תיקון: להעלות {d_mm:.3f} מ''מ")
        except Exception:
            pass

    c_signed = payload.get("conicity_signed_um", None)
    c_thr = payload.get("conicity_thresh_um", None)
    if c_signed is not None and c_thr is not None:
        try:
            c_f = float(c_signed)
            thr_f = float(c_thr)
            if abs(c_f) > thr_f:
                span = payload.get("conicity_span_mm", None)
                if span is not None:
                    span_f = float(span)
                    if span_f > 1e-9:
                        dh_mm = (abs(c_f) * 800.0) / (1000.0 * span_f)
                        action = "להוריד" if c_f > 0 else "להעלות"
                        lines.append(f"קוניות גבוהה. תיקון: {action} {dh_mm:.3f} מ\"מ")
                    else:
                        lines.append("קוניות גבוהה. (שגיאה: axial_span_mm לא תקין)")
                else:
                    lines.append("קוניות גבוהה. (שגיאה: חסר axial_span_mm)")
        except Exception:
            pass

    if not lines:
        reason = payload.get("fail_reason", None)
        reason_str = str(reason) if reason is not None else ""
        msg = "לא תקין" + (f" – {reason_str}" if reason_str else "")
        lines = [msg]

    # Make Hebrew readable in PIL (visual RTL transform)
    lines = [_rtl_visual(l) for l in lines]

    return lines, (200, 0, 0)


def render_thickness_panel_pil(state: ThicknessRuntimeState, last_n: int = 3, size_px=(420, 900)):
    """One-stop renderer: returns a single PIL image representing the whole thickness panel."""
    W, H = size_px
    payload = get_display_payload(state, last_n=last_n)

    # base
    im = Image.new("RGB", (W, H), "white")
    dr = ImageDraw.Draw(im)

    fonts = get_fonts()
    font_label = fonts["label"]
    font_value = fonts["value"]
    font_status = fonts["status"]
    font_symbol = fonts["symbol"]
    font_emoji = fonts["emoji"]

    # ---- top indicators (4 boxes) ----
    pad = 8
    box_h = 80
    gap = 6
    box_w = (W - 2 * pad - 3 * gap) // 4
    y0 = pad
    labels = [_rtl_visual(s) for s in ["מקבילות", "קוניות", "נומינלי", "ממוצע"]]

    err = payload.get("mean_err_um", None)
    tol = payload.get("tol_used_um", None)

    # parallel indicator
    if err is None or tol is None:
        parallel_txt = "—"
    else:
        try:
            err_f = float(err)
            tol_f = float(tol)
            # Use the original UI symbols (Tk shows them fine; we render with Segoe fonts).
            parallel_txt = "👍" if abs(err_f) <= tol_f else ("↓" if err_f > 0 else "↑")
        except Exception:
            parallel_txt = "—"

    # conicity indicator
    c_signed = payload.get("conicity_signed_um", None)
    c_thr = payload.get("conicity_thresh_um", None)
    if c_signed is None or c_thr is None:
        conicity_txt = "—"
    else:
        try:
            c_f = float(c_signed)
            thr_f = float(c_thr)
            # Use the original UI symbols.
            conicity_txt = "👍" if abs(c_f) <= thr_f else ("↻" if c_f > 0 else "↺")
        except Exception:
            conicity_txt = "—"

    nom = payload.get("nominal_used", None)
    mean_th = payload.get("mean_thickness_mm", None)
    nom_txt = f"{float(nom):.3f} mm" if nom is not None else "—"
    mean_txt = f"{float(mean_th):.3f} mm" if mean_th is not None else "—"

    values = [parallel_txt, conicity_txt, nom_txt, mean_txt]

    for i in range(4):
        x = pad + i * (box_w + gap)
        dr.rectangle([x, y0, x + box_w, y0 + box_h], outline=(120, 120, 120), width=1)
        dr.text((x + box_w / 2, y0 + 10), labels[i], fill=(0, 0, 0), anchor="ma", font=font_label)
        vtxt = str(values[i])
        vfont = (font_emoji if vtxt == '👍' else (font_symbol if any(ch in vtxt for ch in ['↓','↑','↻','↺']) else font_value))
        # --- icons for Parallel / Conicity (boxes 0 and 1) ---
        icon_sz = 22

        if i == 0:
            ico = _load_icon_png("images457/icons/parallel.png", icon_sz)
        elif i == 1:
            ico = _load_icon_png("images457/icons/cone.png", icon_sz)
        else:
            ico = None

        if ico is not None:
            ix = int(x + (box_w - icon_sz) / 2)
            iy = int(y0 + 24)  # כוונון גובה בתוך המלבן
            im.paste(ico, (ix, iy), ico)
            value_y = y0 + 52  # מורידים את הטקסט מתחת לאייקון
        else:
            value_y = y0 + 40

        dr.text((x + box_w / 2, value_y), vtxt, fill=(0, 0, 0), anchor="ma", font=vfont)

    # ---- big status ----
    status_lines, status_color = _compute_big_status_lines(payload)
    y = y0 + box_h + 8
    for line in status_lines[:2]:
        dr.text((W - pad, y), line, fill=status_color, anchor="ra", font=font_status)
        y += 16

    # ---- gauge ----
    gauge_y = y0 + box_h + 45
    gauge = _gauge_pil(
        total_um=20,
        green_half_um=3,
        yellow_um=1,
        tick_um=1,
        pointer_um=payload.get("mean_err_um", None),
        err_hist_um=list(getattr(state, "err_hist_um", []) or []),
        size_px=(W - 2 * pad, 150),
    )
    im.paste(gauge, (pad, gauge_y))

    # ---- profile plot ----
    plot_y = gauge_y + 150 + 20
    plot = _profile_plot_pil(
        payload.get("profile_t", []),
        payload.get("profile_th_mm", []),
        nominal_mm=payload.get("nominal_used", None),
        tol_um=payload.get("tol_used_um", None),
        size_px=(W - 2 * pad, 250),
    )
    im.paste(plot, (pad, plot_y))

    # ---- small status line (bottom) ----
    txt_y = plot_y + 250 + 4
    mean_err = payload.get("mean_err_um", None)
    mean_th2 = payload.get("mean_thickness_mm", None)
    status = payload.get("status", "—")
    if mean_th2 is not None and mean_err is not None:
        c = payload.get("conicity_signed_um", None)
        c_txt = f" | cone={float(c):+.1f} µm" if c is not None else ""
        bottom_txt = f"{status} | mean={float(mean_th2):.5f} mm | Δ={float(mean_err):+.1f} µm{c_txt}"
    else:
        bottom_txt = str(status)

    # Bottom status: keep readable for Hebrew
    if _HEB_RE.search(str(bottom_txt)):
        bt = _rtl_visual(str(bottom_txt))
        dr.text((W - pad, txt_y), bt, fill=(0, 0, 0), anchor="ra", font=font_label)
    else:
        dr.text((pad, txt_y), str(bottom_txt), fill=(0, 0, 0), anchor="la", font=font_label)

    return im



