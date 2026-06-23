"""
Sensor working-range finder  --  POSITIONS-only motion.

Goal
----
Map the *ideal working range* of a two-distance-sensor thickness station and
produce, for later analysis:
  * a PNG: averaged thickness (foreground) over the raw thickness (background),
    with the optimal working band shaded relative to the part's nominal
    thickness;
  * an Excel workbook with every recorded sample plus a summary sheet.

The measurement itself reuses the standard thickness module
(``thickness_Module_V3_w.ThicknessModule``).  Motion is driven *only* through
HMI preset POSITIONS -- never JOG:

    1. X axis  -> POS6   (the gauge / measurement location).
    2. Y axis  -> POS1   (one fixed start point of the vertical window).
    3. Y axis  -> POS4   (a RELATIVE step; its interval is configured in the
                          HMI).  Repeated, recording a measurement at every
                          stop, until the TOP sensor leaves its range -- i.e.
                          we have swept past the usable window.

Register map (same convention as vertical_sweep_calibration.py)
---------------------------------------------------------------
    reg0   PC->PLC  livesign (1 = alive); included in every write frame.
    reg6   PC->PLC  M6 (X/horizontal) command (POS code: 6 = go to POS6).
    reg10  PC->PLC  M7 (Y/vertical)   command (POS code: 1 = POS1, 4 = POS4).
    reg24  PLC->PC  M6 status  (1..6 = in position N, 7/8 = going, 9/10 = not-ready/ready).
    reg25  PLC->PC  M6 actual position.
    reg26  PLC->PC  M7 status  (same code set as M6).
    reg27  PLC->PC  M7 actual position.

There is NO separate "servo done" register; move completion is read from the
per-axis status (reg24/reg26) and actual position (reg25/reg27).  (reg23 is the
"Bearing in Camera" 0/1 sensor -- unrelated to the servos.)

A POSITIONS move is self-terminating in the servo controller, so unlike a JOG
there is no run-away risk; we still STOP and idle on every exit path as a
belt-and-braces safety measure.  Move completion is additionally cross-checked
against the sensor stream (the TOP sensor tracks vertical motion 1:1, over UDP),
so a step still terminates correctly even if Modbus reads stall during an HMI
screen change.
"""

import argparse
import atexit
import os
import signal
import threading
import time
from datetime import datetime

import numpy as np
import pandas as pd

import thickness_Module_V3_w as thck
from pyModbusTCP.client import ModbusClient


# =============================================================================
# Logging / run instrumentation
# =============================================================================
# Every console line is timestamped (HH:MM:SS.mmm) and, once main() opens it,
# mirrored to a per-run log file next to the Excel output.  The goal is that if
# a step stalls we can see exactly where: which register reads/writes were slow
# or failed, whether the command register was cleared under us, and what the
# axis status / position were doing the whole time.
SLOW_RT_SEC   = 0.5    # a Modbus round-trip slower than this is flagged (bus/HMI contention)
HEARTBEAT_SEC = 1.0    # while waiting on a move, log a status snapshot this often
LIVESIGN_PERIOD_SEC = 0.3  # background pulse of reg0 (PLC comm watchdog); MUST be
                           # well under the PLC's comm-loss timeout, otherwise the
                           # PLC flags "no communication" mid-move and the HMI
                           # auto-switches to its entry/comm screen.

_LOG_FH = None         # set in main(); when present, the log is also written here


def log(msg, level="INFO"):
    t = time.time()
    stamp = time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"
    line = f"{stamp} [{level}] {msg}"
    print(line, flush=True)
    if _LOG_FH is not None:
        try:
            _LOG_FH.write(line + "\n")
            _LOG_FH.flush()
        except Exception:
            pass


# ----- PLC register map (from the machine register list) ----------------------
# X axis = M6 (horizontal), Y axis = M7 (vertical).
REG_LIVESIGN = 0     # PC -> PLC : 1 = alive
REG_X_CMD    = 6     # PC -> PLC : M6 command (POS code)
REG_Y_CMD    = 10    # PC -> PLC : M7 command (POS code)
REG_X_STATUS = 24    # PLC -> PC : M6 status (see AXIS_* codes below)
REG_X_POS    = 25    # PLC -> PC : M6 actual position
REG_Y_STATUS = 26    # PLC -> PC : M7 status (see AXIS_* codes below)
REG_Y_POS    = 27    # PLC -> PC : M7 actual position
# NOTE: reg23 is "Bearing in Camera" sensor (0/1), NOT a servo handshake.
# There is no separate "servo done" register -- move completion is read from
# the per-axis status (reg24/reg26) and actual position (reg25/reg27).

# ----- Axis command codes (the number after "POS" is the command code) --------
# reg6 / reg10 accept: 1..6 = Go Position N, 7 = Stop, 8 = Home,
#                      9 = JOG forward, 10 = JOG backward.
POS_X_MEASURE = 6        # POS6 : X (M6) to the measurement location
POS_Y_START   = 1        # POS1 : Y (M7) to the single start point of the window
POS_Y_STEP    = 4        # POS4 : Y (M7) relative step (interval defined in the HMI)
CMD_STOP      = 7        # either axis: stop

# ----- Axis status codes (reg24 for M6, reg26 for M7) -------------------------
# 1..6 = in position N ; 7 = going forward ; 8 = going backward ;
# 9 = not ready ; 10 = ready.
AXIS_GOING_FWD = 7
AXIS_GOING_BWD = 8
AXIS_NOT_READY = 9
AXIS_READY     = 10

# ----- Arrival / motion detection (X axis, POS6) ------------------------------
X_TIMEOUT_SEC      = 30.0    # max wait for X to reach POS6
X_STABLE_TOL       = 50      # max reg25 drift to count as "stopped" [counts]
X_STABLE_DWELL_SEC = 1.5     # reg25 must be this stable after motion was seen

# ----- Y POSITIONS move completion --------------------------------------------
Y_MOVE_TIMEOUT_SEC = 20.0    # max wait for a single Y POS move to finish
Y_POS_TOL          = 5       # reg27 drift to count as "moving" / "stopped" [counts]
Y_MIN_MOVE_SEC     = 0.25    # min time to let the servo execute a relative step
Y_SETTLE_TOL_UM    = 8.0     # TOP sensor std over the settle window to call it "stopped"
Y_SETTLE_WIN_SEC   = 0.20    # rolling window used for the settle test
Y_SETTLE_DWELL_SEC = 0.30    # TOP must stay settled this long

# ----- Reading parameters -----------------------------------------------------
SETTLE_SEC     = 0.4     # quiet time after a move before sampling a position
SAMPLE_N       = 40      # median over the newest N fresh samples per sensor
FRESH_MIN      = 8       # min fresh samples needed before we trust a read
FRESH_WAIT_SEC = 2.0     # max wait for fresh samples after a flush

# ----- Sweep parameters -------------------------------------------------------
MAX_STEPS      = 200     # safety cap on POS4 steps
LIN_TOL_UM     = 10.0    # max thickness deviation inside the linear region [um]
LIN_MIN_POINTS = 5

EXCEL_COLUMNS = [
    "timestamp", "phase", "step", "position_mm",
    "top_mm", "bottom_mm", "thickness_mm",
    "top_in_range", "bottom_in_range", "in_optimal_region",
]


# =============================================================================
# PLC  --  POSITIONS-only motion
# =============================================================================
class Plc:
    """Modbus link.  Every write shares one 20-register frame carrying the
    livesign, so the PLC sees a heartbeat with each command.  Only POSITIONS
    presets are commanded -- there is no JOG anywhere in this class."""

    def __init__(self, host, port, timeout=0.3):
        self.host, self.port = host, port
        self.c = ModbusClient(host=host, port=port, auto_open=True,
                              auto_close=False, timeout=timeout)
        # Link health counters (dumped at the end of the run).
        self.stats = {"reads": 0, "read_fail": 0, "writes": 0, "write_fail": 0,
                      "slow": 0, "max_rt_ms": 0.0}
        self.last_rt = 0.0
        # The ModbusClient socket is shared by the main flow and the background
        # livesign thread, so every transaction is serialised by this lock.
        self.lock = threading.Lock()
        self._hb_stop = threading.Event()
        self._hb_thread = None

    def connect(self):
        if not self.c.open():
            raise RuntimeError(
                f"cannot open Modbus TCP to {self.host}:{self.port}")
        if self.read() is None:
            raise RuntimeError(
                f"connected to {self.host}:{self.port} but registers 0-39 "
                f"are empty (unit-id mismatch, or not served yet)")

    def read(self):
        t = time.time()
        try:
            with self.lock:
                r = self.c.read_holding_registers(0, 40)
        except Exception as e:
            self.stats["read_fail"] += 1
            log(f"[modbus] read exception: {e}", "WARN")
            return None
        dt = time.time() - t
        self.last_rt = dt
        self.stats["reads"] += 1
        self.stats["max_rt_ms"] = max(self.stats["max_rt_ms"], dt * 1000.0)
        if dt >= SLOW_RT_SEC:
            self.stats["slow"] += 1
            log(f"[modbus] SLOW read {dt*1000:.0f} ms "
                f"(bus contention? HMI screen change?)", "WARN")
        if r is None or len(r) < 40:
            self.stats["read_fail"] += 1
            return None
        return list(r)

    def write_reg(self, addr, value):
        """Write ONE holding register (FC6).  We never write a full 0..19 block,
        because that zeroes every register we are not using (1..5, 7..9, 11..19)
        -- those belong to the HMI/PLC (screen/menu state, light & result codes,
        position setpoints) and clobbering them made the panel jump screens on
        every command."""
        t = time.time()
        try:
            with self.lock:
                ok = bool(self.c.write_single_register(int(addr), int(value)))
        except Exception as e:
            self.stats["write_fail"] += 1
            log(f"[modbus] write reg{addr} exception: {e}", "WARN")
            return False
        dt = time.time() - t
        self.stats["writes"] += 1
        self.stats["max_rt_ms"] = max(self.stats["max_rt_ms"], dt * 1000.0)
        if not ok:
            self.stats["write_fail"] += 1
            log(f"[modbus] write reg{addr}={value} FAILED", "WARN")
        elif dt >= SLOW_RT_SEC:
            self.stats["slow"] += 1
            log(f"[modbus] SLOW write {dt*1000:.0f} ms (bus contention?)", "WARN")
        return ok

    def _write(self, *, reg6=None, reg10=None, livesign=True):
        """Refresh the livesign and set ONLY the axis command registers we name.
        Registers left as None are not touched at all."""
        ok = self.write_reg(REG_LIVESIGN, 1) if livesign else True
        if reg6 is not None:
            ok = self.write_reg(REG_X_CMD, int(reg6)) and ok
        if reg10 is not None:
            ok = self.write_reg(REG_Y_CMD, int(reg10)) and ok
        return ok

    def idle(self):
        """Clear both axis command registers (keeps the livesign).  Writes only
        reg6/reg10 -- never the other registers, which belong to the HMI/PLC."""
        return self._write(reg6=0, reg10=0)

    def stop(self):
        """Safety idle on exit paths.  POSITIONS moves self-terminate, so this
        simply clears the command registers rather than issuing a JOG-stop."""
        self.idle()

    # ---- Background livesign (PLC comm watchdog) ----------------------------
    def start_heartbeat(self, period=LIVESIGN_PERIOD_SEC):
        """Pulse reg0=1 continuously in the background so the PLC never sees a
        communication gap -- even while we are blocked reading sensors or doing
        a long move.  Without this the watchdog trips mid-motion and the HMI
        jumps to its entry/comm screen."""
        self._hb_stop.clear()

        def _beat():
            while not self._hb_stop.wait(period):
                try:
                    self.write_reg(REG_LIVESIGN, 1)
                except Exception:
                    pass

        self._hb_thread = threading.Thread(target=_beat, daemon=True)
        self._hb_thread.start()
        log(f"[init] livesign heartbeat started ({period*1000:.0f} ms)")

    def stop_heartbeat(self):
        self._hb_stop.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=1.0)
            self._hb_thread = None

    # ---- X axis: go to POS6 -------------------------------------------------
    def go_pos_x(self, pos_code=POS_X_MEASURE, timeout=X_TIMEOUT_SEC):
        """Drive the X axis to POSn.  Two arrival paths so it works even if the
        controller does not publish the arrival status on reg24:
            (a) reg24 == pos_code, OR
            (b) reg25 (actual position) holds still for X_STABLE_DWELL_SEC
                after real motion was seen (reg25 moved >= 500, or the servo
                status went to working).
        Returns (ok, final_reg25)."""
        regs = self.read()
        if regs is None:
            return False, None
        init_pos, init_stat = regs[REG_X_POS], regs[REG_X_STATUS]
        log(f"[x] POS{pos_code} start: reg24(status)={init_stat}, reg25(pos)={init_pos}")
        if init_stat == pos_code:
            log(f"[x] already at POS{pos_code}")
            return True, init_pos

        self._write(reg6=pos_code)
        t0 = time.time()
        last_resend  = t0
        last_beat    = t0
        stable_since = t0
        last_stable  = init_pos
        motion_seen  = False
        last_pos, last_stat = init_pos, init_stat

        while time.time() - t0 < timeout:
            regs = self.read()
            if regs is not None:
                last_pos, last_stat = regs[REG_X_POS], regs[REG_X_STATUS]
                cmd_rb = regs[REG_X_CMD]
                # A competing master/HMI overwriting our command shows up here.
                if cmd_rb not in (pos_code, 0):
                    log(f"[x] reg6 read-back={cmd_rb} (expected {pos_code}) "
                        f"-- command may be overwritten by another master/HMI", "WARN")
                if abs(last_pos - init_pos) >= 500 or last_stat in (AXIS_GOING_FWD, AXIS_GOING_BWD):
                    motion_seen = True
                if abs(last_pos - last_stable) > X_STABLE_TOL:
                    last_stable = last_pos
                    stable_since = time.time()
                if last_stat == pos_code:
                    self.idle()
                    log(f"[x] arrived (status==POS{pos_code}) after {time.time()-t0:.1f}s; reg25={last_pos}")
                    return True, last_pos
                if motion_seen and time.time() - stable_since >= X_STABLE_DWELL_SEC:
                    self.idle()
                    log(f"[x] arrived (position stable) after {time.time()-t0:.1f}s; reg25={last_pos}")
                    return True, last_pos
            if time.time() - last_beat >= HEARTBEAT_SEC:
                last_beat = time.time()
                log(f"[x] waiting t={time.time()-t0:4.1f}s status={last_stat} "
                    f"pos={last_pos} motion_seen={motion_seen}")
            if time.time() - last_resend >= 1.0:
                self._write(reg6=pos_code)
                last_resend = time.time()
            time.sleep(0.05)

        self.idle()
        log(f"[x] TIMEOUT after {timeout:.0f}s: reg24={last_stat}, reg25={last_pos}, "
            f"motion_seen={motion_seen}", "WARN")
        return False, last_pos

    # ---- Y axis: go to a POSn preset ----------------------------------------
    def go_pos_y(self, pos_code, module, timeout=Y_MOVE_TIMEOUT_SEC, tag=""):
        """Command a Y-axis (M7) POSn preset and wait until the move completed.

        Completion is read from the M7 status (reg26) and actual position
        (reg27) -- there is no separate servo-done register:
            motion is "seen" once the status goes to going-fwd/bwd, or reg27
            moves, or the TOP sensor moves;  the move is "done" once, AFTER
            motion was seen, the status returns to in-position/ready AND reg27
            has held still AND the TOP sensor has settled.

        The TOP-sensor settle is an independent (UDP, non-Modbus) cross-check,
        so the step still terminates correctly if Modbus reads stall during an
        HMI screen change.  Returns True on confirmed completion, False on
        timeout.  Re-issues the command until motion is seen, so a clobbered /
        dropped command frame is retried rather than silently lost."""
        lbl = tag or f"POS{pos_code}"
        self._write(reg10=pos_code)
        t0 = time.time()
        last_resend = t0
        last_beat   = t0

        init = self.read()
        init_pos = init[REG_Y_POS] if init is not None else None
        log(f"[y] {lbl}: commanded reg10={pos_code}; reg26(status)="
            f"{init[REG_Y_STATUS] if init else None} reg27(pos)={init_pos}")

        motion_seen   = False
        last_stable   = init_pos
        pos_stable_at = None
        reads_none    = 0
        resends       = 0
        status = pos = None

        while time.time() - t0 < timeout:
            regs = self.read()
            if regs is None:
                reads_none += 1
            status = regs[REG_Y_STATUS] if regs is not None else None
            pos    = regs[REG_Y_POS]    if regs is not None else None

            # A competing master/HMI overwriting our command shows up here.
            if regs is not None:
                cmd_rb = regs[REG_Y_CMD]
                if cmd_rb not in (pos_code, 0):
                    log(f"[y] {lbl}: reg10 read-back={cmd_rb} (expected {pos_code}) "
                        f"-- command overwritten by another master/HMI?", "WARN")

            top_settled = _top_settled(module, Y_SETTLE_TOL_UM, Y_SETTLE_WIN_SEC)
            past_min    = time.time() - t0 >= Y_MIN_MOVE_SEC
            if status in (AXIS_GOING_FWD, AXIS_GOING_BWD):
                motion_seen = True
            if pos is not None and init_pos is not None and abs(pos - init_pos) > Y_POS_TOL:
                motion_seen = True
            if past_min and not top_settled:
                motion_seen = True

            # Track reg27 stability (reset the clock whenever it drifts).
            if pos is not None:
                if last_stable is None or abs(pos - last_stable) > Y_POS_TOL:
                    last_stable, pos_stable_at = pos, time.time()
                elif pos_stable_at is None:
                    pos_stable_at = time.time()

            if motion_seen and past_min:
                status_done = status in (pos_code, AXIS_READY) or status is None
                pos_done    = pos is None or (pos_stable_at is not None and
                                              time.time() - pos_stable_at >= Y_SETTLE_DWELL_SEC)
                top_done    = top_settled
                if status_done and pos_done and top_done:
                    self.idle()
                    log(f"[y] {lbl}: DONE in {time.time()-t0:.2f}s "
                        f"(status={status} reg27={pos} resends={resends} "
                        f"read_fail={reads_none})")
                    return True

            # Heartbeat so a stall is visible second-by-second instead of silent.
            if time.time() - last_beat >= HEARTBEAT_SEC:
                last_beat = time.time()
                log(f"[y] {lbl}: t={time.time()-t0:4.1f}s status={status} reg27={pos} "
                    f"motion={int(motion_seen)} top_settled={int(top_settled)} "
                    f"read_fail={reads_none} resends={resends}")

            # Re-issue the command until the axis acknowledges it with motion.
            if not motion_seen and time.time() - last_resend >= 0.5:
                self._write(reg10=pos_code)
                resends += 1
                last_resend = time.time()
            time.sleep(0.02)

        self.idle()
        log(f"[y] {lbl}: TIMEOUT after {timeout:.0f}s "
            f"(status={status} reg27={pos} motion_seen={motion_seen} "
            f"resends={resends} read_fail={reads_none}). "
            f"If motion never started: command not latched (clobber/contention). "
            f"If it moved but never 'done': check reg26 codes / settle tolerances.", "WARN")
        return False


# =============================================================================
# Sensors
# =============================================================================
def flush(module):
    """Drop buffered samples so the next read reflects only the current
    (stationary) position, not the trail collected while moving."""
    with module.state.lock:
        module.state.samples["TOP"].clear()
        module.state.samples["BOTTOM"].clear()


def wait_fresh(module, n=FRESH_MIN, timeout=FRESH_WAIT_SEC):
    """Block until both sensor buffers have at least n samples, or timeout."""
    t0 = time.time()
    nt = nb = 0
    while time.time() - t0 < timeout:
        with module.state.lock:
            nt = len(module.state.samples["TOP"])
            nb = len(module.state.samples["BOTTOM"])
        if nt >= n and nb >= n:
            return True, nt, nb
        time.sleep(0.02)
    return False, nt, nb


def _top_settled(module, tol_um, win_sec):
    """True when the newest TOP samples spanning ~win_sec vary by less than
    tol_um -- i.e. the vertical axis has stopped.  Used to confirm a POSITIONS
    move finished without trusting the servo-status register."""
    with module.state.lock:
        data = list(module.state.samples["TOP"])
    if len(data) < 5:
        return False
    t_last = data[-1][0]
    recent = [v for (t, v) in data if t_last - t <= win_sec]
    if len(recent) < 5:
        recent = [v for (_, v) in data[-8:]]
    return (np.std(recent) * 1000.0) <= tol_um


def read_position(module, cfg):
    """One robust reading at the current (stationary) axis position.

    Flush, settle, wait for fresh frames, then return the median of the newest
    SAMPLE_N samples per sensor.  A sensor with too few fresh frames is reported
    as None (distinct from out-of-range)."""
    flush(module)
    time.sleep(SETTLE_SEC)
    _, nt, nb = wait_fresh(module)
    with module.state.lock:
        tops = [v for _, v in list(module.state.samples["TOP"])[-SAMPLE_N:]]
        bots = [v for _, v in list(module.state.samples["BOTTOM"])[-SAMPLE_N:]]
    top = float(np.median(tops)) if nt >= FRESH_MIN else None
    bot = float(np.median(bots)) if nb >= FRESH_MIN else None

    thr = cfg.ERROR_THRESHOLD_MM
    top_in = top is not None and top < thr
    bot_in = bot is not None and bot < thr
    thickness = (cfg.SENSOR_DISTANCE_MM - (top + bot)) if (top_in and bot_in) else None
    return {
        "top_mm": top, "bottom_mm": bot, "thickness_mm": thickness,
        "top_in_range": top_in, "bottom_in_range": bot_in,
    }


# =============================================================================
# Sweep  --  POS6 (X) -> POS1 (Y start) -> repeated POS4 (Y relative step)
# =============================================================================
def run_sweep(module, plc, nominal_interval_mm):
    """Drive the documented POSITIONS sequence, recording a measurement at every
    Y stop.  Returns the list of recorded rows."""
    cfg = module.cfg
    rows = []

    def record(phase, step, r):
        rows.append({
            "timestamp":       datetime.now().isoformat(timespec="milliseconds"),
            "phase":           phase,
            "step":            step,
            "position_mm":     step * nominal_interval_mm,  # provisional; rescaled from TOP later
            "top_mm":          r["top_mm"],
            "bottom_mm":       r["bottom_mm"],
            "thickness_mm":    r["thickness_mm"],
            "top_in_range":    r["top_in_range"],
            "bottom_in_range": r["bottom_in_range"],
        })

    def _meas_str(r):
        thk = f"{r['thickness_mm']*1000:.0f}um" if r["thickness_mm"] is not None else "--"
        return (f"top={r['top_mm']} bot={r['bottom_mm']} thk={thk} "
                f"in_range(T,B)=({int(r['top_in_range'])},{int(r['bottom_in_range'])})")

    # --- Y -> POS1: single start point (top of the window) -------------------
    # On this machine POS4 only lowers the part, so POS1 brings the assembly to
    # the TOP of the usable range; the POS4 loop then descends through it.
    log(f"[seq] Y -> POS{POS_Y_START} (start point, top of range)")
    if not plc.go_pos_y(POS_Y_START, module, tag=f"POS{POS_Y_START}"):
        raise RuntimeError("Y did not confirm arrival at POS1 (start point)")
    r = read_position(module, cfg)
    record("start", 0, r)
    log(f"[seq] at start: {_meas_str(r)}")

    # --- Y -> repeated POS4: relative DOWN steps, record, stop when TOP exits -
    # Stop only AFTER the TOP sensor has been in range at least once, so we do
    # not abort prematurely if the window starts out of range.
    log(f"[seq] stepping DOWN with POS{POS_Y_STEP} (relative), recording ...")
    crossed = r["top_in_range"]
    for s in range(1, MAX_STEPS + 1):
        t_step = time.time()
        if not plc.go_pos_y(POS_Y_STEP, module, tag=f"POS{POS_Y_STEP}#{s}"):
            log(f"[seq] step {s}: POS4 did not confirm completion; stopping sweep", "WARN")
            break
        r = read_position(module, cfg)
        record("sweep", s, r)
        log(f"[seq] step {s:3d} ({time.time()-t_step:.1f}s): {_meas_str(r)}")
        if r["top_in_range"]:
            crossed = True
        if crossed and not r["top_in_range"]:
            log(f"[seq] TOP left range after {s} POS4 steps -- window swept")
            break
    else:
        log("[seq] hit MAX_STEPS before TOP left range", "WARN")

    return rows


# =============================================================================
# Honest X-axis (position in mm) from the TOP sensor -- no reliance on the
# HMI step interval, which we cannot read back.
# =============================================================================
def calibrate_position_from_sensor(rows):
    """Replace position_mm in every row with one derived from the TOP sensor.

    TOP moves 1:1 with the vertical assembly, so the median per-step delta of
    TOP across the in-range sweep rows is the true physical step in mm.  If we
    do not have enough in-range data to measure it, the provisional (nominal)
    values are left alone.  Returns the measured step [mm] or None."""
    sw = [r for r in rows if r["phase"] == "sweep"
          and r["top_in_range"] and r["bottom_in_range"]
          and r["top_mm"] is not None]
    if len(sw) < 3:
        return None
    sw.sort(key=lambda r: r["step"])
    deltas = [abs(b["top_mm"] - a["top_mm"]) / (b["step"] - a["step"])
              for a, b in zip(sw, sw[1:]) if b["step"] > a["step"]]
    if not deltas:
        return None
    step_mm = float(np.median(deltas))
    for r in rows:
        r["position_mm"] = r["step"] * step_mm
    return step_mm


# =============================================================================
# Optimal working range = longest stable-thickness in-range run
# =============================================================================
def _longest_inrange_run(rows):
    runs, cur = [], []
    for i, r in enumerate(rows):
        if r["top_in_range"] and r["bottom_in_range"] and r["thickness_mm"] is not None:
            if cur and i != cur[-1][0] + 1:
                runs.append(cur); cur = []
            cur.append((i, r))
        elif cur:
            runs.append(cur); cur = []
    if cur:
        runs.append(cur)
    return max(runs, key=len) if runs else []


def analyze_working_range(rows, nominal_mm, tol_um, lin_tol_um=LIN_TOL_UM):
    """Longest contiguous in-range run whose thickness stays within lin_tol_um
    of the segment median.  Shrink from whichever end is over tolerance until
    both ends are inside -- that run is the optimal working band."""
    run = _longest_inrange_run(rows)
    if len(run) < LIN_MIN_POINTS:
        return {"ok": False, "reason": "no usable in-range run", "indices": set()}

    thk = np.array([r["thickness_mm"] for _, r in run], float)
    pos = np.array([r["position_mm"]  for _, r in run], float)
    lo, hi = 0, len(run) - 1
    while hi - lo + 1 >= LIN_MIN_POINTS:
        med = float(np.median(thk[lo:hi + 1]))
        d_lo = abs(thk[lo] - med) * 1000.0
        d_hi = abs(thk[hi] - med) * 1000.0
        if d_lo <= lin_tol_um and d_hi <= lin_tol_um:
            break
        if d_lo >= d_hi: lo += 1
        else:            hi -= 1
    else:
        return {"ok": False, "reason": f"no flat region within {lin_tol_um:.1f}um",
                "indices": set()}

    sel = run[lo:hi + 1]
    pos_lin = pos[lo:hi + 1]
    thk_lin = thk[lo:hi + 1]
    mean_mm = float(np.mean(thk_lin))
    return {
        "ok": True,
        "indices": {i for i, _ in sel},
        "start_mm": float(pos_lin[0]),
        "end_mm":   float(pos_lin[-1]),
        "length_mm": float(abs(pos_lin[-1] - pos_lin[0])),
        "n_points": len(sel),
        "mean_mm":  mean_mm,
        "std_um":   float(np.std(thk_lin) * 1000.0),
        "lin_tol_um": lin_tol_um,
        "nominal_mm": nominal_mm,
        "tol_um": tol_um,
        "mean_err_um": (mean_mm - nominal_mm) * 1000.0 if nominal_mm is not None else None,
    }


# =============================================================================
# Output
# =============================================================================
def print_summary(s):
    log("==== Optimal working range (longest stable-thickness segment) ====")
    if not s["ok"]:
        log(f"  not found: {s['reason']}", "WARN")
        return
    log(f"  position : {s['start_mm']:.2f} -> {s['end_mm']:.2f} mm  "
        f"({s['length_mm']:.2f} mm, {s['n_points']} points)")
    log(f"  thickness: mean = {s['mean_mm']:.3f} mm   "
        f"std = {s['std_um']:.2f} um   (flat tol = {s['lin_tol_um']:.1f} um)")
    if s.get("nominal_mm") is not None and s.get("mean_err_um") is not None:
        log(f"  vs nominal {s['nominal_mm']:.3f} mm : "
            f"mean err = {s['mean_err_um']:+.1f} um   (tol = {s['tol_um']:.1f} um)")


def export_excel(rows, summary, path):
    """Write the sweep rows (with in_optimal_region flag) and a summary sheet."""
    df = pd.DataFrame(rows)
    df["in_optimal_region"] = [i in summary.get("indices", set()) for i in range(len(df))]
    df = df.reindex(columns=EXCEL_COLUMNS)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    try:
        with pd.ExcelWriter(path, engine="openpyxl") as w:
            df.to_excel(w, index=False, sheet_name="sweep")
            sdf = pd.DataFrame(
                [(k, v) for k, v in summary.items() if k != "indices"],
                columns=["key", "value"],
            )
            sdf.to_excel(w, index=False, sheet_name="summary")
        log(f"[export] {len(df)} rows -> {path}")
    except Exception as e:
        csv_path = os.path.splitext(path)[0] + ".csv"
        df.to_csv(csv_path, index=False)
        log(f"[export] openpyxl unavailable ({e}); wrote CSV -> {csv_path}", "WARN")


def _moving_average(y, k=5):
    """Centred moving average; k clamped to an odd value <= len(y)."""
    n = len(y)
    if n == 0:
        return np.array([])
    k = max(1, min(k if k % 2 == 1 else k + 1, n if n % 2 == 1 else n - 1))
    if k <= 1:
        return np.asarray(y, float)
    pad = k // 2
    yp = np.pad(np.asarray(y, float), pad, mode="edge")
    kern = np.ones(k) / k
    return np.convolve(yp, kern, mode="valid")


def export_plot(rows, summary, path):
    """PNG deliverable.

    Bottom panel  : thickness vs position -- RAW reading in the background
                    (faint dots) and the AVERAGED thickness in the foreground
                    (bold line); the optimal working band is shaded and the
                    part's nominal +/- tolerance is drawn for reference.
    Top panel     : TOP / BOTTOM absolute sensor heights vs position.
    Silent if matplotlib is unavailable."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.ticker import (FormatStrFormatter, MultipleLocator,
                                        AutoMinorLocator)
    except Exception:
        return

    sw = [r for r in rows if r["phase"] in ("start", "sweep")
          and r["top_in_range"] and r["bottom_in_range"]
          and r["thickness_mm"] is not None]
    if not sw:
        return
    sw.sort(key=lambda r: r["position_mm"])

    pos = [r["position_mm"]  for r in sw]
    thk = [r["thickness_mm"] for r in sw]
    avg = _moving_average(thk, k=5)

    fig, ax = plt.subplots(2, 1, figsize=(9, 7), sharex=True)

    # --- top: sensor heights ---
    ax[0].plot(pos, [r["top_mm"]    for r in sw], "o-", label="TOP [mm]",    markersize=3)
    ax[0].plot(pos, [r["bottom_mm"] for r in sw], "s-", label="BOTTOM [mm]", markersize=3)
    ax[0].set_ylabel("sensor absolute height [mm]")
    ax[0].grid(True, alpha=0.3); ax[0].legend(loc="best")

    # --- bottom: raw (background) + averaged (foreground) thickness ---
    ax[1].plot(pos, thk, ".", color="0.6", markersize=4, alpha=0.55,
               label="thickness (raw)", zorder=1)
    ax[1].plot(pos, avg, "-", color="C0", linewidth=2.0,
               label="thickness (averaged)", zorder=3)
    ax[1].set_xlabel("vertical position [mm]")
    ax[1].yaxis.set_major_formatter(FormatStrFormatter("%.3f"))

    # --- y-limits from the measured thickness (keep the band detail visible) --
    nom = summary.get("nominal_mm")
    tol_um = summary.get("tol_um")
    t_arr = np.asarray(thk, float)
    ylo, yhi = float(t_arr.min()), float(t_arr.max())
    margin = max(0.008, (yhi - ylo) * 0.25)
    ylo, yhi = ylo - margin, yhi + margin

    # Draw the nominal +/- tolerance only if it falls within (or near) the data
    # view.  A nominal far from the data (e.g. wrong part loaded) would stretch
    # the axis so a 1um minor grid needs thousands of ticks -- so in that case
    # we note it off-scale instead of drawing it.
    if nom is not None and (ylo - 0.05) <= nom <= (yhi + 0.05):
        tol_mm = (tol_um / 1000.0) if tol_um is not None else 0.0
        ylo, yhi = min(ylo, nom - 1.5 * tol_mm), max(yhi, nom + 1.5 * tol_mm)
        ax[1].axhline(nom, color="C1", linestyle="-", alpha=0.6,
                      label=f"nominal = {nom:.3f} mm", zorder=2)
        if tol_um is not None:
            ax[1].axhline(nom + tol_mm, color="C1", linestyle=":", alpha=0.5, zorder=2)
            ax[1].axhline(nom - tol_mm, color="C1", linestyle=":", alpha=0.5, zorder=2)
    elif nom is not None:
        err = summary.get("mean_err_um")
        note = f"nominal = {nom:.3f} mm (off-scale" + (f", Δ={err:+.0f} um)" if err is not None else ")")
        ax[1].plot([], [], " ", label=note)

    ax[1].set_ylim(ylo, yhi)

    # --- gridlines: 1um minor grid only when the span keeps the tick count
    #     sane; otherwise use an adaptive minor locator (no MAXTICKS warning) ---
    span = yhi - ylo
    ax[1].yaxis.set_major_locator(MultipleLocator(0.005 if span <= 0.06 else 0.010))
    if span <= 0.12:
        ax[1].yaxis.set_minor_locator(MultipleLocator(0.001))
        ax[1].set_ylabel("thickness [mm]   (minor gridlines = 1 um)")
    else:
        ax[1].yaxis.set_minor_locator(AutoMinorLocator())
        ax[1].set_ylabel("thickness [mm]")
    ax[1].grid(True, which="major", alpha=0.40)
    ax[1].grid(True, which="minor", alpha=0.15, linestyle=":")

    # --- optimal working band ---
    if summary["ok"]:
        for a in ax:
            a.axvspan(summary["start_mm"], summary["end_mm"],
                      color="green", alpha=0.12, label="optimal working range", zorder=0)
        ax[1].axhline(summary["mean_mm"], color="C3", linestyle="--", alpha=0.7,
                      label=f"band mean = {summary['mean_mm']:.3f} mm", zorder=2)

    ax[1].legend(loc="best", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    log(f"[export] plot -> {path}")


# =============================================================================
# Main
# =============================================================================
def main():
    ap = argparse.ArgumentParser(
        description="Sensor working-range finder (POSITIONS-only motion)")
    ap.add_argument("--host", default="192.168.3.169",
                    help="PLC Modbus TCP host (live system: 192.168.3.169)")
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--config", default=None,
                    help="thickness module config JSON (defaults to module's own)")
    ap.add_argument("--out", default=None, help="output .xlsx path")
    ap.add_argument("--nominal-mm", type=float, default=None,
                    help="part nominal thickness [mm] (default: from thk config)")
    ap.add_argument("--tol-um", type=float, default=None,
                    help="part tolerance [um] (default: from thk config)")
    ap.add_argument("--flat-tol-um", type=float, default=LIN_TOL_UM,
                    help="max thickness deviation inside the optimal band [um]")
    ap.add_argument("--nominal-interval-mm", type=float, default=0.40,
                    help="provisional POS4 step size [mm]; the X-axis is rescaled "
                         "from the TOP sensor afterwards")
    args = ap.parse_args()

    out_path = args.out or os.path.join(
        "working_range_scans",
        f"working_range_{datetime.now():%Y%m%d_%H%M%S}.xlsx",
    )

    # Open the run log file next to the output, so the full diagnostic trail is
    # saved (not only on screen).
    global _LOG_FH
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    log_path = os.path.splitext(out_path)[0] + "_log.txt"
    try:
        _LOG_FH = open(log_path, "a", encoding="utf-8")
    except Exception:
        _LOG_FH = None
    log(f"[init] run log -> {log_path}")

    module = thck.ThicknessModule(config_path=args.config)
    module.start()
    cfg = module.cfg
    nominal_mm = args.nominal_mm if args.nominal_mm is not None else cfg.NOMINAL_THICKNESS_MM_DEFAULT
    tol_um     = args.tol_um     if args.tol_um     is not None else cfg.TOL_UM_DEFAULT

    plc = Plc(args.host, args.port)

    # SAFETY: idle both axes on every exit path (normal end, exception, signal).
    def _safe_stop(*_):
        try: plc.stop()
        except Exception: pass
    atexit.register(_safe_stop)
    signal.signal(signal.SIGINT,  lambda *_: (_safe_stop(), os._exit(130)))
    signal.signal(signal.SIGTERM, lambda *_: (_safe_stop(), os._exit(143)))

    try:
        log(f"[init] connecting to PLC {args.host}:{args.port} ...")
        plc.connect()
        plc.stop()
        plc.start_heartbeat()   # keep the PLC comm watchdog fed for the whole run
        log("[init] PLC link OK; axes idled")

        # Snapshot regs 0-19 so we can SEE which ones the HMI/PLC keep non-zero
        # (screen/menu state, light & result codes, setpoints).  We only ever
        # write reg0/reg6/reg10, so everything else here is left untouched.
        snap = plc.read()
        if snap is not None:
            owned = {i: snap[i] for i in range(20)
                     if snap[i] != 0 and i not in (REG_LIVESIGN, REG_X_CMD, REG_Y_CMD)}
            log(f"[init] non-command regs 0-19 currently in use (left untouched): "
                f"{owned if owned else '{}'}")

        log("[init] waiting for sensor data stream...")
        flush(module)
        ok, nt, nb = wait_fresh(module, timeout=8.0)
        if not ok:
            raise RuntimeError(
                f"sensors not streaming (TOP={nt}, BOTTOM={nb}). Check the sensors "
                f"and that no other app is bound to the UDP ports.")
        log(f"[init] sensors streaming (TOP={nt}, BOTTOM={nb})")

        log(f"[seq] X -> POS{POS_X_MEASURE} (measurement location)")
        ok, xpos = plc.go_pos_x(POS_X_MEASURE)
        if not ok:
            raise RuntimeError(f"X did not reach POS{POS_X_MEASURE} (last reg25={xpos})")

        rows = run_sweep(module, plc, args.nominal_interval_mm)

        step_mm = calibrate_position_from_sensor(rows)
        if step_mm is not None:
            log(f"[scale] physical POS4 step measured from TOP = {step_mm:.3f} mm")

        summary = analyze_working_range(rows, nominal_mm, tol_um, lin_tol_um=args.flat_tol_um)
        print_summary(summary)
        export_excel(rows, summary, out_path)
        export_plot(rows, summary, os.path.splitext(out_path)[0] + ".png")
    finally:
        plc.stop_heartbeat()
        plc.stop()
        module.stop()
        s = plc.stats
        log(f"[link] Modbus health: reads={s['reads']} writes={s['writes']} "
            f"read_fail={s['read_fail']} write_fail={s['write_fail']} "
            f"slow(>{int(SLOW_RT_SEC*1000)}ms)={s['slow']} max_rt={s['max_rt_ms']:.0f}ms")
        if s["slow"] or s["read_fail"] or s["write_fail"]:
            log("[link] non-zero slow/failed transactions -> suspect a second "
                "Modbus master (457Main?) or HMI bus contention", "WARN")
        if _LOG_FH is not None:
            try: _LOG_FH.close()
            except Exception: pass


if __name__ == "__main__":
    main()
