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
        self.TIME_SYNC_THRESHOLD = float(req("time_sync_threshold_sec"))
        self.LIVE_UPDATE_HZ = float(req("live_update_hz"))

        self.REFERENCE_HEIGHT_MM = float(req("reference_height_mm"))
        self.ERROR_THRESHOLD_MM = float(req("error_threshold_mm"))
        self.AXIAL_SPAN_MM = float(req("axial_span_mm"))

        self.THICKNESS_MIN = float(req("thickness_min_mm"))
        self.THICKNESS_MAX = float(req("thickness_max_mm"))
        self.MAX_ERROR_COUNT = int(req("max_error_count"))

        self.TARGET_POINTS = int(req("target_points"))
        self.TRIM_LO = float(req("trim_lo"))
        self.TRIM_HI = float(req("trim_hi"))
        self.P2P_THRESH_UM = float(req("p2p_thresh_um"))

        # Conicity threshold [µm] (operator spec): default 5µm unless provided in JSON
        self.CONICITY_THRESH_UM = float(d.get("conicity_thresh_um", 5.0))

        self.NOMINAL_THICKNESS_MM_DEFAULT = float(req("nominal_thickness_mm_default"))
        self.TOL_UM_DEFAULT = float(req("tol_um_default"))

        # --- Two-gauge linear calibration ---
        # thickness = a * sensors_sum_mm + b   (sensors_sum = top + bottom distances)
        # Gain `a` is negative (thicker part => smaller sensor sum); magnitude near 1.0.
        # A measurement is produced ONLY when calibration.valid is True; until then the
        # processor refuses to emit verdicts and the UI is expected to require calibration.
        cal = d.get("calibration", {}) or {}
        self.CAL_GAUGE_1_MM = float(cal["gauge_1_known_mm"]) if cal.get("gauge_1_known_mm") is not None else None
        self.CAL_GAUGE_2_MM = float(cal["gauge_2_known_mm"]) if cal.get("gauge_2_known_mm") is not None else None
        self.CAL_WINDOW_SEC = float(cal.get("window_sec", 3.0))
        self.CAL_STABILITY_MAX_STD_UM = float(cal.get("stability_max_std_um", 5.0))
        self.CAL_GAIN_MIN = float(cal.get("gain_min", -1.5))
        self.CAL_GAIN_MAX = float(cal.get("gain_max", -0.5))
        self.CAL_A = float(cal["a"]) if cal.get("a") is not None else None
        self.CAL_B = float(cal["b"]) if cal.get("b") is not None else None
        self.CAL_VALID = bool(cal.get("valid", False))
        # Drift detection: warn if |D_implied - median(history)| exceeds this threshold [mm].
        self.CAL_DRIFT_WARN_MM = float(cal.get("drift_warn_mm", 0.5))
        self.CAL_HISTORY_MAX = int(cal.get("history_max", 20))

        # --- Continuous-run (sweep) calibration ---
        # The gauges are measured by a single continuous M6 move over BOTH of them
        # (Pos2 -> Pos3). Each gauge is a region of in-range samples; the two gauges
        # are separated by ONE dominant air gap (1-3 mm of pure air = a long run of
        # out-of-range samples). A precise gauge measured in motion produces momentary
        # dropouts that fragment it into several in-range runs, so we do NOT rely on
        # fixed sample-count thresholds. Instead the segmenter finds the single LARGEST
        # air gap and splits there (gauge 1 = everything before it, gauge 2 = after),
        # bridging every shorter internal dropout. This is immune to sweep speed and to
        # the number of dropouts.
        #   - sweep_gap_dominance:       the inter-gauge gap must be at least this many
        #                                times larger than the next-largest air gap,
        #                                otherwise the structure is ambiguous and the
        #                                run is rejected (replaces the old "exactly two
        #                                plateaus" check).
        #   - sweep_min_plateau_samples: min in-range samples on EACH side; guards
        #                                against a degenerate tiny span.
        #   - sweep_edge_trim_frac:      fraction trimmed from EACH end of each gauge
        #                                region before averaging (ramp as the sensor
        #                                climbs on/off the gauge). 0.25 => middle 50%.
        # No std/stability gate: the ripple is symmetric and cancels in the mean, so the
        # mean alone is the acceptance value.
        self.CAL_SWEEP_GAP_DOMINANCE = float(cal.get("sweep_gap_dominance", 3.0))
        self.CAL_SWEEP_MIN_PLATEAU_SAMPLES = int(cal.get("sweep_min_plateau_samples", 300))
        self.CAL_SWEEP_EDGE_TRIM_FRAC = float(cal.get("sweep_edge_trim_frac", 0.25))
        #   - sweep_min_gauge_separation_mm: the two gauges have DIFFERENT thicknesses,
        #     so their raw sensor sums must differ by at least this much. If the two
        #     segmented regions read nearly the same, the "gap" was a dropout inside a
        #     single gauge (not the inter-gauge gap) -> reject. Guards the case where
        #     only one air gap exists so the dominance test cannot fire.
        self.CAL_SWEEP_MIN_GAUGE_SEPARATION_MM = float(cal.get("sweep_min_gauge_separation_mm", 0.1))

class ThicknessRuntimeState(object):
    def __init__(self, cfg: ThicknessConfig):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

        self.samples = {"TOP": deque(maxlen=10000), "BOTTOM": deque(maxlen=10000)}
        self.last_ts = {"TOP": 0.0, "BOTTOM": 0.0}

        self.object_in_measurement = False
        self.valid_measurement_detected = False
        self.start_time = None
        self.sample_count = 0
        self.error_count = 0

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
        self.cal_a = float(cfg.CAL_A) if cfg.CAL_A is not None else None  # active gain (applied to every measurement)
        self.cal_b = float(cfg.CAL_B) if cfg.CAL_B is not None else None  # active offset [mm]
        self.cal_valid = bool(cfg.CAL_VALID and cfg.CAL_A is not None and cfg.CAL_B is not None)
        self.cal_collecting = False            # when True, run() captures sensor_sum samples instead of processing parts
        self.cal_buffer = []                   # raw sensor_sum samples gathered during a gauge measurement
        self.last_drift_warning = None         # populated by compute_and_apply_calibration when drift exceeds threshold

    def reset_pass(self):
        self.object_in_measurement = False
        self.valid_measurement_detected = False
        self.start_time = None
        self.sample_count = 0
        self.error_count = 0
        self.timestamps_list.clear()
        self.thickness_values.clear()

    def snapshot_nominal_tol(self):
        with self.nominal_lock:
            return float(self.nominal_thickness_mm), float(self.tol_um)

    # --- Calibration helpers ---
    def apply_calibration(self, sensors_sum_mm):
        """Apply the active linear calibration to a sensor-sum sample.

        Returns None when no valid calibration is loaded; callers must check.
        """
        with self.cal_lock:
            if not self.cal_valid or self.cal_a is None or self.cal_b is None:
                return None
            a, b = self.cal_a, self.cal_b
        return a * sensors_sum_mm + b

    def set_calibration(self, a, b):
        with self.cal_lock:
            self.cal_a = float(a)
            self.cal_b = float(b)
            self.cal_valid = True

    def is_calibrated(self):
        with self.cal_lock:
            return bool(self.cal_valid and self.cal_a is not None and self.cal_b is not None)

    def cal_begin_collect(self):
        with self.lock:
            self.cal_buffer = []
            self.cal_collecting = True

    def cal_add_sample(self, v_mm, in_range=True):
        # Store (in_range, sensor_sum_mm). Air/out-of-range samples are stored with
        # in_range=False and v_mm=None, so the sweep segmenter can see where the gap
        # (and leading/trailing air) is. The legacy static measure_gauge simply keeps
        # the in-range sums.
        self.cal_buffer.append((bool(in_range), (float(v_mm) if v_mm is not None else None)))

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
                    vals_mm = parse_push_packet(data)
                    if not vals_mm:
                        continue
                    num = len(vals_mm)
                    if num <= 0:
                        continue

                    # --- Time tagging (burst-robust) ---
                    # Do NOT use time.time() as the packet timestamp (it causes "time clumping" under CPU load).
                    # Instead, advance a per-sensor virtual clock by the configured frame interval.
                    interval = float(self.cfg.FRAME_INTERVAL_SEC)
                    dt = interval / num

                    with self.state.lock:
                        last = self.state.last_ts[self.name]
                        if last <= 0.0:
                            # Anchor once to host time, then keep advancing deterministically.
                            last = time.time() - interval
                        for v_mm in vals_mm:
                            last += dt
                            abs_mm = self.cfg.REFERENCE_HEIGHT_MM + v_mm
                            self.state.samples[self.name].append((last, abs_mm))
                        self.state.last_ts[self.name] = last
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
            # T6 horizontal-centering sweep sets state.sweep_pause so the processor
            # stops popping samples; the sweep then reads the FULL buffer at each stop.
            if getattr(self.state, "sweep_pause", False):
                time.sleep(0.02)
                continue

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

            t_time, top_abs_mm = t_samp
            b_time, bot_abs_mm = b_samp

            both_in_range = (top_abs_mm < self.cfg.ERROR_THRESHOLD_MM) and (bot_abs_mm < self.cfg.ERROR_THRESHOLD_MM)

            # Calibration capture mode: gather the FULL synced stream, INCLUDING air
            # (out-of-range) samples, so the sweep segmenter can see plateau/gap
            # structure (each gauge = a plateau of in-range samples; the gap between
            # them and the leading/trailing travel = runs of air). This must run BEFORE
            # the error-threshold handling below, which otherwise drops air samples.
            # No part/pass logic and no PLC verdict while capturing.
            if self.state.cal_collecting:
                if both_in_range:
                    self.state.cal_add_sample(top_abs_mm + bot_abs_mm, in_range=True)
                else:
                    self.state.cal_add_sample(None, in_range=False)
                continue

            if top_abs_mm >= self.cfg.ERROR_THRESHOLD_MM and bot_abs_mm >= self.cfg.ERROR_THRESHOLD_MM:
                self.state.error_count += 1
                if self.state.object_in_measurement and self.state.error_count >= self.cfg.MAX_ERROR_COUNT:
                    self.process_end_of_measurement()
                continue
            else:
                self.state.error_count = 0

            sensors_sum_mm = top_abs_mm + bot_abs_mm

            # Normal path requires a valid two-point linearization.
            # Until the operator runs the calibration sequence, samples are discarded
            # so that no verdict is produced from un-trustable measurements.
            thickness = self.state.apply_calibration(sensors_sum_mm)
            if thickness is None:
                continue

            if self.cfg.THICKNESS_MIN <= thickness <= self.cfg.THICKNESS_MAX:
                t_pair = (t_time + b_time) / 2.0
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

            # --- DIAGNOSTIC (temporary): expose per-part sample counts + profile so we
            #     can tell whether high conicity is a real taper or noise from too few
            #     synced samples. n_avg < 20 means the profile is raw (un-averaged). ---
            _con = conicity_signed_um if conicity_signed_um is not None else 0.0
            print(f"[PART DIAG] n_raw={self.state.sample_count} n_trim={raw_trim_count} "
                  f"n_avg={len(avg_y)} conicity={_con:+.2f}um p2p={p2p_um} status={status}")
            if conicity_signed_um is not None and abs(conicity_signed_um) > self.cfg.CONICITY_THRESH_UM:
                print(f"[CONICITY DIAG] avg_y={[round(float(v), 4) for v in avg_y]}")

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
# Continuous-run (sweep) calibration: plateau segmentation
# =========================
def _segment_calibration_plateaus(mask, min_plateau, gap_dominance):
    """Split a boolean in-range mask into the two gauge regions.

    Physical model: exactly two gauges separated by ONE dominant air gap. A precise
    gauge measured in motion produces momentary dropouts, so a single gauge may appear
    as several in-range runs. Rather than thresholding run/gap lengths (speed
    dependent), we locate the single LARGEST air gap between in-range runs and split
    there: gauge 1 = span from the first in-range run up to that gap, gauge 2 = span
    from just after the gap to the last in-range run. Every shorter internal dropout is
    thereby bridged.

    Guards:
      - need at least two in-range runs (else no gap to split on);
      - the largest gap must be >= gap_dominance x the next-largest gap (a clear single
        separator), else the structure is ambiguous;
      - each resulting span must hold >= min_plateau in-range samples.

    Returns (spans, reason): spans is [(s0,e0),(s1,e1)] in travel order on success, or
    [] on failure with a human-readable reason.
    """
    n = len(mask)

    # Maximal in-range runs.
    runs = []
    i = 0
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1

    if len(runs) < 2:
        return [], f"need >=2 in-range regions, found {len(runs)}"

    # Air gaps between consecutive in-range runs.
    gaps = [runs[k + 1][0] - runs[k][1] for k in range(len(runs) - 1)]
    split = max(range(len(gaps)), key=lambda k: gaps[k])
    biggest = gaps[split]
    others = [g for k, g in enumerate(gaps) if k != split]
    second = max(others) if others else 0
    if second > 0 and biggest < gap_dominance * second:
        return [], (f"no dominant air gap (largest={biggest}, next={second}, "
                    f"need >={gap_dominance:g}x) — ambiguous plateau structure")

    span1 = (runs[0][0], runs[split][1])
    span2 = (runs[split + 1][0], runs[-1][1])

    c1 = sum(1 for k in range(span1[0], span1[1]) if mask[k])
    c2 = sum(1 for k in range(span2[0], span2[1]) if mask[k])
    if c1 < min_plateau or c2 < min_plateau:
        return [], f"gauge region too small (g1={c1}, g2={c2}, min={min_plateau})"

    return [span1, span2], "ok"


# =========================
# Headless public API
# =========================
class ThicknessModule(object):
    """Convenience wrapper to start/stop readers+processor without the built-in tkinter UI."""

    def __init__(self, config_path: str | None = None):
        if config_path is None:
            config_path = str(Path(__file__).parent / "config" / "config457_thk.json")
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

        # cal_end_collect returns (in_range, sum_mm) tuples; keep only in-range sums.
        solid = [s for (ir, s) in data if ir and s is not None]
        if len(solid) < 5:
            return {"ok": False, "reason": "too few samples", "n": len(solid),
                    "raw_median_mm": None, "std_um": None}

        arr = np.array(solid, dtype=float)
        med = float(np.median(arr))
        std_um = float(np.std(arr) * 1000.0)
        ok = std_um <= float(self.cfg.CAL_STABILITY_MAX_STD_UM)
        return {"ok": ok, "reason": ("" if ok else "unstable (std too high)"),
                "n": len(solid), "raw_median_mm": med, "std_um": std_um}

    # =====================================================================
    # Continuous-run (sweep) calibration.
    # A single M6 move (Pos2 -> Pos3) carries the sensors over BOTH gauges in
    # one pass. begin_calibration_sweep() starts capturing; the motion driver
    # then commands the move and, on arrival at Pos3, calls
    # finish_calibration_sweep() to segment the captured stream into the two
    # gauge plateaus and return their trimmed-mean sensor sums.
    # =====================================================================
    def begin_calibration_sweep(self):
        """Start capturing a continuous Pos2->Pos3 calibration sweep.

        The caller must have the sensors at the Pos2 start point, call this, THEN
        command the continuous move to Pos3, and call finish_calibration_sweep() once
        arrival at Pos3 is confirmed. Everything the readers deliver in between (air +
        both gauges) is captured.
        """
        self.state.reset_pass()            # make sure we are not mid-part
        self.state.cal_begin_collect()

    def finish_calibration_sweep(self):
        """Stop capturing and segment the sweep into the two gauge plateaus.

        Returns dict: {ok, reason, n, plateaus:[{n, raw_mean_mm, std_um}, ...],
        raw_g1_mm, g1_std_um, raw_g2_mm, g2_std_um}. Plateau order is travel order:
        the first plateau -> gauge 1 (gauge_1_known_mm), the second -> gauge 2.
        """
        buffer = self.state.cal_end_collect()
        return self.analyze_calibration_sweep(buffer)

    def analyze_calibration_sweep(self, buffer):
        """Segment a captured sweep buffer into exactly two gauge plateaus.

        buffer: list of (in_range: bool, sensor_sum_mm or None) as produced during
        cal_collecting. Each gauge is a plateau of in-range samples; the air gap and
        the leading/trailing travel are out-of-range runs. Segmentation bridges tiny
        internal dropouts, drops artifacts, then requires EXACTLY two plateaus. Each
        plateau is edge-trimmed (CAL_SWEEP_EDGE_TRIM_FRAC per end) and averaged — no
        std/stability gate, the mean is the value (ripple cancels in the mean).
        """
        min_plat = int(self.cfg.CAL_SWEEP_MIN_PLATEAU_SAMPLES)
        dominance = float(self.cfg.CAL_SWEEP_GAP_DOMINANCE)
        trim = float(self.cfg.CAL_SWEEP_EDGE_TRIM_FRAC)

        buffer = list(buffer or [])
        mask = [bool(ir and (s is not None)) for (ir, s) in buffer]
        sums = [s for (ir, s) in buffer]

        runs, reason = _segment_calibration_plateaus(mask, min_plat, dominance)

        result = {"ok": False, "reason": "", "n": len(buffer), "plateaus": [],
                  "raw_g1_mm": None, "g1_std_um": None,
                  "raw_g2_mm": None, "g2_std_um": None}

        def _plateau_stats(s0, s1, do_trim):
            seg = [sums[k] for k in range(s0, s1) if sums[k] is not None]
            if not seg:
                return None
            if do_trim:
                m = len(seg)
                lo = int(m * trim)
                hi = m - int(m * trim)
                core = seg[lo:hi] if (hi - lo) >= 1 else seg
            else:
                core = seg
            arr = np.array(core, dtype=float)
            return {"n": len(core), "raw_mean_mm": float(np.mean(arr)),
                    "std_um": float(np.std(arr) * 1000.0)}

        if len(runs) != 2:
            result["reason"] = reason
            return result

        stats = [_plateau_stats(s0, s1, do_trim=True) for (s0, s1) in runs]
        if any(st is None for st in stats):
            result["reason"] = "empty gauge region after trimming"
            return result

        # Two DIFFERENT gauges must read differently; if the two regions are nearly
        # identical the split fell on a dropout inside a single gauge, not the real gap.
        min_sep = float(self.cfg.CAL_SWEEP_MIN_GAUGE_SEPARATION_MM)
        sep = abs(stats[0]["raw_mean_mm"] - stats[1]["raw_mean_mm"])
        if sep < min_sep:
            result["plateaus"] = stats
            result["reason"] = (f"gauge regions too similar (|Δ|={sep*1000.0:.1f}um < "
                                f"{min_sep*1000.0:.0f}um) — likely one gauge split by a dropout")
            return result

        result["plateaus"] = stats
        result["raw_g1_mm"], result["g1_std_um"] = stats[0]["raw_mean_mm"], stats[0]["std_um"]
        result["raw_g2_mm"], result["g2_std_um"] = stats[1]["raw_mean_mm"], stats[1]["std_um"]
        result["ok"] = True
        result["reason"] = "ok"
        return result

    def compute_and_apply_calibration(self, raw_g1_mm, raw_g2_mm):
        """Two-point linear fit from the two gauge sensor-sum readings.

        Pairs (raw_g1 -> CAL_GAUGE_1_MM) and (raw_g2 -> CAL_GAUGE_2_MM), solves
        thickness = a*sensor_sum + b, validates the gain bounds, and on success
        applies it live and persists it (with a drift-tracking history) to the
        config JSON.

        Returns (ok: bool, info: dict{a, b, d_implied, drift_warning, reason}).
        """
        info = {"a": None, "b": None, "d_implied": None, "drift_warning": None, "reason": ""}
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

        # Effective sensor distance implied by this calibration: solving for the
        # sensor_sum that yields thickness=0 gives S0 = -b/a, which is mechanically
        # the gap between the sensors at zero part thickness.
        d_implied = -b / a
        info["d_implied"] = float(d_implied)

        # Compare against the median of recent calibrations on this machine. If the
        # difference exceeds the configured threshold, surface a warning — but do
        # NOT block the calibration: the operator decides whether to accept.
        drift_warning = self._check_drift(d_implied)
        info["drift_warning"] = drift_warning
        self.state.last_drift_warning = drift_warning

        # Apply live and remember on the loaded cfg, then persist.
        self.state.set_calibration(a, b)
        self.cfg.CAL_A, self.cfg.CAL_B = a, b
        self.cfg.CAL_VALID = True
        self._save_calibration(a, b, raw_g1_mm, raw_g2_mm, d_implied)
        info["reason"] = "ok"
        return True, info

    def _check_drift(self, d_implied_new):
        """Compare new D_implied against the median of stored history.

        Returns a human-readable warning string when |delta| > CAL_DRIFT_WARN_MM,
        or None when history is empty or the calibration is consistent.
        """
        try:
            data = load_config_json(self.config_path)
        except Exception:
            return None
        history = (data.get("calibration", {}) or {}).get("history", []) or []
        if not history:
            return None
        recent = [float(h["d_implied"]) for h in history[-5:] if "d_implied" in h]
        if not recent:
            return None
        median = float(np.median(np.array(recent, dtype=float)))
        delta = float(d_implied_new) - median
        if abs(delta) > float(self.cfg.CAL_DRIFT_WARN_MM):
            return (f"drift detected: D_implied={d_implied_new:.4f}mm vs recent median "
                    f"{median:.4f}mm (Δ={delta:+.4f}mm, threshold={self.cfg.CAL_DRIFT_WARN_MM}mm)")
        return None

    def get_calibration(self):
        """Return the currently active calibration {a, b, valid}."""
        with self.state.cal_lock:
            a = self.state.cal_a
            b = self.state.cal_b
            valid = bool(self.state.cal_valid and a is not None and b is not None)
            return {
                "a": (float(a) if a is not None else None),
                "b": (float(b) if b is not None else None),
                "valid": valid,
            }

    def is_calibrated(self):
        """True iff a valid two-point linearization is loaded and active."""
        return self.state.is_calibrated()

    def invalidate_calibration(self, persist=True):
        """Mark the active calibration invalid, e.g. after a failed calibration attempt
        or at startup (to force a fresh calibration every session). While invalid,
        apply_calibration() returns None so NO thickness/verdict is produced until a
        new successful calibration is applied. persist=True also writes valid=false to
        the JSON; persist=False only invalidates in memory (session-only)."""
        with self.state.cal_lock:
            self.state.cal_valid = False
        self.cfg.CAL_VALID = False
        if not persist:
            return
        try:
            data = load_config_json(self.config_path)
            cal = data.get("calibration", {}) or {}
            cal["valid"] = False
            data["calibration"] = cal
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _save_calibration(self, a, b, raw_g1, raw_g2, d_implied):
        """Persist calibration results into the 'calibration' block of the config JSON,
        preserving every other key. Appends a history entry for drift detection.
        Failures here never crash the run."""
        try:
            try:
                data = load_config_json(self.config_path)
            except Exception:
                data = {}
            cal = data.get("calibration", {}) or {}
            iso = time.strftime("%Y-%m-%dT%H:%M:%S")
            cal["a"] = float(a)
            cal["b"] = float(b)
            cal["raw_g1_mm"] = float(raw_g1)
            cal["raw_g2_mm"] = float(raw_g2)
            cal["d_implied_mm"] = float(d_implied)
            cal["valid"] = True
            cal["last_calibration_iso"] = iso

            history = list(cal.get("history", []) or [])
            history.append({
                "iso": iso,
                "a": float(a),
                "b": float(b),
                "d_implied_mm": float(d_implied),
                "raw_g1_mm": float(raw_g1),
                "raw_g2_mm": float(raw_g2),
            })
            max_hist = int(getattr(self.cfg, "CAL_HISTORY_MAX", 20) or 20)
            if len(history) > max_hist:
                history = history[-max_hist:]
            cal["history"] = history

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



