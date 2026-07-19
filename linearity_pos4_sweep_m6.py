"""
POS4 horizontal (M6) linearity sweep.

Twin of linearity_pos4_sweep.py, but drives the M6 HORIZONTAL axis instead of
the M7 vertical axis. Same PLC Modbus link, same sensor pipeline, same thickness
formula and config.

Behaviour:
  - No start conditions. Starts from WHERE THE M6 AXIS CURRENTLY IS.
  - Repeatedly "presses" POS4 = the M6 horizontal relative move (reg6 = 4,
    "Go position 4"), NUM_STEPS times. POS4 is a bounded positioning move, so
    the PLC executes it and stops by itself; we just wait until the axis has
    moved and settled before reading.
  - After each stop it records one row: M6 position, top sensor, bottom sensor,
    and the computed thickness.
  - At the end it writes an Excel file with exactly those columns.

The count of steps is hard-coded as NUM_STEPS (also overridable with --steps).

Register map (from "מיפוי רגיסטרים תקשורת.docx", one shared 0..39 holding block):
    reg0   PC->PLC   livesign (1 = alive)
    reg6   PC->PLC   M6 horizontal command   (4 = POS4 = Go position 4)
    reg10  PC->PLC   M7 vertical command (left idle here)
    reg24  PLC->PC   M6 horizontal status    (4 = in position 4, 7/8 = moving, 10 = ready)
    reg25  PLC->PC   M6 horizontal actual position   <-- "position" column
"""

import argparse
import atexit
import os
import signal
import time
from datetime import datetime

import numpy as np
import pandas as pd

import thickness_Module_457 as thck
from pyModbusTCP.client import ModbusClient


# ----- Hard-coded run size ----------------------------------------------------
# How many POS4 presses / samples to take. Change this number to change the run
# length (also overridable on the command line with --steps).
NUM_STEPS = 130


# ----- PLC register map -------------------------------------------------------
REG_LIVESIGN     = 0    # PC -> PLC : 1 = alive
REG_VERT_CMD     = 10   # PC -> PLC : M7 command (left idle here)
REG_HORIZ_CMD    = 6    # PC -> PLC : M6 command
REG_HORIZ_STATUS = 24   # PLC -> PC : M6 status
# M6 actual position: a single register in microns. Homing is handled in
# hardware, so after homing the value starts at 0 and only increases, and is
# read directly as-is.
REG_HORIZ_POS = 25      # PLC -> PC : M6 actual position ("position" column)

HCMD_IDLE   = 0         # M6: no command
HCMD_POS4   = 4         # M6: Go position 4  (the relative "POS4" move)
HCMD_STOP   = 7         # M6: STOP
HST_IN_POS4 = 4         # M6 status: in position 4
HST_MOVING  = (7, 8)    # M6 status: going forward / backward


# ----- POS4 move-completion detection -----------------------------------------
# POS4 self-stops, but we must not read the sensors until the axis is actually
# stopped. We give it a moment to start, watch reg25 for motion, and then wait
# until reg25 has been stable for a short dwell. Tune these to the real POS4
# step size if needed (reg25 units, typically microns).
POS4_TIMEOUT_SEC     = 15.0   # max wait for one POS4 move to finish
POS4_MIN_MOVE_SEC    = 0.10   # don't accept "arrived" before this (let it start)
POS4_MOTION_MIN      = 3      # reg25 change that counts as real motion
POS4_STABLE_TOL      = 3      # max reg25 drift to count as "stopped"
POS4_STABLE_DWELL_SEC = 0.35  # reg25 must stay put this long to be "arrived"

# ----- Sensor read ------------------------------------------------------------
SETTLE_SEC     = 0.3    # extra settle after the axis stops, before reading
SAMPLE_N       = 40     # median over the newest N fresh samples per sensor
FRESH_MIN      = 8      # min fresh samples needed before we trust a read
FRESH_WAIT_SEC = 2.0    # max wait for fresh samples after a flush

EXCEL_COLUMNS = ["step", "position", "top_mm", "bottom_mm", "thickness_mm"]


# =============================================================================
# PLC : Modbus link + M6 POS4 move
# =============================================================================
class Plc:
    """Modbus link and M6 (horizontal) motion. Every write carries the livesign
    so the PLC sees a heartbeat with each command (same frame style as the old
    sweep script)."""

    def __init__(self, host, port, timeout=0.3):
        self.host, self.port = host, port
        self.c = ModbusClient(host=host, port=port, auto_open=True,
                              auto_close=False, timeout=timeout)

    def connect(self):
        if not self.c.open():
            raise RuntimeError(
                f"cannot open Modbus TCP to {self.host}:{self.port} "
                f"(live system uses 192.168.3.169)")
        if self.read() is None:
            raise RuntimeError(
                f"connected to {self.host}:{self.port} but registers 0-39 "
                f"are empty (unit-id mismatch, or not served yet)")

    def read(self):
        r = self.c.read_holding_registers(0, 40)
        return list(r) if r and len(r) >= 40 else None

    def _write(self, *, reg6=0, reg10=0):
        regs = [0] * 20
        regs[REG_LIVESIGN]  = 1
        regs[REG_HORIZ_CMD] = int(reg6)
        regs[REG_VERT_CMD]  = int(reg10)
        return bool(self.c.write_multiple_registers(0, regs))

    def idle(self):       return self._write()
    def stop_horiz(self):
        self._write(reg6=HCMD_STOP)
        time.sleep(0.01)
        self._write()

    def read_horiz_pos(self):
        regs = self.read()
        return None if regs is None else regs[REG_HORIZ_POS]

    def pos4_step(self):
        """Press POS4 once: command reg6=4, wait until the axis has moved and
        settled, then idle. Returns (final_reg25, ok). ok=False means the move
        did not clearly complete within POS4_TIMEOUT_SEC."""
        regs = self.read()
        if regs is None:
            return None, False
        init_pos = regs[REG_HORIZ_POS]

        # Clean rising edge, then command the relative move.
        self.idle()
        time.sleep(0.02)
        self._write(reg6=HCMD_POS4)

        t0 = time.time()
        stable_since = t0
        last_stable  = init_pos
        motion_seen  = False
        last_pos     = init_pos

        while time.time() - t0 < POS4_TIMEOUT_SEC:
            regs = self.read()
            if regs is not None:
                last_pos  = regs[REG_HORIZ_POS]
                last_stat = regs[REG_HORIZ_STATUS]
                if abs(last_pos - init_pos) >= POS4_MOTION_MIN or last_stat in HST_MOVING:
                    motion_seen = True
                if abs(last_pos - last_stable) > POS4_STABLE_TOL:
                    last_stable = last_pos
                    stable_since = time.time()

                elapsed  = time.time() - t0
                settled  = (time.time() - stable_since) >= POS4_STABLE_DWELL_SEC
                if elapsed >= POS4_MIN_MOVE_SEC and settled:
                    if motion_seen or last_stat == HST_IN_POS4:
                        self.idle()
                        return last_pos, True
            time.sleep(0.03)

        # Timed out: stop cleanly and report the move as not-clearly-complete.
        self.idle()
        return last_pos, False


# =============================================================================
# Sensors : start only the readers (no thickness processor -> buffers not drained)
# =============================================================================
def build_readers(state):
    readers = []
    for name, info in thck.Cfg.SENSORS.items():
        readers.append(thck.SensorReader(state, name, info["port"]))
    return readers


def flush(state):
    """Drop buffered samples so the next read reflects only the current
    (stationary) position, not the trail collected while moving."""
    with state.lock:
        state.samples["TOP"].clear()
        state.samples["BOTTOM"].clear()


def wait_fresh(state, n=FRESH_MIN, timeout=FRESH_WAIT_SEC):
    """Block until both sensor buffers have at least n samples, or timeout."""
    t0 = time.time()
    nt = nb = 0
    while time.time() - t0 < timeout:
        with state.lock:
            nt = len(state.samples["TOP"])
            nb = len(state.samples["BOTTOM"])
        if nt >= n and nb >= n:
            return True, nt, nb
        time.sleep(0.02)
    return False, nt, nb


def read_sensors(state):
    """One robust reading at the current (stationary) axis position: flush,
    settle, wait for fresh frames, return the median of the newest SAMPLE_N
    samples per sensor and the computed thickness."""
    flush(state)
    time.sleep(SETTLE_SEC)
    _, nt, nb = wait_fresh(state)
    with state.lock:
        tops = [v for _, v in list(state.samples["TOP"])[-SAMPLE_N:]]
        bots = [v for _, v in list(state.samples["BOTTOM"])[-SAMPLE_N:]]

    top = float(np.median(tops)) if nt >= FRESH_MIN else None
    bot = float(np.median(bots)) if nb >= FRESH_MIN else None

    thr = thck.Cfg.ERROR_THRESHOLD_MM
    top_in = top is not None and top < thr
    bot_in = bot is not None and bot < thr
    thickness = (thck.Cfg.SENSOR_DISTANCE_MM - (top + bot)) if (top_in and bot_in) else None
    return {"top_mm": top, "bottom_mm": bot, "thickness_mm": thickness}


# =============================================================================
# Export
# =============================================================================
def export_excel(rows, path):
    df = pd.DataFrame(rows).reindex(columns=EXCEL_COLUMNS)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    try:
        with pd.ExcelWriter(path, engine="openpyxl") as w:
            df.to_excel(w, index=False, sheet_name="linearity")
        print(f"[export] {len(df)} rows -> {path}")
    except Exception as e:
        csv_path = os.path.splitext(path)[0] + ".csv"
        df.to_csv(csv_path, index=False)
        print(f"[export] openpyxl unavailable ({e}); wrote CSV -> {csv_path}")


# =============================================================================
# Main
# =============================================================================
def main():
    ap = argparse.ArgumentParser(description="POS4 horizontal (M6) linearity sweep")
    ap.add_argument("--host",  default="192.168.3.169",
                    help="PLC Modbus TCP host (live system: 192.168.3.169)")
    ap.add_argument("--port",  type=int, default=502)
    ap.add_argument("--steps", type=int, default=NUM_STEPS,
                    help=f"number of POS4 presses / samples (default {NUM_STEPS})")
    ap.add_argument("--out",   default=None, help="output .xlsx path")
    args = ap.parse_args()

    out_path = args.out or os.path.join(
        "linearity_sweeps",
        f"linearity_pos4_m6_{datetime.now():%Y%m%d_%H%M%S}.xlsx",
    )

    # Start ONLY the sensor readers (the thickness processor would drain the
    # sample buffers we read from here).
    state = thck.ThicknessRuntimeState()
    readers = build_readers(state)
    for r in readers:
        r.start()

    plc = Plc(args.host, args.port)

    # SAFETY: M6 must be told to STOP on every exit path - normal end, exception,
    # Ctrl+C, kill.
    def _emergency_stop(*_):
        try: plc.stop_horiz()
        except Exception: pass
    atexit.register(_emergency_stop)
    signal.signal(signal.SIGINT,  lambda *_: (_emergency_stop(), os._exit(130)))
    signal.signal(signal.SIGTERM, lambda *_: (_emergency_stop(), os._exit(143)))

    rows = []
    try:
        print(f"[init] connecting to PLC {args.host}:{args.port} ...")
        plc.connect()
        plc.stop_horiz()
        print("[init] PLC link OK; M6 idled")

        print("[init] waiting for sensor data stream...")
        flush(state)
        ok, nt, nb = wait_fresh(state, timeout=8.0)
        if not ok:
            raise RuntimeError(
                f"sensors not streaming (TOP={nt}, BOTTOM={nb}). Check the sensors "
                f"and that no other app is bound to the UDP ports.")
        print(f"[init] sensors streaming (TOP={nt}, BOTTOM={nb})")

        # --- Baseline row at the current position, before any move ---
        pos0 = plc.read_horiz_pos()
        r0 = read_sensors(state)
        rows.append({"step": 0, "position": pos0, "top_mm": r0["top_mm"],
                     "bottom_mm": r0["bottom_mm"], "thickness_mm": r0["thickness_mm"]})
        print(f"[step 0] pos={pos0}  top={r0['top_mm']}  bot={r0['bottom_mm']}  "
              f"thk={r0['thickness_mm']}")

        # --- POS4 sweep: press POS4 `steps` times, sampling after each ---
        for s in range(1, args.steps + 1):
            pos, moved_ok = plc.pos4_step()
            if not moved_ok:
                print(f"[step {s}] WARNING: POS4 move did not clearly complete "
                      f"(pos={pos}) - recording anyway")
            r = read_sensors(state)
            pos_read = plc.read_horiz_pos()  # position at the moment we sample
            if pos_read is not None:
                pos = pos_read
            rows.append({"step": s, "position": pos, "top_mm": r["top_mm"],
                         "bottom_mm": r["bottom_mm"], "thickness_mm": r["thickness_mm"]})
            print(f"[step {s}/{args.steps}] pos={pos}  top={r['top_mm']}  "
                  f"bot={r['bottom_mm']}  thk={r['thickness_mm']}")

        export_excel(rows, out_path)
    finally:
        plc.stop_horiz()
        state.stop_event.set()
        for r in readers:
            r.join(timeout=0.5)


if __name__ == "__main__":
    main()
