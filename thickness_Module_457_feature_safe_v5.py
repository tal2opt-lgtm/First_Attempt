import socket
import struct
import threading
import time
import queue
from collections import deque
import numpy as np
from scipy.stats import theilslopes
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


# =========================
# Helpers: parse PUSH packet
# =========================
def parse_push_packet(data: bytes):
    if len(data) < 18:
        return None
    payload = data[14:]
    if len(payload) < 4 or (len(payload) % 4) != 0:
        return None
    total_vals = len(payload) // 4
    vals_nm = struct.unpack(">" + "i" * total_vals, payload)
    return [v / 1_000_000.0 for v in vals_nm]  # mm

# =========================
# Config + Runtime State
# =========================
class ThicknessConfig(object):
    """Fixed parameters loaded from JSON (no in-code defaults)."""
    def __init__(self ):
        self._config_path = str(Path(__file__).parent / "config" / "config457_thk.json")
        self._load_from_dict(self._load_json())

    def _load_json(self):
        if not os.path.isfile(self._config_path):
            raise FileNotFoundError(f"Config JSON not found: {self._config_path}")
        with open(self._config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_from_dict(self, d):
        required = (
            "sensors", "socket_timeout_sec", "frame_interval_sec", "time_sync_threshold_sec",
            "live_update_hz", "reference_height_mm", "sensor_distance_mm", "error_threshold_mm",
            "axial_span_mm", "thickness_min_mm", "thickness_max_mm", "max_error_count",
            "target_points", "trim_lo", "trim_hi", "p2p_thresh_um",
            "nominal_thickness_mm_default", "tol_um_default",
        )
        for key in required:
            if key not in d:
                raise KeyError(f"Missing required config key: {key}")

        self.SENSORS = d["sensors"]
        self.SOCKET_TIMEOUT = d["socket_timeout_sec"]
        self.FRAME_INTERVAL_SEC = d["frame_interval_sec"]
        self.TIME_SYNC_THRESHOLD = d["time_sync_threshold_sec"]
        self.LIVE_UPDATE_HZ = d["live_update_hz"]

        self.REFERENCE_HEIGHT_MM = d["reference_height_mm"]
        self.SENSOR_DISTANCE_MM = d["sensor_distance_mm"]
        self.ERROR_THRESHOLD_MM = d["error_threshold_mm"]
        self.AXIAL_SPAN_MM = d["axial_span_mm"]

        self.THICKNESS_MIN = d["thickness_min_mm"]
        self.THICKNESS_MAX = d["thickness_max_mm"]
        self.MAX_ERROR_COUNT = d["max_error_count"]

        self.TARGET_POINTS = d["target_points"]
        self.TRIM_LO = d["trim_lo"]
        self.TRIM_HI = d["trim_hi"]
        self.P2P_THRESH_UM = d["p2p_thresh_um"]

        self.CONICITY_THRESH_UM = d.get("conicity_thresh_um", 5.0)

        self.NOMINAL_THICKNESS_MM_DEFAULT = d["nominal_thickness_mm_default"]
        self.TOL_UM_DEFAULT = d["tol_um_default"]

    def load(self):
        """Reload config from the same path used in __init__."""
        self._load_from_dict(self._load_json())

    def save(self):
        """Save config to JSON file (indented)."""
        d = {
            "schema_version": "1.0",
            "sensors": self.SENSORS,
            "socket_timeout_sec": self.SOCKET_TIMEOUT,
            "frame_interval_sec": self.FRAME_INTERVAL_SEC,
            "time_sync_threshold_sec": self.TIME_SYNC_THRESHOLD,
            "live_update_hz": self.LIVE_UPDATE_HZ,
            "reference_height_mm": self.REFERENCE_HEIGHT_MM,
            "sensor_distance_mm": self.SENSOR_DISTANCE_MM,
            "error_threshold_mm": self.ERROR_THRESHOLD_MM,
            "axial_span_mm": self.AXIAL_SPAN_MM,
            "thickness_min_mm": self.THICKNESS_MIN,
            "thickness_max_mm": self.THICKNESS_MAX,
            "max_error_count": self.MAX_ERROR_COUNT,
            "target_points": self.TARGET_POINTS,
            "trim_lo": self.TRIM_LO,
            "trim_hi": self.TRIM_HI,
            "p2p_thresh_um": self.P2P_THRESH_UM,
            "conicity_thresh_um": self.CONICITY_THRESH_UM,
            "nominal_thickness_mm_default": self.NOMINAL_THICKNESS_MM_DEFAULT,
            "tol_um_default": self.TOL_UM_DEFAULT,
        }
        with open(self._config_path, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)

Cfg = ThicknessConfig()

# Internal, non-JSON timing guard for non-continuous parts.
# Out-of-range samples inside a hole/groove should not immediately close the pass.
# The pass closes only after this much time has elapsed since the last valid bulk sample.
END_OF_PART_GAP_SEC = 0.050

class ThicknessRuntimeState(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

        self.samples = {"TOP": deque(maxlen=10000), "BOTTOM": deque(maxlen=10000)}
        self.last_ts = {"TOP": 0.0, "BOTTOM": 0.0}

        self.object_in_measurement = False
        self.valid_measurement_detected = False
        self.start_time = None
        self.sample_count = 0
        self.error_count = 0
        self.last_in_range_time = 0.0
        self.internal_out_of_range_seen = False

        self.timestamps_list = []
        self.thickness_values = []

        self.passes = []
        self.ui_queue = queue.Queue(maxsize=50)

        # Operator-history: last N part mean errors (signed) for bell-curve/gauge.
        self.err_hist_um = deque(maxlen=60)

        self.nominal_lock = threading.Lock()
        self.nominal_thickness_mm = float(Cfg.NOMINAL_THICKNESS_MM_DEFAULT)
        self.tol_um = float(Cfg.TOL_UM_DEFAULT)

        self._last_live_push = 0.0

        # Rolling index for the LAST_10/ raw-profile PNG snapshots (1..10, wraps).
        self.pass_image_index = 0

    def reset_pass(self):
        self.object_in_measurement = False
        self.valid_measurement_detected = False
        self.start_time = None
        self.sample_count = 0
        self.error_count = 0
        self.last_in_range_time = 0.0
        self.internal_out_of_range_seen = False
        self.timestamps_list.clear()
        self.thickness_values.clear()

    def snapshot_nominal_tol(self):
        with self.nominal_lock:
            return float(self.nominal_thickness_mm), float(self.tol_um)

_LAST_10_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "LAST_10")

def _save_raw_profile_png(p: dict, file_index: int):
    """Save a PNG of the raw (un-binned) thickness profile of a finished pass
    into LAST_10/pass_{file_index:02d}.png. file_index is 1..10 and wraps.
    Error-vs-nominal in µm on the Y axis, with the tolerance band shaded -
    same style as Thck_Meas_V8 RAW view."""
    raw_t = p.get("raw_t") or []
    raw_y = p.get("raw_y") or []
    if not raw_t or not raw_y:
        return

    nominal = float(p.get("nominal_used") or 0.0)
    tol_um = float(p.get("tol_used_um") or 0.0)
    status = p.get("status") or ""
    mean_mm = p.get("mean_thickness")

    os.makedirs(_LAST_10_DIR, exist_ok=True)

    t = np.asarray(raw_t, dtype=float)
    y = np.asarray(raw_y, dtype=float)
    err_um = (y - nominal) * 1000.0

    fig = Figure(figsize=(6.4, 3.6), dpi=100)
    canvas = FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)

    if tol_um > 0:
        ax.axhspan(-tol_um, +tol_um, alpha=0.18, color="green")
        ax.axhline(+tol_um, lw=1.0, linestyle="--", color="gray")
        ax.axhline(-tol_um, lw=1.0, linestyle="--", color="gray")
    ax.axhline(0, lw=1.2, color="black")

    ax.plot(t, err_um, "-", lw=0.9, alpha=0.45, color="blue")
    if tol_um > 0:
        bad = np.abs(err_um) > tol_um
        if np.any(bad):
            ax.plot(t[bad], err_um[bad], "o", ms=2.8, color="red")

    title = f"#{file_index:02d}  {status}"
    if mean_mm is not None:
        title += f"   mean={mean_mm:.4f} mm   (nom={nominal:.4f} mm, tol=±{tol_um:.1f}µm)"
    ax.set_title(title)
    ax.set_xlabel("Time [s]")
    ax.set_ylabel("Error vs nominal [µm]")
    ax.grid(True, alpha=0.4)
    fig.tight_layout()

    out_path = os.path.join(_LAST_10_DIR, f"pass_{file_index:02d}.png")
    try:
        canvas.print_png(out_path)
    except Exception:
        pass
_DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "DEBUG_PASSES")

def save_pass_debug_csv(p: dict):
    try:
        os.makedirs(_DEBUG_DIR, exist_ok=True)

        ts = time.strftime("%Y%m%d_%H%M%S")
        idx = p.get("pass_idx", "unknown")
        path = os.path.join(_DEBUG_DIR, f"pass_{idx}_{ts}.csv")

        avg_y = p.get("avg_y", []) or []
        raw_y = p.get("raw_y", []) or []

        with open(path, "w", encoding="utf-8") as f:
            f.write("key,value\n")
            f.write(f"pass_idx,{idx}\n")
            f.write(f"raw_trim_count,{p.get('raw_trim_count')}\n")
            f.write(f"duration,{p.get('duration')}\n")
            f.write(f"raw_trim_rate_hz,{p.get('raw_trim_rate_hz')}\n")
            f.write(f"mean_thickness,{p.get('mean_thickness')}\n")
            f.write(f"mean_err_um,{p.get('mean_err_um')}\n")
            f.write(f"p2p_um,{p.get('p2p_um')}\n")
            f.write(f"conicity_signed_um,{p.get('conicity_signed_um')}\n")
            f.write(f"conicity_um,{p.get('conicity_um')}\n")
            f.write(f"len_avg_y,{len(avg_y)}\n")
            f.write(f"len_raw_y,{len(raw_y)}\n")

            f.write("\nraw_index,raw_t,raw_y\n")
            for i, (t, y) in enumerate(zip(p.get("raw_t", []), raw_y)):
                f.write(f"{i},{t},{y}\n")

            f.write("\navg_index,avg_t,avg_y\n")
            for i, (t, y) in enumerate(zip(p.get("avg_t", []), avg_y)):
                f.write(f"{i},{t},{y}\n")

    except Exception:
        pass


# =========================
# Feature-safe profile handling
# =========================
def _contiguous_true_runs(mask):
    """Return inclusive (start, end) runs where mask is True."""
    runs = []
    n = len(mask)
    i = 0
    while i < n:
        if not bool(mask[i]):
            i += 1
            continue
        j = i
        while j + 1 < n and bool(mask[j + 1]):
            j += 1
        runs.append((i, j))
        i = j + 1
    return runs


def _detect_feature_runs(y_arr_mm, median_mm, tol_um):
    """Return (n_runs, n_samples) for contiguous groups that look like a real
    feature (groove wall / hole edge), NOT noise spikes. A feature has to:
      - exceed 3x the user's tolerance from the bulk median, AND
      - persist for at least MIN_RUN consecutive samples.
    Single-sample noise spikes never qualify. Display-only - the statistics
    are robust on their own.
    """
    MIN_RUN = 5
    if tol_um is None or tol_um <= 0 or len(y_arr_mm) == 0:
        return 0, 0
    threshold_mm = 2.0 * float(tol_um) / 1000.0
    outlier = (np.abs(y_arr_mm - median_mm) > threshold_mm).tolist()
    runs = _contiguous_true_runs(outlier)
    long_runs = [(s, e) for (s, e) in runs if (e - s + 1) >= MIN_RUN]
    n_samples = sum(e - s + 1 for (s, e) in long_runs)
    return len(long_runs), int(n_samples)


def _split_at_time_gaps(t, y, gap_factor=4.0):
    """Insert NaN before a large time gap so matplotlib does not draw a
    fake line through the feature area. The original x-axis is preserved."""
    if len(t) < 2:
        return list(t), list(y)
    t_arr = np.asarray(t, dtype=float)
    y_arr = np.asarray(y, dtype=float)
    dts = np.diff(t_arr)
    positive = dts[dts > 0]
    if positive.size == 0:
        return list(t_arr), list(y_arr)
    nominal_dt = float(np.median(positive))
    threshold = float(gap_factor) * nominal_dt
    out_t = [float(t_arr[0])]
    out_y = [float(y_arr[0])]
    for i in range(1, len(t_arr)):
        if float(t_arr[i] - t_arr[i - 1]) > threshold:
            out_t.append((float(t_arr[i - 1]) + float(t_arr[i])) / 2.0)
            out_y.append(float("nan"))
        out_t.append(float(t_arr[i]))
        out_y.append(float(y_arr[i]))
    return out_t, out_y


def _count_internal_time_gaps(t_list, gap_factor=4.0):
    """Count large internal time gaps in a valid-only profile.
    These gaps usually mean a through-hole / deep groove was skipped by the
    in-range filter. The first/last edges are not considered here because
    edge trim already handled them.
    """
    if len(t_list) < 4:
        return 0, 0.0
    t = np.asarray(t_list, dtype=float)
    dts = np.diff(t)
    positive = dts[dts > 0]
    if positive.size == 0:
        return 0, 0.0
    nominal_dt = float(np.median(positive))
    if nominal_dt <= 0:
        return 0, 0.0
    threshold = float(gap_factor) * nominal_dt
    gaps = dts > threshold
    return int(np.sum(gaps)), float(np.max(dts[gaps]) if np.any(gaps) else 0.0)

def setup_socket(port: int, timeout_sec: float):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("", port))
    s.settimeout(timeout_sec)
    return s

class SensorReader(object):
    def __init__(self, state: ThicknessRuntimeState, name: str, port: int):
        self.state = state
        self.name = name
        self.port = int(port)
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def join(self, timeout=None):
        self.thread.join(timeout=timeout)

    def run(self):
        sock = setup_socket(self.port, Cfg.SOCKET_TIMEOUT)
        while not self.state.stop_event.is_set():
            while True:
                try:
                    data, _ = sock.recvfrom(4096)
                    vals_mm = parse_push_packet(data)
                    if not vals_mm:
                        continue
                    num = len(vals_mm)
                    if num <= 0:
                        continue

                    # --- Time tagging (burst-robust) ---
                    # Do NOT use time.time() as the packet timestamp (it causes "time clumping" under CPU load).
                    # Instead, advance a per-sensor virtual clock by the configured frame interval.
                    interval = float(Cfg.FRAME_INTERVAL_SEC)
                    dt = interval / num

                    with self.state.lock:
                        last = self.state.last_ts[self.name]
                        if last <= 0.0:
                            # Anchor once to host time, then keep advancing deterministically.
                            last = time.time() - interval
                        for v_mm in vals_mm:
                            last += dt
                            abs_mm = Cfg.REFERENCE_HEIGHT_MM + v_mm
                            self.state.samples[self.name].append((last, abs_mm))
                        self.state.last_ts[self.name] = last
                except socket.timeout:
                    break
            time.sleep(0.0005)

class ThicknessProcessor(object):
    def __init__(self, state: ThicknessRuntimeState, get_job_limits=None):
        self.state = state
        self.get_job_limits = get_job_limits  # optional callable () -> (min_mm, max_mm) | (None, None); used when running under T3
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def join(self, timeout=None):
        self.thread.join(timeout=timeout)

    def _effective_min_max_mm(self):
        # Job override (T3) takes precedence; fall back to config bounds.
        if self.get_job_limits is not None:
            try:
                lo, hi = self.get_job_limits()
                if lo is not None and hi is not None:
                    return float(lo), float(hi)
            except Exception:
                pass
        return float(Cfg.THICKNESS_MIN), float(Cfg.THICKNESS_MAX)

    def run(self):
        while not self.state.stop_event.is_set():
            t_samp = b_samp = None

            with self.state.lock:
                if self.state.samples["TOP"] and self.state.samples["BOTTOM"]:
                    t_t, _ = self.state.samples["TOP"][0]
                    b_t, _ = self.state.samples["BOTTOM"][0]
                    dt = abs(t_t - b_t)

                    if dt <= Cfg.TIME_SYNC_THRESHOLD:
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

            # Unified error / valid logic: ANY sample whose computed thickness falls
            # outside [THICKNESS_MIN, THICKNESS_MAX] counts as an "error" sample.
            # This handles both end-of-part (sensors empty -> thickness out of range)
            # AND short surface features like holes/grooves whose thickness reading is
            # off the bulk surface. Short error runs (< MAX_ERROR_COUNT) reset on the
            # next in-range sample, so a feature is silently skipped and the pass
            # continues with only the bulk-surface samples in the average.
            thickness = Cfg.SENSOR_DISTANCE_MM - (top_abs_mm + bot_abs_mm)

            min_mm, max_mm = self._effective_min_max_mm()
            t_pair = (t_time + b_time) / 2.0

            if min_mm <= thickness <= max_mm:
                self.state.error_count = 0
                if not self.state.object_in_measurement:
                    self.state.object_in_measurement = True
                    self.state.valid_measurement_detected = True
                    self.state.start_time = t_pair

                self.state.last_in_range_time = t_pair
                self.state.sample_count += 1
                t_rel = t_pair - self.state.start_time
                self.state.timestamps_list.append(t_rel)
                self.state.thickness_values.append(thickness)

                now = time.time()
                if now - self.state._last_live_push >= 1.0 / max(1.0, float(Cfg.LIVE_UPDATE_HZ)):
                    self.state._last_live_push = now
                    nominal_used, _ = self.state.snapshot_nominal_tol()
                    err_um = (thickness - nominal_used) * 1000.0
                    try:
                        self.state.ui_queue.put_nowait({"type": "live", "thickness_mm": thickness, "err_um": err_um})
                    except queue.Full:
                        pass
            else:
                # Out of range after a pass has started can be either:
                #   1) an internal hole/groove, or
                #   2) the real end of the part.
                # Do not close immediately by MAX_ERROR_COUNT, because that cuts
                # through-holes into two separate parts. Keep the pass open and close
                # only after a real time gap since the last valid sample.
                if self.state.object_in_measurement:
                    self.state.error_count += 1
                    self.state.internal_out_of_range_seen = True
                    if (t_pair - self.state.last_in_range_time) >= END_OF_PART_GAP_SEC:
                        self.process_end_of_measurement()

    def process_end_of_measurement(self):
        nominal_used, tol_used_um = self.state.snapshot_nominal_tol()

        if self.state.valid_measurement_detected and self.state.sample_count > 0 and self.state.start_time is not None:
            t = self.state.timestamps_list
            y = self.state.thickness_values
            m = len(t)
            i_lo = int(m * Cfg.TRIM_LO)
            i_hi = int(m * Cfg.TRIM_HI)

            if i_hi - i_lo >= 2:
                t_trim = t[i_lo:i_hi]
                y_trim = y[i_lo:i_hi]
            else:
                t_trim, y_trim = t[:], y[:]

            raw_t_full = t_trim[:]
            raw_y_full = y_trim[:]

            # No filtering: robust statistics below (median, P90-P10, Theil-Sen)
            # tolerate groove/hole contamination by construction.
            raw_t = t_trim[:]
            raw_y = y_trim[:]

            # Internal time gap = THICKNESS_MIN/MAX dropped a segment
            # (typical for through-holes). Display-only signal.
            gap_count, max_gap_sec = _count_internal_time_gaps(t_trim)

            raw_trim_count = len(raw_t)
            if raw_trim_count >= 2:
                raw_trim_duration = max(1e-9, raw_t[-1] - raw_t[0])
                raw_trim_rate_hz = raw_trim_count / raw_trim_duration
            else:
                raw_trim_rate_hz = 0.0

            if len(t_trim) > Cfg.TARGET_POINTS:
                avg_t, avg_y = [], []
                n = len(t_trim)
                for i in range(Cfg.TARGET_POINTS):
                    s = (i * n) // Cfg.TARGET_POINTS
                    e = ((i + 1) * n) // Cfg.TARGET_POINTS
                    if e <= s:
                        continue
                    seg_t = t_trim[s:e]
                    seg_y = y_trim[s:e]
                    avg_t.append(sum(seg_t) / len(seg_t))
                    avg_y.append(sum(seg_y) / len(seg_y))
            else:
                avg_t, avg_y = t_trim[:], y_trim[:]

            # Robust statistics on the full trimmed profile. Groove walls and
            # other one-sided contamination do not bias these because each
            # estimator ignores up to ~29% of outliers by construction.
            y_arr = np.asarray(y_trim, dtype=float) if len(y_trim) else np.array([])
            mean_thickness = float(np.median(y_arr)) if y_arr.size else None
            mean_err_um = (mean_thickness - nominal_used) * 1000.0 if mean_thickness is not None else None

            p2p_um = None
            if len(avg_y) >= 2:
                arr_um = (np.array(avg_y, float) - nominal_used) * 1000.0
                p2p_um = float(np.percentile(arr_um, 90) - np.percentile(arr_um, 10))

            conicity_um = None
            conicity_signed_um = None
            conicity_delta_mm = None
            conicity_span_mm = None
            # Conicity via Theil-Sen regression (median of pairwise slopes):
            # robust to up to ~29% outlier samples - i.e. groove walls do not
            # tilt the fit the way an ordinary least-squares fit would.
            if len(avg_y) >= 6:
                conicity_span_mm = float(Cfg.AXIAL_SPAN_MM)

                if conicity_span_mm > 1e-9:
                    x_mm = np.linspace(0.0, conicity_span_mm, len(avg_y))
                    y_mm = np.asarray(avg_y, dtype=float)

                    slope_mm_per_mm, _intercept, _lo, _hi = theilslopes(y_mm, x_mm)

                    conicity_delta_mm = float(slope_mm_per_mm * conicity_span_mm)
                    conicity_signed_um = conicity_delta_mm * 1000.0
                    conicity_um = abs(conicity_signed_um)
                else:
                    conicity_delta_mm = None
                    conicity_signed_um = None
                    conicity_um = None
                    conicity_span_mm = None

            # Display-only feature flag: any samples > 3x tol away from the
            # bulk median (or any internal time gap from THICKNESS_MIN/MAX).
            outlier_runs = 0
            outlier_count = 0
            if y_arr.size and mean_thickness is not None:
                outlier_runs, outlier_count = _detect_feature_runs(
                    y_arr, mean_thickness, tol_used_um
                )
            feature_count = outlier_runs + gap_count
            feature_removed_samples = outlier_count
            feature_rejection_used = bool(feature_count > 0)
            feature_rejection_reason = (
                "outliers_detected" if outlier_runs > 0 else
                "internal_time_gap" if gap_count > 0 else
                "none"
            )

            ok_nominal = (abs(mean_err_um) <= tol_used_um) if mean_err_um is not None else False
            ok_p2p = (p2p_um is not None and p2p_um <= Cfg.P2P_THRESH_UM)

            # Conicity OK if within threshold (or if conicity couldn't be computed for this pass)
            if conicity_signed_um is None:
                ok_conicity = True
            else:
                ok_conicity = (abs(conicity_signed_um) <= Cfg.CONICITY_THRESH_UM)

            status = "תקין" if (ok_nominal and ok_conicity) else "פסול"
            overall_ok = (status == "תקין")

            fail_reason = None
            if not overall_ok:
                if mean_err_um is not None and tol_used_um is not None and abs(mean_err_um) > tol_used_um:
                    fail_reason = "גבוה מהנומינלי" if mean_err_um > 0 else "נמוך מהנומינלי"
                elif conicity_signed_um is not None and abs(conicity_signed_um) > float(Cfg.CONICITY_THRESH_UM):
                    fail_reason = "קוניות גבוהה"
                else:
                    fail_reason = None

            # PLC verdict code:
            #   1 = OK
            #   2 = thickness below nominal
            #   3 = thickness above nominal
            #   4 = conicity too high
            if overall_ok:
                verdict = 1
            elif mean_err_um is not None and tol_used_um is not None and abs(mean_err_um) > tol_used_um:
                verdict = 3 if mean_err_um > 0 else 2
            elif conicity_signed_um is not None and abs(conicity_signed_um) > float(Cfg.CONICITY_THRESH_UM):
                verdict = 4
            else:
                verdict = 2

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
                "conicity_thresh_um": float(Cfg.CONICITY_THRESH_UM),
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
                "verdict": verdict,
                "raw_t_full": raw_t_full,
                "raw_y_full": raw_y_full,
                "feature_valid_mask": [True] * len(raw_t),
                "feature_count": int(feature_count),
                "feature_removed_samples": int(feature_removed_samples),
                "feature_rejection_used": feature_rejection_used,
                "feature_rejection_reason": feature_rejection_reason,
                "feature_threshold_um": float(2.0 * tol_used_um) if tol_used_um else None,
                "feature_max_gap_sec": float(max_gap_sec) if max_gap_sec else None,

            }

            p["pass_idx"] = len(self.state.passes) + 1
            save_pass_debug_csv(p)
            self.state.passes.append(p)

            # Keep signed mean error history for operator view (bell curve / gauge).
            try:
                if mean_err_um is not None:
                    self.state.err_hist_um.append(float(mean_err_um))
            except Exception:
                pass

            # Save the raw thickness profile of this pass to LAST_10/pass_NN.png.
            # The index rotates 1..10 so the 11th pass overwrites the 1st.
            try:
                self.state.pass_image_index = (self.state.pass_image_index % 10) + 1
                _save_raw_profile_png(p, self.state.pass_image_index)
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
    def __init__(self, config_path: str | None = None, get_job_limits=None):
        self.state = ThicknessRuntimeState()
        self.get_job_limits = get_job_limits  # optional () -> (min_mm, max_mm) for job overrides when running under T3
        self.readers = []
        for name, info in Cfg.SENSORS.items():
            r = SensorReader(self.state, name, info["port"])
            self.readers.append(r)
        self.proc = ThicknessProcessor(self.state, get_job_limits=get_job_limits)

        # Edge-detect counter so a finished pass reports its verdict to the PLC exactly once.
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

    # --- PLC-facing API ---
    def poll_new_verdict(self):
        """Return the verdict code (1/2/3/4) of the latest finished pass exactly once,
        then None until the next pass closes. Same pattern as mv.Cfg_mv.sttprog -> reg 4."""
        with self.state.lock:
            passes_so_far = len(self.state.passes)
            if passes_so_far <= self._last_reported_pass:
                return None
            self._last_reported_pass = passes_so_far
            return int(self.state.passes[-1].get("verdict", 0))

def _safe_float_list(v):
    try:
        return [float(x) for x in v]
    except Exception:
        return []

def get_display_payload(state: ThicknessRuntimeState, last_n: int = 3):

    # UI principle for feature-safe profiles:
    # show ONLY the latest finished pass and ONLY the points used for the
    # measurement. Do not average several passes and do not interpolate across
    # missing feature regions; those two operations can visually distort the two
    # bulk sides even when the numeric result is correct.
    with state.lock:
        ref = state.passes[-1] if state.passes else None

    if ref is None:
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
            "conicity_thresh_um": float(getattr(Cfg, "CONICITY_THRESH_UM", 5.0)),
            "feature_count": 0,
            "feature_removed_samples": 0,
            "feature_rejection_used": False,
        }

    # Show the same 20-point averaged profile used for P2P/conicity,
    # after feature masking. Fall back to raw only if averaging is unavailable.
    t_profile = _safe_float_list(ref.get("avg_t", []))
    y_profile = _safe_float_list(ref.get("avg_y", []))
    if not t_profile or not y_profile:
        t_profile = _safe_float_list(ref.get("raw_t", []))
        y_profile = _safe_float_list(ref.get("raw_y", []))

    nominal_used = float(ref.get("nominal_used", state.snapshot_nominal_tol()[0]))
    tol_used_um = float(ref.get("tol_used_um", state.snapshot_nominal_tol()[1]))
    mean_th = ref.get("mean_thickness", None)
    mean_err_um = ref.get("mean_err_um", None)

    return {
        "profile_t": [float(x) for x in t_profile],
        "profile_th_mm": [float(x) for x in y_profile],
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
        "conicity_thresh_um": float(ref.get("conicity_thresh_um", getattr(Cfg, "CONICITY_THRESH_UM", 5.0))),
        "p2p_um": (float(ref.get("p2p_um")) if ref.get("p2p_um") is not None else None),
        "overall_ok": ref.get("overall_ok", None),
        "fail_reason": ref.get("fail_reason", None),
        "feature_count": int(ref.get("feature_count") or 0),
        "feature_removed_samples": int(ref.get("feature_removed_samples") or 0),
        "feature_rejection_used": bool(ref.get("feature_rejection_used", False)),
        "feature_rejection_reason": ref.get("feature_rejection_reason", ""),
        "feature_threshold_um": ref.get("feature_threshold_um", None),
        "feature_max_gap_sec": ref.get("feature_max_gap_sec", None),
    }


# =========================
# Rendering API (UI should receive ONLY images)
# =========================

def _profile_plot_pil(profile_t, profile_th_mm, nominal_mm=None, tol_um=None,
                      median_mm=None, size_px=(420, 250), feature_text=""):
    """Render the thickness profile to a PIL RGB image (headless).

    Visual conventions:
      - Bulk samples (within 2x tol of the bulk median): blue line.
      - Outlier samples (> 2x tol from median): red dots overlay.
      - Bulk median: solid green horizontal line ("the answer").
      - Nominal: solid gray line; tolerance band: dashed gray lines.
      - Y-axis: nominal +/- 3x tol so a 15um wall stays inside the frame.
    """
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
        t_plot, y_plot = _split_at_time_gaps(profile_t, profile_th_mm)
        ax.plot(t_plot, y_plot, marker="o", markersize=3, linewidth=1.2, color="#1f77b4")

        # Outlier overlay (red dots). Anchor the band on the bulk median;
        # fall back to nominal if the renderer was called without a median.
        if tol_um is not None:
            try:
                tol_mm = float(tol_um) / 1000.0
                anchor_mm = (float(median_mm) if median_mm is not None
                             else (float(nominal_mm) if nominal_mm is not None else None))
                if anchor_mm is not None:
                    t_arr = np.asarray(profile_t, dtype=float)
                    y_arr = np.asarray(profile_th_mm, dtype=float)
                    bad = np.abs(y_arr - anchor_mm) > 2.0 * tol_mm
                    if np.any(bad):
                        ax.plot(t_arr[bad], y_arr[bad], "o", ms=3.5,
                                color="#c0392b", zorder=5)
            except Exception:
                pass

        if nominal_mm is not None and tol_um is not None:
            try:
                tol_mm = float(tol_um) / 1000.0
                nom = float(nominal_mm)
                ax.axhline(nom, linewidth=1.0, color="#666666")
                ax.axhline(nom + tol_mm, linestyle="--", linewidth=0.8, color="#999999")
                ax.axhline(nom - tol_mm, linestyle="--", linewidth=0.8, color="#999999")
                ax.set_ylim(nom - 3.0 * tol_mm, nom + 3.0 * tol_mm)
            except Exception:
                pass

        if median_mm is not None:
            try:
                ax.axhline(float(median_mm), linewidth=1.2, color="#27ae60",
                           label="bulk median")
            except Exception:
                pass

        if feature_text:
            ax.text(0.02, 0.97, feature_text, transform=ax.transAxes,
                    ha="left", va="top", fontsize=9, color="#a04000",
                    bbox=dict(boxstyle="round,pad=0.3", fc="#fff4e6",
                              ec="#d68910", alpha=0.9))
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
            ico = _load_icon_png("icons\\parallel.png", icon_sz)
        elif i == 1:
            ico = _load_icon_png("icons\\cone.png", icon_sz)
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
        median_mm=payload.get("mean_thickness_mm", None),
        size_px=(W - 2 * pad, 250),
        feature_text=("Feature detected" if payload.get("feature_rejection_used", False) else ""),
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



