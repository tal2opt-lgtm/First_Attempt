import cv2 as cv2
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox
import random
import math
import json
from collections import deque
from PIL import Image, ImageTk
import time
import threading
import os
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
import Utils
import  thickness_Module_457  as thck
import  Vision_Module_457 as mv
from pyModbusTCP.client import ModbusClient

# ----------------------------
# Config (global variables)
# ----------------------------
class Cfg_:
    """Configuration loaded from config\\config457.json on init."""
    ConfigPath = os.path.join("config", "config457.json")
    ConfigAttrs = ("WorkstationId", "PlcTcpip", "PlcPort", "PlcLiveSignIntervalSeconds", "SqlNt", "SqlPri", "MvFolder", "Password")

    def __init__(self):
        self.WorkstationId = ""
        self.PlcTcpip ="192.168.3.169"
        self.PlcPort = 502
        self.PlcLiveSignIntervalSeconds = 2.0
        self.SqlNt = ""
        self.SqlPri = ""
        self.MvFolder = ""
        self.Password = "1234567"
        self.plc_connected = False  # runtime only, not persisted to JSON
        self._load()

    def _load(self):
        if not os.path.isfile(self.ConfigPath):
            return
        with open(self.ConfigPath, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.WorkstationId = data.get("WorkstationId", self.WorkstationId)
        self.PlcTcpip = data.get("PlcTcpip", self.PlcTcpip)
        self.PlcPort = int(data.get("PlcPort", self.PlcPort))
        self.PlcLiveSignIntervalSeconds = float(data.get("PlcLiveSignIntervalSeconds", self.PlcLiveSignIntervalSeconds))
        self.SqlNt = data.get("SqlNt", self.SqlNt)
        self.SqlPri = data.get("SqlPri", self.SqlPri)
        self.MvFolder = data.get("MvFolder", self.MvFolder)
        self.Password = data.get("Password", "1234567")

    def save(self):
        """Persist current values to config457.json."""
        data = {
            "WorkstationId": self.WorkstationId,
            "PlcTcpip": self.PlcTcpip,
            "PlcPort": self.PlcPort,
            "PlcLiveSignIntervalSeconds": self.PlcLiveSignIntervalSeconds,
            "SqlNt": self.SqlNt,
            "SqlPri": self.SqlPri,
            "MvFolder": self.MvFolder,
            "Password": self.Password,
        }
        os.makedirs(os.path.dirname(self.ConfigPath), exist_ok=True)
        with open(self.ConfigPath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)

Cfg = Cfg_()
log_lock = threading.Lock()
RegsLock = threading.Lock()
# PLC registers: 20 integers (global); when any > 0, T4 stores them, zeros them, writes 200 regs to PLC
RegsToPlc = [0] * 20
RegsTemp = [0] * 20  # last 20 values stored when T4 flushes to PLC

RegsFromPlc = [0] * 40

# --- Thickness verdict register (Py -> PLC): 0 none|1 OK|2 below|3 above|4 conicity ---
THK_RESULT_REG = 5

# --- Calibration: M6 horizontal motor register map (0-based, per the register doc) ---
# The code drives M6 to each Johnson gauge: motion is commanded on reg 6 (write the
# target POS number) and arrival is read back on reg 24 (== target POS number).
# This is exactly the proven one-shot "regs[6]=5 -> Go position 5" primitive, routed
# through RegsToPlc so T4 remains the single Modbus writer.
M6_CMD_REG = 6           # PC->PLC : write POS number -> "M6 Go position N"
M6_STATUS_REG = 24       # PLC->PC : M6 status code (10=Ready, 7/8=moving, 9=not ready)
M6_ACTPOS_REG = 25       # PLC->PC : M6 actual position (encoder units) — diagnostic
M6_STATUS_READY = 10     # M6 reports "Ready" when stopped/arrived
M6_STATUS_MOVING = (7, 8)  # going forward / backward
M6_CMD_STOP = 7          # R6 Stop
M6_CMD_JOG_FWD = 9       # R6 JOG forward
M6_CMD_JOG_BACK = 10     # R6 JOG backward
POS_GAUGE_1 = 5          # Johnson gauge 1
POS_GAUGE_2 = 6          # Johnson gauge 2
POS_MEASURE = 1          # measurement (work) position
M6_MOVE_TIMEOUT_SEC = 60.0  # per Zero_thickness_gauge procedure (step 5): wait up to 60 s for arrival
# T6 horizontal-centering sweep parameters. Motion uses a preset ABSOLUTE start
# (POS3) and a RELATIVE step POS (POS4, configured on the HMI to move a fixed small
# increment). Each POS4 command = one exact relative step, executed by the PLC in its
# own closed loop — so there is no software jog, no overshoot, and no bus lease.
T6_START_POS = 3             # absolute preset POS reached before the sweep (R6 = 3)
T6_STEP_POS = 4              # RELATIVE step POS (R6 = 4): one fixed increment per command
T6_N_STEPS = 12
T6_SETTLE_SEC = 0.5          # vibration damping before sampling
T6_SAMPLE_SEC = 0.2          # sampling window (all samples averaged)
T6_PRODUCTION_SETTLE_SEC = 4.0   # wait for the machine to stop after pausing production
# Arrival detection (shared by the absolute POS3/POS1 moves and the relative POS4 step)
T6_POS_ARRIVE_TIMEOUT = 60.0
T6_POS_STABLE_DWELL_SEC = 0.5
T6_POS_STABLE_TOL_UM = 30
T6_POS_MOTION_MIN_UM = 500   # "motion seen" threshold for the absolute moves
T6_REL_MOVED_TOL_UM = 15     # smaller "motion seen" threshold for the tiny relative step
T6_EDGE_CLEAR_SEC = 0.15     # settle after forcing R6->0, so the next POS4 is a fresh edge

# Set by T3 at startup so the calibration sequence can reach the thickness module.
ThckModule: "thck.ThicknessModule | None" = None

def log(msg, level="INFO"):
    t = time.time()
    s = time.strftime("%H:%M:%S", time.localtime(t))
    ms = int((t % 1) * 1000)

    line = f"{s}.{ms:03d} [{level}] {msg}"

    with log_lock:
        try:
            print(line)
        except Exception:
            pass
        # encoding="utf-8" so Hebrew log lines don't crash on Windows cp1252.
        with open("system_log.txt", "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")

class Job_settings_():
    # Keys persisted only in Job_settings.json (not from CSV/XLSX)
    JsonKeys = ("wo_order_number", "wo_part_number", "wo_order_quantity", "wo_object_counter", "current_pack_job")
    Job_settings_path = os.path.join("config", "Job_settings.json")
    job_pack_settings_path = os.path.join("config", "job_pack_settings.xlsx")
    last_mv_spec_path = os.path.join("config", "last_mv_spec.csv")

    def __init__(self):
        # PIL images (updated by threads / logic)
        self.pil1 = None   # engraving photot
        self.pil2 = None   # mv photo
        self.pil22 = None  # mv live photo
        self.pil3 = None   #TkcK pohoto

        # textual/debug (optional)
        self.txt1 = None
        self.txt2 = None
        self.txt3 = None

        # Work order header fields (defaults; overwritten by reload())
        self.wo_order_number   = "^ txt1"
        self.wo_part_number    = "txt2"
        self.wo_order_quantity = "1wo_order_quantity"
        self.wo_object_counter = "456"
        self.current_pack_job = 2

        # From XLSX/CSV (loaded in reload())
        self.pack_jobs = None
        self.spec = None

        # stop flag (shared)
        self.stop_event = threading.Event()

        self.thk_mean_mm = None
        self.thk_p2p_um = None
        self.thk_conicity_um = None

        # Thickness calibration view (written by T3, read by the UI). Reassigned as a
        # whole dict on each update so the cross-thread read is always consistent.
        self.thk_cal = {
            "active": False, "phase": "idle",
            "g1_raw_mm": None, "g1_std_um": None,
            "g2_raw_mm": None, "g2_std_um": None,
            "a": None, "b_um": None, "message": "",
        }

        # Production block while the wrapped calibration/zeroing sequence runs
        # (read by T2). Set by run_calibration_sequence, honoring the procedure's
        # "Production (T2) stays blocked while ZeroingInProgress applies".
        self.zeroing_in_progress = False

        # T6 Horizontal Zeroing Temporary UI handshake (written by T6, read by EL_UI).
        self.t6 = {
            "running": False,
            "close_working": False,
            "show_error": False,
            "error_msg": "",
        }

        # True once a job/spec has been loaded into the vision module via mv.GetSpec
        # (which creates the geometry globals like Body). Image analysis in T2 runs
        # ONLY when this is True — the real prerequisite for analysis, independent of
        # the thickness calibration gate.
        self.spec_loaded = False

        self.reload()

    def reload(self):
        """Load from Job_settings.json, job_pack_settings.xlsx, last_mv_spec.csv."""
        if os.path.isfile(self.Job_settings_path):
            with open(self.Job_settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key in self.JsonKeys:
                if key in data:
                    setattr(self, key, data[key])
        if os.path.isfile(self.job_pack_settings_path):
            self.pack_jobs = pd.read_excel(self.job_pack_settings_path)
        if os.path.isfile(self.last_mv_spec_path):
            self.spec = pd.read_csv(self.last_mv_spec_path)

    def save(self):
        """Save to Job_settings.json, job_pack_settings.xlsx, last_mv_spec.csv. None defaults save as null (last value ignored)."""
        data = {}
        for key in self.JsonKeys:
            val = getattr(self, key, None)
            data[key] = val
        os.makedirs(os.path.dirname(self.Job_settings_path), exist_ok=True)
        with open(self.Job_settings_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        if self.pack_jobs is not None:
            self.pack_jobs.to_excel(self.job_pack_settings_path, index=False)
        if self.spec is not None:
            self.spec.to_csv(self.last_mv_spec_path, index=False)

Job_settings = Job_settings_()
 

# ----------------------------
# Startup warmup + calibration gate (T5)
# ----------------------------
class StartupCalGate_:
    """From program start, blocks measurement/verdict for `warmup_minutes`
    (config457_zeroing.json). After warmup the machine requires a calibration
    before any work is allowed: the UI shows 'נדרש כיול', the operator presses
    'Start Calibration', the M6 sequence runs, and on success work is enabled."""
    ConfigPath = os.path.join("config", "config457_zeroing.json")

    def __init__(self):
        self.WarmupMinutes = 10.0
        self._load()
        self._lock = threading.Lock()
        self.SessionStart = time.time()
        self.state = "warmup"          # warmup -> cal_required -> calibrating -> ready ; cal_failed
        self.last_cal_iso = None
        self.last_cal_ok = None

    def _load(self):
        if os.path.isfile(self.ConfigPath):
            try:
                with open(self.ConfigPath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.WarmupMinutes = float(data.get("warmup_minutes", self.WarmupMinutes))
            except Exception:
                pass

    @property
    def WorkAllowed(self):
        return self.state == "ready"

    def WarmupRemainingMin(self):
        return max(self.WarmupMinutes - (time.time() - self.SessionStart) / 60.0, 0.0)

    def Tick(self):
        with self._lock:
            if self.state == "warmup" and self.WarmupRemainingMin() <= 0.0:
                self.state = "cal_required"
                log("warmup complete -> calibration required", "STATUS")

    def CanCalibrate(self):
        return self.state in ("cal_required", "cal_failed")

    def BeginCalibration(self):
        """Called by the 'Start Calibration' button; valid only after warmup ended."""
        with self._lock:
            if self.state in ("cal_required", "cal_failed"):
                self.state = "calibrating"
                log("calibration sequence started by operator", "STATUS")
                return True
            return False

    def CalibrationResult(self, ok, iso=None):
        with self._lock:
            self.last_cal_iso = iso or time.strftime("%Y-%m-%d %H:%M:%S")
            self.last_cal_ok = bool(ok)
            self.state = "ready" if ok else "cal_failed"
            log(f"calibration result: {'OK' if ok else 'FAIL'} at {self.last_cal_iso}", "STATUS")

    def StatusText(self):
        st = self.state
        if st == "warmup":
            return f"חימום — נותרו {self.WarmupRemainingMin():.1f} דק'"
        if st == "cal_required":
            return "נדרש כיול"
        if st == "calibrating":
            return "כיול בתהליך…"
        if st == "cal_failed":
            return "כיול נכשל — נדרש כיול חוזר"
        return "מוכן לעבודה"

    def LastCalText(self):
        if self.last_cal_iso is None:
            return "כיול אחרון: —"
        verdict = "תקין" if self.last_cal_ok else "פסול"
        return f"כיול אחרון: {self.last_cal_iso} — {verdict}"


CalGate = StartupCalGate_()


def T5_startup_gate():
    """Warmup countdown + state ticking for the calibration gate."""
    while not Job_settings.stop_event.is_set():
        CalGate.Tick()
        time.sleep(1.0)


def T6_horizontal_zeroing():
    """T6 Horizontal Centering — find the X measurement-center by sweeping M6.

    Motion goes through the normal T4 register path (no bus lease): an absolute move
    to POS3 (start), then N RELATIVE steps via POS4 (each POS4 command moves a fixed
    HMI-configured increment, executed precisely by the PLC's own closed loop), then
    a return to the measurement position. At each stop TOP/BOTTOM are sampled and
    averaged and bottom/top/pos (R25) are recorded, then saved to Excel.

    Production (T2) is paused for the whole run and the thickness Processor is paused
    so the sweep can read the full sample buffer at each stop.
    Rule: all helpers nested inside this function — no new top-level helpers.
    """
    def _publish(**changes):
        view = dict(getattr(Job_settings, "t6", {}) or {})
        view.update(changes)
        Job_settings.t6 = view

    def _show_error(msg):
        log(f"T6 error: {msg}", "WARN")
        _publish(show_error=True, error_msg=str(msg), close_working=True, running=False)

    def _go_and_wait(pos, relative=False, timeout=T6_POS_ARRIVE_TIMEOUT):
        """Command M6 to POS `pos` and wait for arrival, via RegsToPlc/RegsFromPlc.
        Arrival = R24 READY once motion was seen, or the position holding stable after
        motion. ABSOLUTE moves re-assert the target every 1 s (harmless). A RELATIVE
        step (POS4) is edge-triggered: it fires on a fresh 0 -> N transition. T4 latches
        R6 at the last commanded value, so a repeat R6=4 with no intervening 0 is "no
        change" and never re-triggers. Before a relative step we therefore drive R6
        back to 0 at the PLC ("release the button"). Returns (ok, final_actpos)."""
        if relative:
            with RegsLock:
                RegsToPlc[0] = 1          # live-sign flush carries the zeroed R6 -> PLC R6=0
            time.sleep(T6_EDGE_CLEAR_SEC)
        start = int(RegsFromPlc[M6_ACTPOS_REG])
        with RegsLock:
            RegsToPlc[M6_CMD_REG] = pos
        moved_tol = T6_REL_MOVED_TOL_UM if relative else T6_POS_MOTION_MIN_UM
        t0 = time.time(); last_resend = t0; stable_since = t0
        last = start; moved = False; cur = start
        while time.time() - t0 < timeout:
            if Job_settings.stop_event.is_set():
                return False, cur
            cur = int(RegsFromPlc[M6_ACTPOS_REG])
            st = int(RegsFromPlc[M6_STATUS_REG])
            if st in M6_STATUS_MOVING or abs(cur - start) >= moved_tol:
                moved = True
            if abs(cur - last) > T6_POS_STABLE_TOL_UM:
                last = cur; stable_since = time.time()
            if moved and st == M6_STATUS_READY:
                return True, cur
            if moved and time.time() - stable_since >= T6_POS_STABLE_DWELL_SEC:
                return True, cur
            if not relative and time.time() - last_resend >= 1.0:
                with RegsLock:
                    RegsToPlc[M6_CMD_REG] = pos
                last_resend = time.time()
            time.sleep(0.02)
        log(f"T6: TIMEOUT waiting for POS {pos} (start={start} cur={cur} moved={moved})", "WARN")
        return False, cur

    def _sample_stop():
        """Flush, settle, collect a fresh window, average. Valid because the sweep
        pauses the thickness Processor (state.sweep_pause), so nothing pops the buffer
        while we fill it — we read ALL samples the readers deliver in the window."""
        tm = ThckModule
        if tm is None:
            return float("nan"), float("nan"), 0, 0
        st = tm.state
        with st.lock:
            st.samples["TOP"].clear(); st.samples["BOTTOM"].clear()
        time.sleep(T6_SETTLE_SEC)                 # vibration damping
        with st.lock:
            st.samples["TOP"].clear(); st.samples["BOTTOM"].clear()
        time.sleep(T6_SAMPLE_SEC)                 # fresh stationary window (no popping)
        with st.lock:
            tops = [v for _, v in st.samples["TOP"]]
            bots = [v for _, v in st.samples["BOTTOM"]]
        top = float(np.mean(tops)) if tops else float("nan")
        bot = float(np.mean(bots)) if bots else float("nan")
        return top, bot, len(tops), len(bots)

    def _center_from_profile(xm):
        """From the (N,3) sweep matrix [top, bottom, pos_um] find the measurement
        center: fit a parabola to TOP and to BOTTOM vs position, take each vertex
        (TOP minimum / BOTTOM maximum), and average them. Returns a dict in mm/um, or
        None if too few in-range points to fit."""
        thr = float(ThckModule.cfg.ERROR_THRESHOLD_MM)
        ok = [i for i in range(xm.shape[0])
              if np.isfinite(xm[i, 0]) and np.isfinite(xm[i, 1]) and np.isfinite(xm[i, 2])
              and xm[i, 0] < thr and xm[i, 1] < thr]
        if len(ok) < 5:
            return None
        pos_mm = xm[ok, 2] / 1000.0
        top_v = xm[ok, 0]; bot_v = xm[ok, 1]
        try:
            at = np.polyfit(pos_mm, top_v, 2)   # TOP: minimum  -> a > 0
            ab = np.polyfit(pos_mm, bot_v, 2)   # BOTTOM: maximum -> a < 0
            if abs(at[0]) < 1e-9 or abs(ab[0]) < 1e-9:
                return None
            top_c = -at[1] / (2.0 * at[0])
            bot_c = -ab[1] / (2.0 * ab[0])
        except Exception:
            return None
        center_mm = (top_c + bot_c) / 2.0
        return {
            "top_center_mm": float(top_c),
            "bot_center_mm": float(bot_c),
            "offset_um": float((top_c - bot_c) * 1000.0),
            "center_mm": float(center_mm),
            "center_um": float(center_mm * 1000.0),
        }

    error_msg = None
    paused = False
    try:
        _publish(running=True, close_working=False, show_error=False, error_msg="")
        log("T6: horizontal centering started", "STATUS")

        if ThckModule is None:
            error_msg = "T6: thickness module not ready"
            _show_error(error_msg)
            return

        # --- envelope: pause production, block vision + processor, let it settle ---
        with RegsLock:
            RegsToPlc[0] = 2                       # pause machine
        paused = True
        Job_settings.zeroing_in_progress = True    # block T2 (vision)
        ThckModule.state.sweep_pause = True         # pause the thickness Processor
        log("T6: production paused (R0=2), settling", "STATUS")
        time.sleep(T6_PRODUCTION_SETTLE_SEC)

        # --- absolute move to the start position (POS3) ---
        ok, pfinal = _go_and_wait(T6_START_POS, relative=False)
        if not ok:
            log(f"T6: WARNING — POS{T6_START_POS} not confirmed (pos={pfinal}); sweeping from here", "WARN")
        else:
            log(f"T6: at POS{T6_START_POS} (pos={pfinal}) — starting sweep", "STATUS")

        # --- sweep: sample at the current stop, then take one relative POS4 step ---
        x = np.zeros((T6_N_STEPS, 3), dtype=float)
        thr = float(ThckModule.cfg.ERROR_THRESHOLD_MM)
        for i in range(T6_N_STEPS):
            if Job_settings.stop_event.is_set():
                error_msg = "T6 aborted (stop)"
                _show_error(error_msg)
                return
            top, bot, ct, cb = _sample_stop()
            pos = int(RegsFromPlc[M6_ACTPOS_REG])
            x[i, 0] = top
            x[i, 1] = bot
            x[i, 2] = float(pos)
            oor = "" if (top < thr and bot < thr) else " <OOR>"
            log(f"T6[{i:03d}/{T6_N_STEPS}] pos={pos} top={top:.3f} bot={bot:.3f} n={ct}/{cb}{oor}", "INFO")
            if i < T6_N_STEPS - 1:
                sok, sfinal = _go_and_wait(T6_STEP_POS, relative=True)
                if not sok:
                    log(f"T6: relative step {i} not confirmed (pos={sfinal})", "WARN")

        # --- save the matrix (columns bottom, top, pos); pandas+openpyxl, CSV fallback ---
        df_t6 = pd.DataFrame({"bottom": x[:, 1], "top": x[:, 0], "pos": x[:, 2]})
        os.makedirs("native_img", exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join("native_img", f"T6_horizontal_zeroing_{stamp}.xlsx")
        try:
            df_t6.to_excel(path, index=False, engine="openpyxl")
            saved = path
        except Exception as e:
            csv_path = os.path.splitext(path)[0] + ".csv"
            df_t6.to_csv(csv_path, index=False)
            saved = csv_path
            log(f"T6: xlsx unavailable ({e}); saved CSV instead", "WARN")
        log(f"T6: saved {saved}", "STATUS")

        # --- compute the measurement center from the profile extrema, publish results ---
        res = _center_from_profile(x)
        if res is not None:
            _publish(done=True, running=False, show_error=False, error_msg="", **res)
            log(f"T6: center bottom={res['bot_center_mm']:.3f}mm top={res['top_center_mm']:.3f}mm "
                f"offset={res['offset_um']:+.0f}um -> POS1={res['center_um']:.0f}um "
                f"({res['center_mm']:.3f}mm)", "STATUS")
        else:
            _publish(done=True, running=False, show_error=False, error_msg="",
                     top_center_mm=None, bot_center_mm=None, offset_um=None,
                     center_mm=None, center_um=None)
            log("T6: center computation failed (too few in-range points)", "WARN")

        # --- return M6 to the measurement position ---
        _go_and_wait(POS_MEASURE, relative=False)
        log("T6: horizontal centering done", "STATUS")

    except Exception as e:
        _show_error(f"T6 exception: {e}")
    finally:
        # Always: resume production, unblock vision + processor, publish stop.
        if paused:
            with RegsLock:
                RegsToPlc[0] = 3                  # resume machine
        Job_settings.zeroing_in_progress = False
        try:
            if ThckModule is not None:
                ThckModule.state.sweep_pause = False   # let the Processor resume
        except Exception:
            pass
        cur = dict(getattr(Job_settings, "t6", {}) or {})
        if cur.get("running"):
            _publish(running=False)


# ----------------------------
# Random images (PIL only)
# ----------------------------
def Random_Image():
    x = random.randint(1, 3)


    img_path = f"icons\\engraving{x}.png"
    max_size = (320, 240)

    im = to_pil( Image.open(img_path))

    im.thumbnail(max_size, Image.LANCZOS)
    return im, img_path

def to_pil(im):
    # If it's already PIL, return as-is
    if isinstance(im, Image.Image):
        return im

    # If it's a numpy array, convert
    if isinstance(im, np.ndarray):
        # Grayscale: (H, W)
        if im.ndim == 2:
            return Image.fromarray(im.astype(np.uint8), mode="L")

        # Color: (H, W, 3) or (H, W, 4)
        if im.ndim == 3:
            arr = im
            if arr.dtype != np.uint8:
                arr = np.clip(arr, 0, 255).astype(np.uint8)

            # Many camera/OpenCV frames are BGR; convert to RGB if needed:
            if arr.shape[2] == 3:
                arr = arr[..., ::-1]   # BGR->RGB
                return Image.fromarray(arr, mode="RGB")


def T1():
    i = 0
    t = time.time()
    Job_settings.txt1 = 'xxx'
    Job_settings.pil1, Job_settings.txt1 = Random_Image()

    tl = t
    while not Job_settings.stop_event.is_set():
        tc = 0.001
        time.sleep(max(tc - (time.time() - tl), 0))
        i += 1
        tl = time.time()
        if i >= 1000:

         

            Job_settings.pil1, Job_settings.txt1 = Random_Image()

            i = 0
            Job_settings.txt1 += f"  {round(time.time() - t, 3)}"
            t = time.time()

def T2():
    t = time.time()
    t1 = 0
    state = 0
    t21 = None

    RegsToPlc[1] = 1
    RegsToPlc[2] = 2
    im = to_pil(mv.img.last_img)
    im.thumbnail((640, 480), Image.LANCZOS)
    Job_settings.pil2 = im


    while not Job_settings.stop_event.is_set():
        if Job_settings.zeroing_in_progress:
            # Vision pauses ONLY during the active thickness-calibration measurement
            # (the flag is raised after the machine's 4 s settle and cleared when the
            # calibration ends). Vision is otherwise fully INDEPENDENT of the thickness
            # calibration gate — warmup / cal-required / never-calibrated do NOT block it.
            time.sleep(0.05)
            continue
        mv.img.refresh()
        if time.time()-t1>0.3 and state !=3:
            im2 = to_pil(mv.img.last_img)
            im2.thumbnail((320, 240), Image.LANCZOS)
            Job_settings.pil22 = im2
            t1=time.time()

        # Image analysis needs the job/spec geometry globals (mv.GetSpec creates them,
        # e.g. Body). Until a spec is loaded, keep the LIVE feed above running but SKIP
        # the analysis state machine — this is the vision's real prerequisite and is
        # independent of the thickness calibration gate.
        if not Job_settings.spec_loaded:
            time.sleep(max(0, 0.05 - (time.time() - t)))
            t = time.time()
            continue

        #print('mv.img.status:', mv.img.status,'state:',state)
        if state == 0 and mv.img.status == 3:
            log('conveyor was stopped')
            RegsToPlc[1]=3 #stop conveyor
            state = 1
        if state == 1 and mv.img.status == 4:
            mv.img.img_G=mv.img.last_img ##save current img as Green_img
            cv2.imwrite(f"native_img\\native_green{mv.Cfg_mv.counter+1}.jpg", mv.img.img_G)
            log('green light img is saved')
            #RegsToPlc[2:4] = [1, 2]
            RegsToPlc[2] = 1 ##turn green light off
            RegsToPlc[3] = 2 ##turn white light on
            state = 2
        if state == 2 and mv.img.WhiteOn == 1:
            mv.img.img_W = mv.img.last_img  ##save current img as White_img
            cv2.imwrite(f"native_img\\native_white{mv.Cfg_mv.counter +1}.jpg", mv.img.img_W)
            log('white light img is saved')
            with RegsLock:
                RegsToPlc[1:4]=[1,2,1]
            #RegsToPlc[1]=1 #start conveyor
            #RegsToPlc[2] = 2  ##turn green light on
            #RegsToPlc[3] = 1  ##turn white light off
            #print('RegsToPlc_change',RegsToPlc)

            #להוסיף בדיקה אם יש כבר תהליך ניתוח תמונה פעיל, להמתין עד שייסתיים
            log(f'start image processing prog-- No. {mv.Cfg_mv.counter+1}')
            t21 = threading.Thread(target=mv.run_prog, daemon=True)
            t21.start()
            state = 3
            mv.img.status = 0

        if state == 3 and t21 is not None and not t21.is_alive():  # analyze is completed
            RegsToPlc[4] = mv.Cfg_mv.sttprog ## report to plc if the bearing is valid/invalid
            if  mv.Cfg_mv.sttprog==2:
                stt_obj='OK'
            else:
                stt_obj='NOT OK'
            log(f'img processed done successfully-- object status is {stt_obj}','STATUS')
            ## refresh the current img_toshow at the UI
            im = to_pil(mv.img.img_toShow)
            im.thumbnail((640, 480), Image.LANCZOS)
            Job_settings.pil2 = im
            state = 0

        ctime=time.time() - t
        #print('ctime', round(ctime, 3))
        time.sleep(max(0,  0.05 -(time.time() - t) ) )

        t = time.time()
        #print(       f'state: {state}, mv.img.status:{mv.img.status}, looptime:{round(time.time() - t, 3)}sec')



def T3_thickness():
    global ThckModule
    ThckModule = thck.ThicknessModule()
    ThckModule.start()
    # Require a fresh calibration every session: drop any calibration loaded from JSON
    # so no measurement is produced until the operator calibrates this run.
    ThckModule.invalidate_calibration(persist=False)

    while not Job_settings.stop_event.is_set():
        try:
                Job_settings.pil3, Job_settings.thk_mean_mm, Job_settings.thk_p2p_um, Job_settings.thk_conicity_um = ThckModule.get_ui_packet(last_n=3, size_px=(420, 900))
                # Forward the thickness verdict to PLC reg 5 (only once work is allowed).
                code = ThckModule.poll_new_result_code()
                if code is not None and CalGate.WorkAllowed:
                    RegsToPlc[THK_RESULT_REG] = code
                    verdict = {1: 'OK', 2: 'thickness low', 3: 'thickness high', 4: 'conicity high'}.get(code, 'unknown')
                    log(f'thickness verdict reg5={code} ({verdict})', 'STATUS')
        except Exception as e:
            Job_settings.txt3 = f"THK update error: {e}"

        time.sleep(0.2)

    ThckModule.stop()


def run_calibration_sequence():
    """PC-driven calibration, wrapped by the Zero_thickness_gauge procedure.

    Triggered by the 'Start Calibration' button. The CORE is the original two-gauge
    calibration (move M6 to each Johnson gauge, sample, fit, return to the measurement
    position); the ENVELOPE is MD Docs/Zero_thickness_gauge_Procedure.md — pause the
    machine (R0=2), wait for it to stop, apply 0.5 s vibration damping before every
    sample, show status on the calibration window, perform a debug hold, return to
    Pos1, then resume (R0=3). Production (T2) is blocked for the whole sequence, and
    each M6 position is visited exactly once. Motion uses the proven one-shot
    primitive: RegsToPlc[6] = POS number ('Go position N'), flushed by T4; arrival is
    confirmed on reg 24 (with reg 25 as the actual position)."""
    tm = ThckModule
    if tm is None:
        log("calibration: thickness module not ready", "WARN")
        CalGate.CalibrationResult(False)
        return

    # Rolling reference = the PREVIOUS calibration, as persisted before THIS run.
    # Captured now (before compute_and_apply_calibration overwrites the JSON) so the
    # window can show each Johnson's measured THICKNESS under the previous calibration
    # (raw -> thickness) next to its defined value, revealing drift by eye. On the very
    # first calibration there is no reference yet -> ref stays None and thk shows "—".
    ref_a = ref_b = None
    g1_prev_thk = g2_prev_thk = None
    try:
        _cal_prev = (thck.load_config_json(tm.config_path).get("calibration", {}) or {})
        if _cal_prev.get("a") is not None and _cal_prev.get("b") is not None:
            ref_a, ref_b = float(_cal_prev["a"]), float(_cal_prev["b"])
        # "Previous calibration" column = the thickness the PREVIOUS calibration itself
        # displayed = the calibration before it (history[-2]) applied to its raw (history[-1]).
        _hist = _cal_prev.get("history", []) or []
        if len(_hist) >= 2:
            _prev, _rref = (_hist[-1] or {}), (_hist[-2] or {})
            if _rref.get("a") is not None and _rref.get("b") is not None:
                if _prev.get("raw_g1_mm") is not None:
                    g1_prev_thk = float(_rref["a"]) * float(_prev["raw_g1_mm"]) + float(_rref["b"])
                if _prev.get("raw_g2_mm") is not None:
                    g2_prev_thk = float(_rref["a"]) * float(_prev["raw_g2_mm"]) + float(_rref["b"])
    except Exception:
        ref_a = ref_b = None
        g1_prev_thk = g2_prev_thk = None

    def _publish(**changes):
        view = dict(getattr(Job_settings, "thk_cal", {}) or {})
        view.update(changes)
        Job_settings.thk_cal = view

    def _go_and_wait(pos, label):
        with RegsLock:
            RegsToPlc[M6_CMD_REG] = pos          # one-shot 'M6 -> Go position N' (T4 sends + clears)
        log(f"calibration: M6 -> POS {pos} ({label})", "STATUS")
        t0 = time.time()
        started = False          # became True once we saw M6 actually moving
        last_log = 0.0
        while time.time() - t0 < M6_MOVE_TIMEOUT_SEC:
            if Job_settings.stop_event.is_set():
                return False
            st = RegsFromPlc[M6_STATUS_REG]
            actpos = RegsFromPlc[M6_ACTPOS_REG]
            if st in M6_STATUS_MOVING:
                started = True
            if time.time() - last_log >= 1.0:
                last_log = time.time()
                log(f"calibration: waiting POS {pos} — M6 status={st} actpos={actpos} started={started}", "INFO")
            # Arrived when the PLC reports READY *after* we saw it move; the >5 s
            # fallback covers the case where M6 was already at the target (no motion).
            if st == M6_STATUS_READY and (started or time.time() - t0 >= 5.0):
                log(f"calibration: M6 arrived at POS {pos} (actpos={actpos})", "STATUS")
                return True
            time.sleep(0.05)
        log(f"calibration: TIMEOUT waiting for M6 at POS {pos}; "
            f"last status={RegsFromPlc[M6_STATUS_REG]} actpos={RegsFromPlc[M6_ACTPOS_REG]}", "WARN")
        return False

    def _measure(label):
        res = tm.measure_gauge()
        log(f"calibration: {label} measure result = {res}", "INFO")
        return res

    # --- nested helper: set the procedure "step line" on the calibration window ---
    def _step(text):
        _publish(step_text=text)
        log(f"calibration wrapper: {text}", "STATUS")

    # --- nested helper: one-shot machine pause/resume via R0 (procedure R0=2/3) ---
    def _machine(value, label):
        with RegsLock:
            RegsToPlc[0] = value
        log(f"calibration: R0={value} ({label})", "STATUS")

    # ===================================================================
    # ONE sequence: the button-triggered two-gauge calibration (core, from
    # the original code — motion to each Johnson gauge + sampling + fit) is
    # wrapped by the Zero_thickness_gauge procedure envelope
    # (MD Docs/Zero_thickness_gauge_Procedure.md): pause machine -> settle
    # -> [Pos5 damp+sample, Pos6 damp+sample, fit] -> debug hold -> return
    # Pos1 -> resume. Vision (T2) is paused ONLY from after the 4 s settle until the
    # sequence ends; M6 visits each position exactly once (no duplicated motion).
    # ===================================================================
    paused = False
    try:
        _publish(active=True, close=False, phase="measuring_g1", step_text="Starting — I'm in zeroing process",
                 cal_iso=time.strftime("%Y-%m-%d %H:%M:%S"),
                 g1_known_mm=tm.cfg.CAL_GAUGE_1_MM, g2_known_mm=tm.cfg.CAL_GAUGE_2_MM,
                 ref_a=ref_a, ref_b=ref_b,
                 g1_prev_thk_mm=g1_prev_thk, g2_prev_thk_mm=g2_prev_thk,
                 g1_raw_mm=None, g1_std_um=None, g2_raw_mm=None, g2_std_um=None,
                 a=None, b_um=None, message="")

        # Procedure steps 2-3: pause the machine, then wait for it to fully stop.
        _step("Pause machine (R0=2)")
        _machine(2, "pause machine")
        paused = True
        _step("Waiting 4s for machine to stop")
        time.sleep(4.0)

        # Machine has settled: NOW pause vision (T2) for the actual calibration
        # measurement. Vision ran normally through the pause + 4 s settle above, and
        # resumes when this sequence ends (flag cleared in the finally block).
        Job_settings.zeroing_in_progress = True

        # The sequence ALWAYS visits both gauges at both positions and samples each;
        # success/failure is decided only after both measurements, at the fit step.

        # --- Gauge 1 @ POS 5: move, vibration-damp, sample (procedure steps 4-7) ---
        _publish(phase="measuring_g1")
        _step("M6 -> Pos5 (Johnson 1)")
        if not _go_and_wait(POS_GAUGE_1, "Johnson 1"):
            log("calibration: WARNING — measuring gauge 1 without confirmed arrival at POS 5", "WARN")
        _step("Vibration damping 0.5s")
        time.sleep(0.5)
        _step("Measuring gauge 1")
        res1 = _measure("gauge 1")
        _publish(g1_raw_mm=res1.get("raw_median_mm"), g1_std_um=res1.get("std_um"), phase="g1_done")

        # --- Gauge 2 @ POS 6: move, vibration-damp, sample (same envelope) ---
        _publish(phase="measuring_g2")
        _step("M6 -> Pos6 (Johnson 2)")
        if not _go_and_wait(POS_GAUGE_2, "Johnson 2"):
            log("calibration: WARNING — measuring gauge 2 without confirmed arrival at POS 6", "WARN")
        _step("Vibration damping 0.5s")
        time.sleep(0.5)
        _step("Measuring gauge 2")
        res2 = _measure("gauge 2")
        _publish(g2_raw_mm=res2.get("raw_median_mm"), g2_std_um=res2.get("std_um"), phase="computing")

        # --- Save + compute the two-point linearization, then decide success/fail ---
        _step("Computing two-point fit")
        ok, info = tm.compute_and_apply_calibration(res1.get("raw_median_mm"), res2.get("raw_median_mm"))
        if ok:
            _publish(phase="done_ok", a=info["a"], b_um=info["b"] * 1000.0, message="",
                     step_text="Calibration OK")
            log(f"calibration OK: a={info['a']:.4f} b={info['b'] * 1000.0:+.1f}um", "STATUS")
        else:
            # A failed calibration must NOT leave the previous calibration in effect —
            # invalidate it so the machine produces no measurements until a valid one.
            tm.invalidate_calibration()
            extra = (f" [g1 n={res1.get('n')} std={res1.get('std_um')} | "
                     f"g2 n={res2.get('n')} std={res2.get('std_um')}]")
            _publish(phase="done_fail", message=(info.get("reason", "") + extra),
                     step_text="Calibration FAIL")
            log(f"calibration FAIL (calibration invalidated): {info.get('reason')}{extra}", "WARN")

        # Procedure step 10: temporary debug hold (window stays open).
        _step("Debug hold 20s (window stays open)")
        time.sleep(20.0)

        # Procedure step 11: return M6 to the measurement position (POS 1).
        _step("M6 -> Pos1 (return to measurement)")
        _go_and_wait(POS_MEASURE, "measurement")

        # Procedure step 12: resume the machine.
        _step("Resume machine (R0=3)")
        _machine(3, "resume machine")
        paused = False

        CalGate.CalibrationResult(ok)
    finally:
        # Always release the production block and make sure the machine is resumed,
        # even if the sequence aborted before step 12 (one-shot R0=3 is harmless if
        # it was already sent).
        Job_settings.zeroing_in_progress = False
        if paused:
            with RegsLock:
                RegsToPlc[0] = 3
            log("calibration: aborted — R0=3 resume issued in cleanup", "WARN")
        # The calibration window belongs to the sequence: request the UI to close it
        # once the sequence ends (for any reason). View-mode windows are unaffected.
        _publish(active=False, close=True)


def T4_plc_flush():
    """Send to PLC 20 regs only when any > 0;
     if nothing written for > 2 s send live sign (reg 0 = 1).
     After each send, read 40 regs from PLC into plc_read_registers."""
    global RegsToPlc, RegsTemp, RegsFromPlc, LastSentRegs

    def _write_20_and_read_40(client, regs_20):

        if not client.write_multiple_registers(0, regs_20):
            return False
        time.sleep(0.003)
        read_40 = client.read_holding_registers(0, 40)

        if read_40 is not None and len(read_40) >= 40:
            RegsFromPlc[:] = list(read_40)[:40]
        return True



    global RegsToPlc, RegsFromPlc

    client = ModbusClient(
        host="192.168.3.169",
        port=Cfg.PlcPort,
        auto_open=True,
        auto_close=False,
        timeout=0.1
    )
    regs=[0] * 20
    LastPlcWriteTime = time.time() - 10
    while not Job_settings.stop_event.is_set():
        #print('RegsToPlc',RegsToPlc)
        # בדיקה אם יש משהו לשלוח
        if any(val > 0 for val in RegsToPlc):

            # העתקה כדי למנוע שינוי תוך כדי שליחה
            #regs = RegsToPlc.copy()
            with RegsLock:
                regs = RegsToPlc.copy()
                RegsToPlc = [0] * 20
            #print("Sent_b:", regs)

            try:
                # שליחה
                ok = _write_20_and_read_40(client, regs)
                #ok=client.write_multiple_registers(0, regs)
                if ok:
                    print("Sent successfully:", regs)
                    # Mark the moment of the last real PLC write so the live-sign
                    # below only fires after genuine inactivity. Without this the
                    # timer stayed stale and a live-sign (R0=1, R6=0) could fire
                    # mid-jog and stop M6 out from under T6 (froze T6 at POS1).
                    LastPlcWriteTime = time.time()

                else:
                    print("Write failed")
                    with RegsLock:
                        RegsToPlc = regs
            except Exception as e:
                print("PLC error:", e)
                client.close()

        else:
            # Keep the feedback snapshot (RegsFromPlc) live even when we are not
            # sending commands. Reads were coupled to writes, so during a sustained
            # jog (no commands queued) R25/R24 went stale and T6 could never detect
            # its jog delta -> it froze at POS1. Refresh the read every cycle.
            try:
                read_40 = client.read_holding_registers(0, 40)
                if read_40 is not None and len(read_40) >= 40:
                    RegsFromPlc[:] = list(read_40)[:40]
            except Exception as e:
                print("PLC read error:", e)

            if time.time() - LastPlcWriteTime > Cfg.PlcLiveSignIntervalSeconds:

                if RegsToPlc[0] == 0:
                    RegsToPlc[0] = 1
                LastPlcWriteTime = time.time()

        time.sleep(0.02)

        ##RegsToPlc value:
        ##RegsToPlc[0]: 1- Livesign
        ##RegsToPlc[1]: 1- run con forward 2- run con backwards 3- stop con
        ##RegsToPlc[2]: 1- turn on green light 2- turn off green light
        ##RegsToPlc[3]: 1- turn on white light 2- turn off white light
        ##RegsToPlc[4]: 3- Image analysis OK 4- Image analysis NOT OK
        ##RegsToPlc[5]: thickness verdict 0-none 1-OK 2-below nominal 3-above nominal 4-conicity too high
        ##RegsToPlc[13]: calibration status 0-idle 1-measuring 2-gauge1 done 3-cal OK 4-cal FAIL


def T4_plc_flush_1():
    """Send to PLC 20 regs only when any > 0;
     if nothing written for > 2 s send live sign (reg 0 = 1).
     After each send, read 40 regs from PLC into plc_read_registers."""
    global RegsToPlc, RegsTemp, RegsFromPlc, LastSentRegs

    def _write_20_and_read_40(client, regs_20):

        client.write_multiple_registers(0, regs_20)
        read_40 = client.read_holding_registers(0, 40)

        if read_40 is not None and len(read_40) >= 40:
            RegsFromPlc[:] = list(read_40)[:40]
            return True

        return False

    LastPlcWriteTime = time.time() - 10
    LastSentRegs = [0] * 20

    client = ModbusClient(
        host="192.168.3.169",
        port=Cfg.PlcPort,
        auto_open=False,
        auto_close=False,
        timeout=0.2
    )

    conn = client.open()
    Cfg.plc_connected = conn

    while not Job_settings.stop_event.is_set():

        # reconnect אם החיבור נפל
        if not conn:
            try:
                client.close()
            except:
                pass

            conn = client.open()
            Cfg.plc_connected = conn
            time.sleep(0.1)
            print("new connect is open")
            continue

        try:

            if RegsToPlc != LastSentRegs:


                Regs=RegsToPlc.copy()
                ok =_write_20_and_read_40(client, Regs)
                print("Send change:", RegsToPlc,time.time(),'status:',ok)
                if not ok:
                    time.sleep(0.02)
                    ok = _write_20_and_read_40(client, Regs)
                    print('resend',ok)
                LastSentRegs = Regs
                LastPlcWriteTime = time.time()

            else:
                if time.time() - LastPlcWriteTime > Cfg.PlcLiveSignIntervalSeconds:

                    RegsTemp = RegsToPlc.copy()
                    RegsTemp[0] = 1

                    ok=_write_20_and_read_40(client, RegsTemp)
                    LastPlcWriteTime = time.time()
                    #print("PLC LiveSign",RegsTemp,time.time())
                    RegsTemp[0] = 0


        except Exception as e:

            print("PLC communication error:", e)

            conn = False
            Cfg.plc_connected = False

        time.sleep(0.02)


        ##RegsToPlc value:
        ##RegsToPlc[0]: 1- Livesign
        ##RegsToPlc[1]: 1- run con forward 2- run con backwards 3- stop con
        ##RegsToPlc[2]: 1- turn on green light 2- turn off green light
        ##RegsToPlc[3]: 1- turn on white light 2- turn off white light
        ##RegsToPlc[4]: 3- Image analysis OK 4- Image analysis NOT OK
        ##RegsToPlc[5]: thickness verdict 0-none 1-OK 2-below nominal 3-above nominal 4-conicity too high
        ##RegsToPlc[13]: calibration status 0-idle 1-measuring 2-gauge1 done 3-cal OK 4-cal FAIL




def GetNewJob_OLDREV(value):
    """Activate new job: resolve order from SQL, update Job_settings, load MV spec. Returns (success, part_name)."""
    value = (value or "").strip()
    if not value or not (len(value) > 1 and value[1:].isdigit()):
        return False, None
    try:
        order_id = int(value[1:]) if value[1:].isdigit() else int(value)
    except ValueError:
        return False, None
    Utils.conn_string2 = "mssql+pyodbc://pyapps:kngapps887@king-nt1/KINGDB?driver=SQL+Server+Native+Client+11.0"
    sql = """   SELECT
          RTRIM(dbo.kprdord.part_no) + '-' + RTRIM(dbo.KSPEC_NO.descript) + '.csv' AS filnemae,
          RTRIM(dbo.kprdord.part_no) + '-' + RTRIM(dbo.KSPEC_NO.descript)+ ' ' + dbo.kprdord.mida AS PARTNAME,
          dbo.KINGHAZT.ordr_no,
          dbo.kprdord.ordline_no, 
          dbo.kprdord.SERIALNAME,
          dbo.kprdord.qty,
          dbo.kprdord.hs_nomi, 
          dbo.kprdord.thik_min,
           dbo.kprdord.length, dbo.kprdord.mida
    FROM            dbo.kprdord INNER JOIN
                                         dbo.KSPEC_NO ON dbo.kprdord.spec_no = dbo.KSPEC_NO.spec_no INNER JOIN
                                         dbo.KINGHAZT ON dbo.kprdord.ordheadid = dbo.KINGHAZT.ordheadid
    WHERE        dbo.kprdord.num_id =""" + str(order_id)

    df = Utils.trim_all_columns(Utils.SqlSelect(sql))
   
    if df.shape[0] != 1:
        return False, None
    F = df.loc[0, "filnemae"]
    Filename  = F.replace(chr(92), "-").replace("/", "-")+".csv"
    P = df.loc[0, "PARTNAME"]
    Job_settings.wo_order_number = value
    Job_settings.wo_part_number = F
   
    if ".CSV" not in Filename:
        Filename = Filename + ".CSV"
    spec_paths = [
        os.path.join("W:\\MachineVisionTemplates", Filename),
        os.path.join("config", Filename),
    ]
    spec_loaded = False
    for path in spec_paths:
        if os.path.isfile(path):
            try:
                spec_df = pd.read_csv(path)
                spec_df.to_csv("config\\last_mv_spec.csv", index=False)
                mv.GetSpec(spec_df)
                spec_loaded = True
                Job_settings.spec_loaded = True   # vision geometry (Body, …) now defined
                break
            except Exception:
                continue
    if not spec_loaded:
        pass
    return True, P


def GetNewJob(value):
    """Activate new job: resolve order from SQL, update Job_settings, load MV spec, refresh thickness limits. Returns (success, part_name)."""
    value = (value or "").strip()
    if not value or not (len(value) > 1 and value[1:].isdigit()):
        return False, None
    try:
        order_id = int(value[1:]) if value[1:].isdigit() else int(value)
    except ValueError:
        return False, None
    Utils.conn_string2 = "mssql+pyodbc://pyapps:kngapps887@king-nt1/KINGDB?driver=SQL+Server+Native+Client+11.0"
    sql = """   SELECT
          RTRIM(dbo.kprdord.part_no) + '-' + RTRIM(dbo.KSPEC_NO.descript) + '.csv' AS filnemae,
          RTRIM(dbo.kprdord.part_no) + '-' + RTRIM(dbo.KSPEC_NO.descript)+ ' ' + dbo.kprdord.mida AS PARTNAME,
          dbo.KINGHAZT.ordr_no,
          dbo.kprdord.ordline_no,
          dbo.kprdord.SERIALNAME,
          dbo.kprdord.qty,
          dbo.kprdord.hs_nomi,
          dbo.kprdord.thik_min,
          dbo.kprdord.thik_max,
          dbo.kprdord.length,
          dbo.kprdord.mida
    FROM            dbo.kprdord INNER JOIN
                                         dbo.KSPEC_NO ON dbo.kprdord.spec_no = dbo.KSPEC_NO.spec_no INNER JOIN
                                         dbo.KINGHAZT ON dbo.kprdord.ordheadid = dbo.KINGHAZT.ordheadid
    WHERE        dbo.kprdord.num_id =""" + str(order_id)

    df = Utils.trim_all_columns(Utils.SqlSelect(sql))
    if df.shape[0] != 1:
        return False, None

    F = df.loc[0, "filnemae"]
    P = df.loc[0, "PARTNAME"]
    Job_settings.wo_order_number = value
    Job_settings.wo_part_number = F

    # Save job thickness limits only in config\config457_thk.json (T3 reads from there via get_job_limits)
    default_min_mm = 0.3
    default_max_mm = 3.0
    try:
        thik_min_val = df.loc[0, "thik_min"]
        thik_min_mm = float(thik_min_val) if thik_min_val is not None and not (isinstance(thik_min_val, float) and pd.isna(thik_min_val)) else default_min_mm
    except (TypeError, ValueError):
        thik_min_mm = default_min_mm
    try:
        thik_max_val = df.loc[0, "thik_max"]
        thik_max_mm = float(thik_max_val) if thik_max_val is not None and not (isinstance(thik_max_val, float) and pd.isna(thik_max_val)) else default_max_mm
    except (TypeError, ValueError):
        thik_max_mm = default_max_mm

    thk_config_path = os.path.join("config", "config457_thk.json")
    if os.path.isfile(thk_config_path):
        try:
            with open(thk_config_path, "r", encoding="utf-8") as f:
                thk_data = json.load(f)
            thk_data["thickness_min_mm"] = thik_min_mm
            thk_data["thickness_max_mm"] = thik_max_mm
            with open(thk_config_path, "w", encoding="utf-8") as f:
                json.dump(thk_data, f, indent=2)
        except Exception:
            pass

    base = (F.replace(chr(92), "-").replace("/", "-")) if F else ""
    Filename = base + ".csv" if base and not base.upper().endswith(".CSV") else (base if base else "")
    spec_loaded = False
   
    if Cfg.MvFolder and Filename:
        path = os.path.join(Cfg.MvFolder, Filename)
        if os.path.isfile(path):
            try:
                spec_df = pd.read_csv(path)
                mv.GetSpec(spec_df)
                spec_loaded = True
                Job_settings.spec_loaded = True   # vision geometry (Body, …) now defined
            except Exception:
                pass
    if not spec_loaded:
        return False, None
    return True, P


# ----------------------------
# UI Form
# ----------------------------
class EL_UI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("King Engine Bearings End Line Machine 457")
        self.geometry("1650x850")
        self.resizable(False, False)

        self.pil_plc_off = Image.open("icons\\PLC_off.png").resize((48, 48), Image.LANCZOS)
        self.tk_plc_off = ImageTk.PhotoImage(self.pil_plc_off)
        self.pil_plc_on = Image.open("icons\\PLC_on.png").resize((48, 48), Image.LANCZOS)

        self.tk_plc_on = ImageTk.PhotoImage(self.pil_plc_on)
        self._build_ui()

        self.after(100, self._refresh_from_Job_settings)

    def NewJob(self, title: str = "Settings"):
        # Use separate _newjob_win so New Job dialog is independent of other settings windows
        if getattr(self, "_newjob_win", None) is not None and self._newjob_win.winfo_exists():
            try:
                self._newjob_win.deiconify()
                self._newjob_win.lift()
                self._newjob_win.focus_force()
            except Exception:
                pass
            return

        win = tk.Toplevel(self)
        self._newjob_win = win
        win.title(title)
        win.transient(self)
        win.grab_set()
        win.resizable(False, False)

        w, h = 1500, 750
        try:
            self.update_idletasks()
            x = self.winfo_rootx() + (self.winfo_width() - w) // 2
            y = self.winfo_rooty() + (self.winfo_height() - h) // 2
            win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            win.geometry(f"{w}x{h}")

        def _close():

            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()
            self._newjob_win = None

        win.protocol("WM_DELETE_WINDOW", _close)

        container = ttk.Frame(win, padding=12)
        container.pack(fill="both", expand=True)

        body = ttk.Frame(container, borderwidth=1, relief="solid")
        body.pack(fill="both", expand=True)

        # ===============================
        # Top input (upper-left)
        # ===============================
        top_row = ttk.Frame(body, padding=(10, 10, 10, 0))
        top_row.pack(anchor="nw", fill="x")

        ttk.Label(top_row, text="New Order Nbr:", font=("Arial", 18, "bold")).pack(side="left")

        self.newjob_text_var = tk.StringVar()

        entry = tk.Entry(
            top_row,
            textvariable=self.newjob_text_var,
            font=("Arial", 22, "bold"),
            width=20,  # 20 characters visible
            justify="left",
        )
        entry.pack(side="left", padx=(10, 0))
        entry.focus_set()

        # ===============================
        # Two info lines below
        # ===============================
        info_frame = ttk.Frame(body, padding=(20, 15, 10, 0))
        info_frame.pack(anchor="nw", fill="x")

        # existing var1/var2 placeholders (StringVar so they refresh automatically)
        if not hasattr(self, "var_order_nbr"):
            self.var_order_nbr = tk.StringVar(value="—")  # var1
        if not hasattr(self, "var_part_nbr"):
            self.var_part_nbr = tk.StringVar(value="—")  # var2

        # line1: ['Order Nbr:'] [var1]
        row1 = ttk.Frame(info_frame)
        row1.pack(anchor="w", pady=4)
        ttk.Label(row1, text="Order Nbr:", font=("Arial", 14, "bold")).pack(side="left")
        ttk.Label(row1, textvariable=self.var_order_nbr, font=("Arial", 14)).pack(side="left", padx=8)

        # line2: ['Part Nbr:'] [var2]
        row2 = ttk.Frame(info_frame)
        row2.pack(anchor="w", pady=4)
        ttk.Label(row2, text="Part Nbr:", font=("Arial", 14, "bold")).pack(side="left")
        ttk.Label(row2, textvariable=self.var_part_nbr, font=("Arial", 14)).pack(side="left", padx=8)

        # ===============================
        # Bottom buttons (same size/shape)
        # ===============================
        bottom = ttk.Frame(container)
        bottom.pack(fill="x", pady=(12, 0))

        btn_opts = dict(padding=(14, 10))

        btn_save = ttk.Button(
            bottom,
            text="Save and exit",
            command=_close,
            state="disabled",  # disabled until valid

        )
        btn_save.pack(side="left", expand=True, fill="x", padx=6)

        ttk.Button(
            bottom,
            text="Exit without saving",
            command=_close,

        ).pack(side="left", expand=True, fill="x", padx=6)

        # ===============================
        # Validation on Enter
        # ===============================
        def _on_validate_enter(_evt=None):
            value = self.newjob_text_var.get().strip()

            # enforce max length 20
            if len(value) > 20:
                value = value[:20]
                self.newjob_text_var.set(value)

            if not value:
                win.bell()
                btn_save.config(state="disabled")
                return "break"

            # valid only if txt[1:] all digits
            is_valid = (len(value) > 1 and value[1:].isdigit())

            if is_valid:
                success, part_name =     GetNewJob(value)
                if success:
                    print("WO number format OK:", value)
                    self.var_order_nbr.set(value)
                    self.var_part_nbr.set(part_name if part_name else f"P-{value[1:]}")
                    btn_save.config(state="normal")
                else:
                    win.bell()
                    btn_save.config(state="disabled")
            else:
                win.bell()
                btn_save.config(state="disabled")

            return "break"

        entry.bind("<Return>", _on_validate_enter)

        # Bring New Job dialog to front so it is visible
        win.lift()
        win.focus_force()

    def _open_settings_screen(self, title: str = "Settings"):
        if title == "Thickness Adjustment Settings":
            self._open_thickness_adjustment_screen()
            return
        if getattr(self, "_settings_win", None) is not None and self._settings_win.winfo_exists():
            try:
                self._settings_win.deiconify()
                self._settings_win.lift()
                self._settings_win.focus_force()
            except Exception:
                pass
            return

        win = tk.Toplevel(self)
        self._settings_win = win
        win.title(title)
        win.transient(self)
        win.grab_set()
        win.resizable(False, False)

        w, h = 900, 550
        try:
            self.update_idletasks()
            x = self.winfo_rootx() + (self.winfo_width() - w) // 2
            y = self.winfo_rooty() + (self.winfo_height() - h) // 2
            win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            win.geometry(f"{w}x{h}")

        def _close():
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()
            self._settings_win = None

        win.protocol("WM_DELETE_WINDOW", _close)

        container = ttk.Frame(win, padding=12)
        container.pack(fill="both", expand=True)

        body = ttk.Frame(container, borderwidth=1, relief="solid")
        body.pack(fill="both", expand=True)

        bottom = ttk.Frame(container)
        bottom.pack(fill="x", pady=(12, 0))

        btn_opts = dict(padding=(14, 10))
        ttk.Button(bottom, text="Save and exit", command=_close, **btn_opts).pack(side="left", expand=True, fill="x", padx=6)
        ttk.Button(bottom, text="Exit without saving", command=_close, **btn_opts).pack(side="left", expand=True, fill="x", padx=6)
        ttk.Button(bottom, text="Resolution Calibration", command=mv.Image_calibration, **btn_opts).pack(side="left", expand=True, fill="x", padx=6)

    def _open_thickness_adjustment_screen(self):
        """Open form to modify all thck.Cfg (config457_thk.json) properties. Save / Reload / Close. Protected by Password from config457.json."""
        if getattr(self, "_thk_settings_win", None) is not None:
            try:
                if self._thk_settings_win.winfo_exists():
                    self._thk_settings_win.lift()
                    self._thk_settings_win.focus_force()
                    return
            except Exception:
                self._thk_settings_win = None

        entered = simpledialog.askstring("Thickness Adjustment", "Enter password:", show="*")
        if entered is None:
            return
        if entered != getattr(Cfg, "Password", '9114'):
            messagebox.showerror("Access denied", "Incorrect password.")
            return

        ThkConfigPath = os.path.join("config", "config457_thk.json")

        # Flat keys for form: (label, key_path). key_path "sensors.TOP.ip" etc. or scalar key.
        def _thk_flat_keys():
            return [
                ("schema_version", "schema_version"),
                ("TOP ip", "sensors.TOP.ip"),
                ("TOP port", "sensors.TOP.port"),
                ("BOTTOM ip", "sensors.BOTTOM.ip"),
                ("BOTTOM port", "sensors.BOTTOM.port"),
                ("socket_timeout_sec", "socket_timeout_sec"),
                ("frame_interval_sec", "frame_interval_sec"),
                ("time_sync_threshold_sec", "time_sync_threshold_sec"),
                ("live_update_hz", "live_update_hz"),
                ("reference_height_mm", "reference_height_mm"),
                ("error_threshold_mm", "error_threshold_mm"),
                ("axial_span_mm", "axial_span_mm"),
                ("thickness_min_mm", "thickness_min_mm"),
                ("thickness_max_mm", "thickness_max_mm"),
                ("max_error_count", "max_error_count"),
                ("target_points", "target_points"),
                ("trim_lo", "trim_lo"),
                ("trim_hi", "trim_hi"),
                ("p2p_thresh_um", "p2p_thresh_um"),
                ("conicity_thresh_um", "conicity_thresh_um"),
                ("nominal_thickness_mm_default", "nominal_thickness_mm_default"),
                ("tol_um_default", "tol_um_default"),
            ]

        def _get_nested(data, path):
            parts = path.split(".")
            cur = data
            for p in parts:
                cur = cur.get(p) if isinstance(cur, dict) else cur
                if cur is None:
                    return None
            return cur

        def _set_nested(data, path, value):
            parts = path.split(".")
            cur = data
            for i, p in enumerate(parts[:-1]):
                if p not in cur:
                    cur[p] = {}
                cur = cur[p]
            cur[parts[-1]] = value

        def _load_thk_data():
            if not os.path.isfile(ThkConfigPath):
                return {}
            with open(ThkConfigPath, "r", encoding="utf-8") as f:
                return json.load(f)

        win = tk.Toplevel(self)
        self._thk_settings_win = win
        win.title("Thickness Adjustment Settings")
        win.transient(self)
        win.grab_set()
        win.resizable(True, True)

        w, h = 560, 620
        try:
            self.update_idletasks()
            x = self.winfo_rootx() + (self.winfo_width() - w) // 2
            y = self.winfo_rooty() + (self.winfo_height() - h) // 2
            win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            win.geometry(f"{w}x{h}")

        def close_win():
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()
            self._thk_settings_win = None

        win.protocol("WM_DELETE_WINDOW", close_win)

        container = ttk.Frame(win, padding=12)
        container.pack(fill="both", expand=True)

        form_frame = ttk.LabelFrame(container, text="thck.Cfg (config457_thk.json)", padding=8)
        form_frame.pack(fill="both", expand=True)

        flat = _thk_flat_keys()
        mid = (len(flat) + 1) // 2
        left_frame = ttk.Frame(form_frame)
        right_frame = ttk.Frame(form_frame)
        left_frame.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right_frame.pack(side="left", fill="both", expand=True)
        left_frame.grid_columnconfigure(1, weight=1)
        right_frame.grid_columnconfigure(1, weight=1)

        thk_vars = {}
        data = _load_thk_data()
        for idx, (label, key_path) in enumerate(flat):
            parent = left_frame if idx < mid else right_frame
            row_idx = idx if idx < mid else idx - mid
            val = _get_nested(data, key_path)
            if val is None and "." not in key_path:
                val = data.get(key_path)
            disp = "" if val is None else str(val)
            ttk.Label(parent, text=label + ":", width=22, anchor="w").grid(row=row_idx, column=0, sticky="w", padx=(0, 6), pady=2)
            var = tk.StringVar(value=disp)
            thk_vars[key_path] = var
            entry = ttk.Entry(parent, textvariable=var, width=20)
            entry.grid(row=row_idx, column=1, sticky="ew", pady=2)
            if key_path == "schema_version":
                entry.config(state="disabled")

        bottom_btns = ttk.Frame(container)
        bottom_btns.pack(fill="x", pady=(12, 0))

        def save_thk():
            data = _load_thk_data()
            for label, key_path in flat:
                var = thk_vars.get(key_path)
                if var is None:
                    continue
                raw = var.get().strip()
                if key_path == "schema_version":
                    continue
                if key_path in ("sensors.TOP.port", "sensors.BOTTOM.port", "max_error_count", "target_points", "live_update_hz"):
                    try:
                        val = int(raw) if raw else 0
                    except ValueError:
                        val = 0
                elif key_path in ("socket_timeout_sec", "frame_interval_sec", "time_sync_threshold_sec", "reference_height_mm",
                                  "error_threshold_mm", "axial_span_mm", "thickness_min_mm", "thickness_max_mm",
                                  "trim_lo", "trim_hi", "p2p_thresh_um", "conicity_thresh_um", "nominal_thickness_mm_default", "tol_um_default"):
                    try:
                        val = float(raw) if raw else 0.0
                    except ValueError:
                        val = 0.0
                else:
                    val = raw if raw else ""
                _set_nested(data, key_path, val)
            os.makedirs(os.path.dirname(ThkConfigPath), exist_ok=True)
            with open(ThkConfigPath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            # Note: thickness_Module_V3_w no longer exposes a module-level Cfg.
            # Edits to the JSON take effect on the next run of T3_thickness.

        def reload_thk():
            data = _load_thk_data()
            for label, key_path in flat:
                var = thk_vars.get(key_path)
                if var is None:
                    continue
                val = _get_nested(data, key_path)
                if val is None and "." not in key_path:
                    val = data.get(key_path)
                var.set("" if val is None else str(val))

        btn_opts = dict(padding=(14, 10))
        ttk.Button(bottom_btns, text="Save", command=save_thk, **btn_opts).pack(side="left", expand=True, fill="x", padx=6)
        ttk.Button(bottom_btns, text="Reload", command=reload_thk, **btn_opts).pack(side="left", expand=True, fill="x", padx=6)
        ttk.Button(bottom_btns, text="Close", command=close_win, **btn_opts).pack(side="left", expand=True, fill="x", padx=6)

    def _build_ui(self):
        footer = ttk.Frame(self, padding=10)
        footer.pack(side="bottom", fill="x")

        main = ttk.Frame(self, padding=10)
        main.pack(side="top", fill="both", expand=True)

        header = ttk.Frame(main)
        header.pack(fill="x")

        pil = Image.open("icons\\logo.png").resize((220, 60), Image.LANCZOS)
        self.logo_img = ImageTk.PhotoImage(pil)
        ttk.Label(header, image=self.logo_img).pack(side="left")

        wo_header = ttk.Frame(header)
        wo_header.pack(side="left", padx=(12, 0))
        wo_header.bind("<Button-1>", lambda e: self.NewJob())
        wo_header.bind("<Double-Button-1>", lambda e: self.NewJob())

        def add_hdr_row(r, title):
            ttk.Label(wo_header, text=title + ":", width=14).grid(row=r, column=0, sticky="w")
            val = ttk.Label(wo_header, text="", width=18)
            val.grid(row=r, column=1, sticky="w")
            return val

        self.hdr_wo_order_num   = add_hdr_row(0, "Order Number")
        self.hdr_wo_part_num    = add_hdr_row(1, "Part Number")
        self.hdr_wo_order_qty   = add_hdr_row(2, "Order Quantity")
        self.hdr_wo_obj_counter = add_hdr_row(3, "Object Counter")
        for lbl in (self.hdr_wo_order_num, self.hdr_wo_part_num):
            lbl.bind("<Button-1>", lambda e: self.NewJob())
        wo_header.grid_columnconfigure(1, weight=1)

        # Calibration status + last calibration (top of screen)
        cal_hdr = ttk.Frame(header)
        cal_hdr.pack(side="left", padx=(24, 0))
        self.hdr_cal_status = ttk.Label(cal_hdr, text="", font=("Arial", 14, "bold"))
        self.hdr_cal_status.pack(anchor="w")
        self.hdr_last_cal = ttk.Label(cal_hdr, text="", font=("Arial", 10))
        self.hdr_last_cal.pack(anchor="w")

        ### Upper Right
        right_bar = ttk.Frame(header)
        right_bar.pack(side="right")

        plc_frame = ttk.Frame(right_bar)
        plc_frame.pack(side="left", padx=(0, 12))
#
        self.plc_icon_off = os.path.join("icons", "PLC_off.png")
        self.plc_icon_on = os.path.join("icons", "PLC_on.png")

        self.plc_icon_lbl = ttk.Label(plc_frame)
        self.plc_icon_lbl.pack(side="left")
        self.plc_text_lbl = ttk.Label(plc_frame, text="")
        self.plc_text_lbl.pack(side="left", padx=(6, 0))

        ttk.Button(right_bar, text="Refresh", command=self._on_refresh).pack(side="left", padx=(0, 6))
        ttk.Button(right_bar, text="Close", command=self._on_close).pack(side="left")

        ### end plc

        imgs = ttk.Frame(main)
        imgs.pack(side="top", fill="both", expand=True)

        self._info_block(imgs, 0)
        self._analysis_block(imgs, 1)
        self._thickness_block(imgs, 2)

        imgs.grid_columnconfigure(0, weight=2, uniform='panels')
        imgs.grid_columnconfigure(1, weight=3, uniform='panels')
        imgs.grid_columnconfigure(2, weight=2, uniform='panels')
        imgs.grid_rowconfigure(0, weight=1)

        buttons = ttk.Frame(footer)
        buttons.pack(fill="x")

        ttk.Button(buttons, text="⚙ Engraving Adjustment",
                   command=lambda: self._open_settings_screen("Engraving Adjustment Settings")).pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(buttons, text="⚙ Camera Adjustment",
                   command=lambda: self._open_settings_screen("Camera Adjustment Settings")).pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(buttons, text="⚙ Thck Adjustment",
                   command=lambda: self._open_settings_screen("Thickness Adjustment Settings")).pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(buttons, text="⚙ NewJob",
                   #command=lambda: self._open_settings_screen("Select Work Order")).pack(side="left", expand=True, fill="x", padx=5)
                   command=lambda: self.NewJob ()).pack(side="left", expand=True,fill="x", padx=5)
        ttk.Button(buttons, text="Calibration",
                   command=self._open_view_calibration).pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(buttons, text="Start Calibration-Zeroing",
                   command=self._start_calibration).pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(buttons, text="Horizontal Zeroing Temporary",
                   command=self._start_horizontal_zeroing_temp).pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(buttons, text="Modify Settings",
                   command=self._open_modify_settings).pack(side="left", expand=True, fill="x", padx=5)

    def _open_modify_settings(self):
        """Open form to modify all settings (scalars + Cfg). Save/Reload/Close. Only non-None values shown for editing."""
        if getattr(self, "_modify_settings_win", None) is not None:
            try:
                self._modify_settings_win.lift()
                self._modify_settings_win.focus_force()
            except Exception:
                self._modify_settings_win = None
            if self._modify_settings_win is not None:
                return

        win = tk.Toplevel(self)
        self._modify_settings_win = win
        win.title("Modify Settings")
        win.transient(self)
        win.grab_set()
        win.resizable(True, True)

        w, h = 900, 420
        try:
            self.update_idletasks()
            x = self.winfo_rootx() + (self.winfo_width() - w) // 2
            y = self.winfo_rooty() + (self.winfo_height() - h) // 2
            win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            win.geometry(f"{w}x{h}")

        def close_win():
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()
            self._modify_settings_win = None

        win.protocol("WM_DELETE_WINDOW", close_win)

        # Bottom button bar: pack first so it stays visible at bottom
        bottom_btns = ttk.Frame(win, padding=(12, 0))
        bottom_btns.pack(side="bottom", fill="x", pady=(8, 12))

        container = ttk.Frame(win, padding=12)
        container.pack(fill="both", expand=True)

        # --- Scalars (all 5; empty = None on save) ---
        scalars_frame = ttk.LabelFrame(container, text="Scalar settings", padding=8)
        scalars_frame.pack(fill="x", pady=(0, 8))

        scalar_vars = {}
        row_idx = 0
        for key in Job_settings_.JsonKeys:
            val = getattr(Job_settings, key, None)
            ttk.Label(scalars_frame, text=key + ":", width=22, anchor="w").grid(row=row_idx, column=0, sticky="w", padx=(0, 6), pady=2)
            var = tk.StringVar(value="" if val is None else str(val))
            scalar_vars[key] = var
            ttk.Entry(scalars_frame, textvariable=var, width=30).grid(row=row_idx, column=1, sticky="ew", pady=2)
            row_idx += 1
        scalars_frame.grid_columnconfigure(1, weight=1)

        # --- Cfg (config457.json) ---
        cfg_frame = ttk.LabelFrame(container, text="Cfg (config457.json)", padding=8)
        cfg_frame.pack(fill="x", pady=(0, 8))

        cfg_vars = {}
        cfg_row = 0
        for attr in Cfg_.ConfigAttrs:
            val = getattr(Cfg, attr, "")
            ttk.Label(cfg_frame, text=attr + ":", width=18, anchor="w").grid(row=cfg_row, column=0, sticky="w", padx=(0, 6), pady=2)
            var = tk.StringVar(value="" if val is None else str(val))
            cfg_vars[attr] = var
            ttk.Entry(cfg_frame, textvariable=var, width=50).grid(row=cfg_row, column=1, sticky="ew", pady=2)
            cfg_row += 1
        # plc_connected: runtime only (Cfg), read-only in UI
        ttk.Label(cfg_frame, text="plc_connected:", width=18, anchor="w").grid(row=cfg_row, column=0, sticky="w", padx=(0, 6), pady=2)
        plc_status = "OnLine" if getattr(Cfg, "plc_connected", False) else "OffLine"
        plc_connected_lbl = ttk.Label(cfg_frame, text=plc_status, width=50, anchor="w")
        plc_connected_lbl.grid(row=cfg_row, column=1, sticky="w", pady=2)
        cfg_row += 1
        cfg_frame.grid_columnconfigure(1, weight=1)

        def save_form():
            for key in Job_settings_.JsonKeys:
                var = scalar_vars.get(key)
                if var is None:
                    continue
                raw = var.get().strip()
                if key == "current_pack_job":
                    try:
                        setattr(Job_settings, key, int(raw) if raw else None)
                    except ValueError:
                        setattr(Job_settings, key, None)
                else:
                    setattr(Job_settings, key, raw if raw else None)

            for attr in Cfg_.ConfigAttrs:
                var = cfg_vars.get(attr)
                if var is None:
                    continue
                raw = var.get().strip()
                if attr == "PlcPort":
                    try:
                        setattr(Cfg, attr, int(raw) if raw else 502)
                    except ValueError:
                        setattr(Cfg, attr, 502)
                elif attr == "PlcLiveSignIntervalSeconds":
                    try:
                        setattr(Cfg, attr, float(raw) if raw else 2.0)
                    except ValueError:
                        setattr(Cfg, attr, 2.0)
                else:
                    setattr(Cfg, attr, raw if raw else "")

            Cfg.save()

            Job_settings.save()
            if hasattr(self, "_update_t1_packjob_view"):
                self._update_t1_packjob_view()
            if hasattr(self, "hdr_wo_order_num"):
                self.hdr_wo_order_num.config(text=Job_settings.wo_order_number or "")
                self.hdr_wo_part_num.config(text=Job_settings.wo_part_number or "")
                self.hdr_wo_order_qty.config(text=Job_settings.wo_order_quantity or "")
                self.hdr_wo_obj_counter.config(text=Job_settings.wo_object_counter or "")

        def reload_form():
            Job_settings.reload()
            Cfg._load()
            for key in Job_settings_.JsonKeys:
                var = scalar_vars.get(key)
                if var is not None:
                    val = getattr(Job_settings, key, None)
                    var.set("" if val is None else str(val))
            for attr in Cfg_.ConfigAttrs:
                var = cfg_vars.get(attr)
                if var is not None:
                    val = getattr(Cfg, attr, "")
                    var.set("" if val is None else str(val))

        btn_opts = dict(padding=(14, 10))
        ttk.Button(bottom_btns, text="Save", command=save_form, **btn_opts).pack(side="left", expand=True, fill="x", padx=6)
        ttk.Button(bottom_btns, text="Reload", command=reload_form, **btn_opts).pack(side="left", expand=True, fill="x", padx=6)
        ttk.Button(bottom_btns, text="Close", command=close_win, **btn_opts).pack(side="left", expand=True, fill="x", padx=6)

    def _info_block(self, parent, col):
        frame = ttk.Frame(parent, borderwidth=1, relief="solid", padding=8)
        frame.grid(row=0, column=col, padx=10, sticky="nsew")
        ttk.Label(frame, text="Work Order Info", font=("Arial", 10, "bold")).pack(anchor="w", pady=(0, 6))

        t1_table_wrap = ttk.Frame(frame)
        t1_table_wrap.pack(fill="x", pady=(0, 8))

        self.t1_tree = ttk.Treeview(t1_table_wrap, show="headings", selectmode="none", height=4)
        self.t1_tree.pack(side="left", fill="both", expand=True)

        t1_scroll = ttk.Scrollbar(t1_table_wrap, orient="vertical", command=self.t1_tree.yview)
        self.t1_tree.configure(yscrollcommand=t1_scroll.set)
        t1_scroll.pack(side="right", fill="y")

        self.t1_tree.tag_configure("highlight", background="yellow")

        # --- Image holder (same behavior style as analysis/thickness) ---
        bottom = ttk.Frame(frame, borderwidth=2, relief="sunken")
        bottom.pack(fill="both", expand=True)

        self.info_img_lbl = ttk.Label(bottom, text="(No image)")
        self.info_img_lbl.pack(expand=True)

        return frame

    def _update_t1_packjob_view(self):
        if not hasattr(self, "t1_tree"):
            return

        for iid in self.t1_tree.get_children():
            self.t1_tree.delete(iid)

        df = Job_settings.pack_jobs

        if df is None or df.empty:
            self.t1_tree["columns"] = []
            if hasattr(self, "info_img_lbl"):
                self.info_img_lbl.configure(text="(No pack-jobs data)", image="")
                self.info_img_lbl.image = None
            return

        if df.shape[1] < 5:
            self.t1_tree["columns"] = []
            if hasattr(self, "info_img_lbl"):
                self.info_img_lbl.configure(text="(Pack-jobs Excel must have >= 5 columns)", image="")
                self.info_img_lbl.image = None
            return

        headers = [str(c) for c in df.columns[:4]]
        self.t1_tree["columns"] = list(range(4))
        for i, h in enumerate(headers):
            self.t1_tree.heading(i, text=h)
            self.t1_tree.column(i, width=110, stretch=True, anchor="w")

        try:
            raw = getattr(Job_settings, "current_pack_job", None)
            highlight_idx = int(raw) if raw is not None and str(raw).strip() != "" else 0
        except (TypeError, ValueError):
            highlight_idx = 0
        max_rows = min(len(df), 4)
        highlight_idx = max(0, min(highlight_idx, max_rows - 1)) if max_rows else 0

        for r in range(max_rows):
            vals = [str(df.iat[r, c]) for c in range(4)]
            tags = ("highlight",) if r == highlight_idx else ()
            self.t1_tree.insert("", "end", values=vals, tags=tags)

        self.t1_tree.update_idletasks()

        def _show_pil1_fallback():
            """When pack-job has no valid image, show engraving (pil1) instead of 'not found'."""
            if not hasattr(self, "info_img_lbl"):
                return
            if Job_settings.pil1 is not None:
                Job_settings.img1 = ImageTk.PhotoImage(Job_settings.pil1)
                self.info_img_lbl.configure(image=Job_settings.img1, text="")
                self.info_img_lbl.image = Job_settings.img1
            else:
                self.info_img_lbl.configure(text="(No image)", image="")
                self.info_img_lbl.image = None

        img_path = str(df.iat[highlight_idx, 4]) if 0 <= highlight_idx < len(df) and df.shape[1] > 4 else ""
        if not img_path or str(img_path).lower() == "nan":
            _show_pil1_fallback()
            return

        img_path = os.path.normpath(img_path)
        if not os.path.isabs(img_path):
            img_path = os.path.normpath(os.path.join(os.getcwd(), img_path))
        if not os.path.exists(img_path):
            _show_pil1_fallback()
            return

        try:
            pil = Image.open(img_path)
            pil = pil.resize((220, 220), Image.LANCZOS)
            self._info_pack_photo = ImageTk.PhotoImage(pil)
            self.info_img_lbl.configure(image=self._info_pack_photo, text="")
            self.info_img_lbl.image = self._info_pack_photo
        except Exception:
            _show_pil1_fallback()

    def _analysis_block(self, parent, col):
        frame = ttk.Frame(parent, borderwidth=1, relief="solid", padding=5)
        frame.grid(row=0, column=col, padx=10, sticky="nsew")
        ttk.Label(frame, text="Image Analysis", font=("Arial", 10, "bold")).pack(anchor="w", pady=(0, 6))

        status_box = ttk.Frame(frame, borderwidth=1, relief="solid", padding=6)
        status_box.pack(fill="x", pady=(0, 8))
        self.analysis_status_lbl = ttk.Label(status_box, text="👍 / 👎", font=("Arial", 14))
        self.analysis_status_lbl.pack(anchor="center")

        img_holder = ttk.Frame(frame, borderwidth=2, relief="sunken")
        img_holder.pack(fill="both", expand=True)
        self.analysis_live_img_lbl = ttk.Label(img_holder)
        self.analysis_live_img_lbl.pack(expand=True)
        self.analysis_img_lbl = ttk.Label(img_holder)
        self.analysis_img_lbl.pack(expand=True)

        return frame

    def _thickness_block(self, parent, col):
        outer = ttk.Frame(parent, borderwidth=1, relief="solid", padding=5)
        outer.grid(row=0, column=col, padx=10, sticky="nsew")

        title_row = ttk.Frame(outer)
        title_row.pack(fill="x", pady=(0, 6))

        ttk.Label(title_row, text="Thickness Measurement", font=("Arial", 10, "bold")).pack(side="left")

        big = ttk.Frame(outer, borderwidth=2, relief="sunken")
        big.pack(fill="both", expand=True)

        self.thk_img_lbl = ttk.Label(big)
        self.thk_img_lbl.pack(expand=True, fill="both")

        return outer

    def _on_refresh(self):
        Job_settings.wo_order_number = "Default0"
        Job_settings.wo_part_number = "Default1"
        Job_settings.wo_order_quantity = "Default2"
        Job_settings.wo_object_counter = "Default3"
        self._refresh_from_Job_settings()

    def _on_close(self):
        global RegsToPlc
        RegsToPlc[1] = 3  # stop conveyor
        RegsToPlc[2] = 1  ##turn green light off
        RegsToPlc[3] = 1  ##turn white light off
        time.sleep(0.2)
        Job_settings.stop_event.set()
        self.destroy()

    def _update_plc_indicator(self):
        if not hasattr(self, "plc_text_lbl"):
            return

        connected = bool(getattr(Cfg, "plc_connected", False))
        status_text = "PLC: OnLine" if connected else "PLC: OffLine"
        self.plc_text_lbl.config(text=status_text)

        tk_img = self.tk_plc_on if connected else self.tk_plc_off

        # update only when state changes
        if getattr(self, "_plc_last_state", None) != connected:
            self.plc_icon_lbl.config(image=tk_img)
            self.plc_icon_lbl.image = tk_img  # keep reference!
            self._plc_last_state = connected

    # ----------------------------
    # Thickness calibration window (auto-opens during a calibration sequence)
    # ----------------------------
    # phase -> (display text, color). Driven by Job_settings.thk_cal["phase"] (set by T3).
    _CAL_PHASES = {
        "measuring_g1": ("מודד מדיד 1…", "#000000"),
        "g1_done":      ("מדיד 1 נמדד — ממתין למדיד 2", "#000000"),
        "measuring_g2": ("מודד מדיד 2…", "#000000"),
        "g2_done":      ("מדיד 2 נמדד — ממתין לחישוב", "#000000"),
        "computing":    ("מחשב כיול…", "#000000"),
        "done_ok":      ("כיול הושלם בהצלחה ✓", "#127a1f"),
        "done_fail":    ("כיול נכשל ✗", "#b00000"),
        "view":         ("כיול נוכחי", "#000000"),
        "idle":         ("", "#000000"),
    }

    def _build_thk_cal_window(self):
        win = tk.Toplevel(self)
        self._thk_cal_win = win
        win.title("Thickness Calibration")
        win.transient(self)
        win.resizable(True, True)

        w, h = 640, 340
        try:
            self.update_idletasks()
            x = self.winfo_rootx() + (self.winfo_width() - w) // 2
            y = self.winfo_rooty() + (self.winfo_height() - h) // 2
            win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            win.geometry(f"{w}x{h}")

        def _close():
            # Mark inactive so the refresh loop won't immediately reopen it.
            cal = dict(getattr(Job_settings, "thk_cal", {}) or {})
            cal["active"] = False
            Job_settings.thk_cal = cal
            try:
                win.destroy()
            except Exception:
                pass
            self._thk_cal_win = None

        win.protocol("WM_DELETE_WINDOW", _close)

        container = ttk.Frame(win, padding=14)
        container.pack(fill="both", expand=True)

        # Big phase/status line
        self._cal_phase_lbl = ttk.Label(container, text="", font=("Arial", 16, "bold"))
        self._cal_phase_lbl.pack(anchor="center", pady=(0, 12))

        # Calibration date/time (kept for the operator).
        self._cal_dt_var = tk.StringVar(value="")
        ttk.Label(container, textvariable=self._cal_dt_var, font=("Arial", 10)).pack(anchor="w", pady=(0, 6))

        grid = ttk.LabelFrame(container, text="Measurement", padding=10)
        grid.pack(fill="x")

        # Simplified operator table: one row per gauge, three columns —
        # true value | previous calibration | current calibration (Δ from previous).
        ttk.Label(grid, text="", width=11).grid(row=0, column=0)
        ttk.Label(grid, text="ערך אמיתי", font=("Arial", 10, "bold")).grid(row=0, column=1, padx=8)
        ttk.Label(grid, text="כיול קודם", font=("Arial", 10, "bold")).grid(row=0, column=2, padx=8)
        ttk.Label(grid, text="כיול נוכחי (Δ)", font=("Arial", 10, "bold")).grid(row=0, column=3, padx=8)

        self._cal_cells = {}
        for gi, gname in ((1, "Johnson 1"), (2, "Johnson 2")):
            ttk.Label(grid, text=gname, width=11, anchor="w").grid(row=gi, column=0, sticky="w", pady=4)
            tv = tk.StringVar(value="—")
            pv = tk.StringVar(value="—")
            cv = tk.StringVar(value="—")
            ttk.Label(grid, textvariable=tv, font=("Consolas", 11)).grid(row=gi, column=1, padx=8)
            ttk.Label(grid, textvariable=pv, font=("Consolas", 11)).grid(row=gi, column=2, padx=8)
            ttk.Label(grid, textvariable=cv, font=("Consolas", 11)).grid(row=gi, column=3, padx=8)
            self._cal_cells[gi] = (tv, pv, cv)

        self._cal_msg_lbl = ttk.Label(container, text="", foreground="#444444", wraplength=420)
        self._cal_msg_lbl.pack(anchor="w", pady=(10, 0))

        ttk.Button(container, text="Close", command=_close, padding=(14, 8)).pack(side="bottom", pady=(12, 0))

        win.lift()

    def _start_calibration(self):
        """Operator trigger: run the PC-driven calibration sequence (after warmup)."""
        if not CalGate.CanCalibrate():
            messagebox.showinfo("כיול", CalGate.StatusText())
            return
        if not CalGate.BeginCalibration():
            return
        # Open the live window; run_calibration_sequence drives M6 + measures + fits.
        if getattr(self, "_thk_cal_win", None) is None or not self._thk_cal_win.winfo_exists():
            self._build_thk_cal_window()
        threading.Thread(target=run_calibration_sequence, daemon=True).start()

    def _start_horizontal_zeroing_temp(self):
        """Footer trigger for T6 Horizontal Centering. Opens a small window that shows
        'בתהליך…' while running and fills in the measured center + suggested POS1 when done."""
        t6 = getattr(Job_settings, "t6", {}) or {}
        if t6.get("running"):
            return
        if getattr(self, "_t6_win", None) is not None:
            try:
                if self._t6_win.winfo_exists():
                    self._t6_win.lift()
                    return
            except Exception:
                self._t6_win = None

        win = tk.Toplevel(self)
        self._t6_win = win
        win.title("בדיקת מרכזיות")
        win.transient(self)
        win.resizable(False, False)
        w, h = 470, 300
        try:
            self.update_idletasks()
            x = self.winfo_rootx() + (self.winfo_width() - w) // 2
            y = self.winfo_rooty() + (self.winfo_height() - h) // 2
            win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            win.geometry(f"{w}x{h}")

        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)

        self._t6_title_lbl = ttk.Label(body, text="בתהליך…", font=("Arial", 18, "bold"))
        self._t6_title_lbl.pack(anchor="center", pady=(0, 14))
        self._t6_bot_lbl = ttk.Label(body, text="מרכז BOTTOM:  —", font=("Arial", 13))
        self._t6_bot_lbl.pack(anchor="e", pady=3)
        self._t6_top_lbl = ttk.Label(body, text="מרכז TOP:  —", font=("Arial", 13))
        self._t6_top_lbl.pack(anchor="e", pady=3)
        self._t6_off_lbl = ttk.Label(body, text="היסט (TOP−BOTTOM):  —", font=("Arial", 13))
        self._t6_off_lbl.pack(anchor="e", pady=3)
        self._t6_center_lbl = ttk.Label(body, text="יש להגדיר ל-POS1 את הערך:  —",
                                        font=("Arial", 13, "bold"), foreground="#127a1f")
        self._t6_center_lbl.pack(anchor="e", pady=(10, 0))

        ttk.Button(win, text="סגור חלון", command=self._close_t6_working_window,
                   padding=(14, 8)).pack(pady=(0, 14))

        self._t6_shown_done = False
        Job_settings.t6 = {
            "running": True,
            "done": False,
            "show_error": False,
            "error_msg": "",
        }
        threading.Thread(target=T6_horizontal_zeroing, daemon=True).start()

    def _close_t6_working_window(self):
        win = getattr(self, "_t6_win", None)
        if win is not None:
            try:
                if win.winfo_exists():
                    win.destroy()
            except Exception:
                pass
        self._t6_win = None

    def _show_t6_error_window(self, msg):
        self._close_t6_working_window()
        if getattr(self, "_t6_err_win", None) is not None:
            try:
                if self._t6_err_win.winfo_exists():
                    self._t6_err_win.lift()
                    return
            except Exception:
                self._t6_err_win = None

        win = tk.Toplevel(self)
        self._t6_err_win = win
        win.title("T6 Horizontal Zeroing — Error")
        win.transient(self)
        win.resizable(False, False)
        w, h = 520, 200
        try:
            self.update_idletasks()
            x = self.winfo_rootx() + (self.winfo_width() - w) // 2
            y = self.winfo_rooty() + (self.winfo_height() - h) // 2
            win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            win.geometry(f"{w}x{h}")

        ttk.Label(win, text=str(msg), wraplength=480, foreground="#b00000",
                  font=("Arial", 12)).pack(expand=True, padx=16, pady=16)

        def _close():
            try:
                win.destroy()
            except Exception:
                pass
            self._t6_err_win = None

        ttk.Button(win, text="Close", command=_close, padding=(14, 8)).pack(pady=(0, 16))

    def _update_t6_windows(self):
        t6 = getattr(Job_settings, "t6", {}) or {}
        if t6.get("show_error"):
            msg = t6.get("error_msg") or "T6 error"
            Job_settings.t6 = dict(t6, show_error=False, close_working=False, done=False)
            self._show_t6_error_window(msg)
            return
        # On completion, fill the results into the (still-open) window once.
        if t6.get("done") and not getattr(self, "_t6_shown_done", False):
            win = getattr(self, "_t6_win", None)
            try:
                if win is None or not win.winfo_exists():
                    return
            except Exception:
                self._t6_win = None
                return
            self._t6_shown_done = True

            def _mm(v):
                return f"{float(v):.3f} mm" if v is not None else "—"

            bot = t6.get("bot_center_mm"); top = t6.get("top_center_mm")
            off = t6.get("offset_um"); c_um = t6.get("center_um"); c_mm = t6.get("center_mm")
            self._t6_title_lbl.config(text="בדיקת מרכזיות הסתיימה")
            self._t6_bot_lbl.config(text=f"מרכז BOTTOM:  {_mm(bot)}")
            self._t6_top_lbl.config(text=f"מרכז TOP:  {_mm(top)}")
            self._t6_off_lbl.config(
                text=(f"היסט (TOP−BOTTOM):  {off:+.0f} µm" if off is not None
                      else "היסט (TOP−BOTTOM):  —"))
            if c_um is not None:
                self._t6_center_lbl.config(
                    text=f"יש להגדיר ל-POS1 את הערך:  {c_um:.0f} µm  ({c_mm:.3f} mm)")
            else:
                self._t6_center_lbl.config(
                    text="יש להגדיר ל-POS1 את הערך:  — (החישוב נכשל)")

    def _open_view_calibration(self):
        """Operator-triggered: read the last saved calibration from JSON, populate the
        shared view, and open the window. If a calibration sequence later starts on the
        PLC, the same window will receive the live updates automatically."""
        path = os.path.join("config", "config457_thk.json")
        saved = {}
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    saved = (json.load(f) or {}).get("calibration", {}) or {}
            except Exception:
                saved = {}

        a = float(saved.get("a", 1.0))
        b_mm = float(saved.get("b", 0.0))
        ts = saved.get("last_calibration_iso")

        # Rolling reference for the "thk"/"Δ" columns: the calibration made just BEFORE
        # the last one (history[-2]). Applying it to the last calibration's raw readings
        # reproduces exactly what the live window showed when that calibration ran. On the
        # first calibration there is no previous entry -> ref stays None (thk/Δ omitted).
        history = saved.get("history", []) or []
        # "current" column = the last calibration's displayed thk = the calibration
        # before it (history[-2]) applied to the last calibration's raw (top-level).
        ref_a = ref_b = None
        if len(history) >= 2:
            prev = history[-2] or {}
            if prev.get("a") is not None and prev.get("b") is not None:
                ref_a, ref_b = float(prev["a"]), float(prev["b"])

        # "previous" column = the previous calibration's displayed thk = the calibration
        # before IT (history[-3]) applied to the previous calibration's raw (history[-2]).
        g1_prev_thk = g2_prev_thk = None
        if len(history) >= 3:
            p, r = (history[-2] or {}), (history[-3] or {})
            if r.get("a") is not None and r.get("b") is not None:
                if p.get("raw_g1_mm") is not None:
                    g1_prev_thk = float(r["a"]) * float(p["raw_g1_mm"]) + float(r["b"])
                if p.get("raw_g2_mm") is not None:
                    g2_prev_thk = float(r["a"]) * float(p["raw_g2_mm"]) + float(r["b"])

        view = {
            "active": False, "phase": "view",
            "cal_iso": ts,
            "g1_known_mm": saved.get("gauge_1_known_mm"), "g2_known_mm": saved.get("gauge_2_known_mm"),
            "ref_a": ref_a, "ref_b": ref_b,
            "g1_prev_thk_mm": g1_prev_thk, "g2_prev_thk_mm": g2_prev_thk,
            "g1_raw_mm": saved.get("raw_g1_mm"), "g1_std_um": None,
            "g2_raw_mm": saved.get("raw_g2_mm"), "g2_std_um": None,
            "a": a, "b_um": b_mm * 1000.0,
            "message": f"Last calibration: {ts}" if ts else "No calibration applied yet (defaults a=1, b=0).",
        }
        Job_settings.thk_cal = view

        if getattr(self, "_thk_cal_win", None) is None or not self._thk_cal_win.winfo_exists():
            self._build_thk_cal_window()
        self._update_thk_cal_window()
        try:
            self._thk_cal_win.lift()
            self._thk_cal_win.focus_force()
        except Exception:
            pass

    def _update_thk_cal_window(self):
        cal = getattr(Job_settings, "thk_cal", None)
        if not cal:
            return

        win = getattr(self, "_thk_cal_win", None)
        win_open = win is not None and win.winfo_exists()

        # A finished calibration sequence requests the window to close with it.
        # (Only the live sequence sets close=True; view mode never does.)
        if cal.get("close"):
            if win_open:
                try:
                    win.destroy()
                except Exception:
                    pass
            self._thk_cal_win = None
            # Consume the request so it can't affect a future view/live window.
            cleared = dict(cal)
            cleared["close"] = False
            cleared["active"] = False
            Job_settings.thk_cal = cleared
            return

        # Auto-open only when a calibration sequence is actually running.
        # View mode (button) opens the window itself before calling here.
        if not win_open:
            if cal.get("active"):
                self._build_thk_cal_window()
            else:
                return

        ref_a = cal.get("ref_a")
        ref_b = cal.get("ref_b")

        # phase / step line (live status).
        phase = cal.get("phase", "idle")
        text, color = self._CAL_PHASES.get(phase, ("", "#000000"))
        # Procedure "step line": when the wrapped sequence sets a step_text, show it
        # (it names the current envelope step, e.g. "Vibration damping 0.5s").
        step_text = cal.get("step_text")
        if step_text:
            text = step_text
        self._cal_phase_lbl.config(text=text, foreground=color)

        # calibration date/time.
        iso = cal.get("cal_iso")
        self._cal_dt_var.set(f"Calibration: {iso}" if iso else "")

        # --- 3-column operator view: true value | previous cal | current cal (Δ) ---
        # "current" = the reference (ref_a,ref_b) applied to the raw measured now;
        # "previous" = the thickness the previous calibration displayed (pre-computed).
        def _fmt_val(v):
            return f"{float(v):.4f} mm" if v is not None else "—"

        def _cur_thk(raw):
            if raw is None or ref_a is None or ref_b is None:
                return None
            return float(ref_a) * float(raw) + float(ref_b)

        def _fmt_cur(cur, prev):
            if cur is None:
                return "—"
            s = f"{float(cur):.4f} mm"
            if prev is not None:
                s += f"  (Δ {(float(cur) - float(prev)) * 1000.0:+.1f}µm)"
            return s

        for gi, kraw, kprev in ((1, "g1_raw_mm", "g1_prev_thk_mm"), (2, "g2_raw_mm", "g2_prev_thk_mm")):
            true_v = cal.get(f"g{gi}_known_mm")
            prev_v = cal.get(kprev)
            cur_v = _cur_thk(cal.get(kraw))
            tv, pv, cv = self._cal_cells[gi]
            tv.set(_fmt_val(true_v))
            pv.set(_fmt_val(prev_v))
            cv.set(_fmt_cur(cur_v, prev_v))

        # Tint the message red only on a failure phase; otherwise keep it neutral.
        msg_color = "#b00000" if cal.get("phase") == "done_fail" else "#444444"
        self._cal_msg_lbl.config(text=str(cal.get("message") or ""), foreground=msg_color)

    def _refresh_from_Job_settings(self):
        try:
            self.hdr_wo_order_num.config(text=str(getattr(Job_settings, 'wo_order_number', '')))
            self.hdr_wo_part_num.config(text=str(getattr(Job_settings, 'wo_part_number', '')))
            self.hdr_wo_order_qty.config(text=str(getattr(Job_settings, 'wo_order_quantity', '')))
            self.hdr_wo_obj_counter.config(text=str(getattr(Job_settings, 'wo_object_counter', '')))

            self._update_plc_indicator()

            if hasattr(self, "hdr_cal_status"):
                self.hdr_cal_status.config(
                    text=CalGate.StatusText(),
                    foreground=("#127a1f" if CalGate.WorkAllowed else "#b00000"),
                )
                self.hdr_last_cal.config(text=CalGate.LastCalText())

            # Snapshot each shared PIL once (the producing threads may swap it mid-refresh).
            pil1 = Job_settings.pil1
            if pil1 is not None and hasattr(self, "info_img_lbl"):
                Job_settings.img1 = ImageTk.PhotoImage(pil1)
                self.info_img_lbl.configure(image=Job_settings.img1, text="")
                self.info_img_lbl.image = Job_settings.img1

            pil2 = Job_settings.pil2
            if pil2 is not None and hasattr(self, "analysis_img_lbl"):
                Job_settings.img2 = ImageTk.PhotoImage(pil2)
                self.analysis_img_lbl.configure(image=Job_settings.img2)
                self.analysis_img_lbl.image = Job_settings.img2

            pil22 = Job_settings.pil22
            if pil22 is not None and hasattr(self, "analysis_live_img_lbl"):
                Job_settings.img22 = ImageTk.PhotoImage(pil22)
                self.analysis_live_img_lbl.configure(image=Job_settings.img22)
                self.analysis_live_img_lbl.image = Job_settings.img22

            pil3 = Job_settings.pil3
            if pil3 is not None and hasattr(self, "thk_img_lbl"):
                Job_settings.img3 = ImageTk.PhotoImage(pil3)
                self.thk_img_lbl.configure(image=Job_settings.img3)
                self.thk_img_lbl.image = Job_settings.img3

            if hasattr(self, 't1_tree'):
                try:
                    self._update_t1_packjob_view()
                except Exception:
                    pass

            self._update_thk_cal_window()
            self._update_t6_windows()
        except Exception as e:
            log(f"UI refresh error: {e}", "WARN")
        finally:
            # Always reschedule, so a single failed frame can never freeze the UI.
            self.after(200, self._refresh_from_Job_settings)


# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    # Load the last saved job's spec into the vision module at startup, so its geometry
    # globals (e.g. Body, created only inside mv.GetSpec) exist and image analysis can
    # run without first re-entering a job. Best-effort: on failure, analysis simply stays
    # gated (spec_loaded=False) until a real job is loaded — no crash.
    try:
        if Job_settings.spec is not None:
            mv.GetSpec(Job_settings.spec)
            Job_settings.spec_loaded = True
            log("startup: loaded saved MV spec into vision module (analysis enabled)", "STATUS")
    except Exception as e:
        log(f"startup: could not load saved MV spec ({e}); analysis gated until a job is loaded", "WARN")

    t1 = threading.Thread(target=T1, daemon=True)
    t2 = threading.Thread(target=T2, daemon=True)
    t3 = threading.Thread(target=T3_thickness, daemon=True)
    t4 = threading.Thread(target=T4_plc_flush, daemon=True)
    t5 = threading.Thread(target=T5_startup_gate, daemon=True)

    t4.start()
    t1.start()
    t2.start()
    t3.start()
    t5.start()


    app = EL_UI()
    app.mainloop()
    Job_settings.stop_event.set()


