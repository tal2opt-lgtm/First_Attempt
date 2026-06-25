
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


# ----- PLC register map -------------------------------------------------------
REG_LIVESIGN     = 0    # PC -> PLC : 1 = alive
REG_HORIZ_CMD    = 6    # PC -> PLC : M6 command
REG_VERT_CMD     = 10   # PC -> PLC : M7 command
REG_HORIZ_STATUS = 24   # PLC -> PC : M6 status
REG_HORIZ_POS    = 25   # PLC -> PC : M6 actual position

HCMD_GO_POS6 = 6        # M6: go to preset POS6 (gauge location)
HST_IN_POS6  = 6        # M6 status: in POS6 (may not be wired - we also use position stability)
VCMD_STOP    = 7        # M7: STOP
VCMD_JOG_DOWN = 10      # M7: JOG that lowers the assembly on this machine
VCMD_JOG_UP   = 9       # M7: JOG that raises the assembly on this machine


# ----- Sweep parameters -------------------------------------------------------
# JOG_DURATION_SEC sets how long reg10 is held; the controller's jog speed
# after stabilization is 5 mm/s, but ramp-up eats a variable fraction of each
# short jog, so we CANNOT compute the physical distance from time x speed.
# JOG_MM below is a nominal placeholder. At end of run we replace each row's
# position_mm with a value measured from the TOP sensor (which tracks vertical
# motion 1:1), so the X-axis is the truth - not a calibration guess.
JOG_DURATION_SEC = 0.35
JOG_MM           = 0.40   # nominal only - overwritten by sensor measurement

SETTLE_SEC       = 0.4   # wait for the axis to fully stop after STOP, before reading
SAMPLE_N         = 40    # median over the newest N fresh samples per sensor
FRESH_MIN        = 8     # min fresh samples needed before we trust a read
FRESH_WAIT_SEC   = 2.0   # max wait for fresh samples after a flush

M6_TIMEOUT_SEC      = 30.0   # max wait for M6 to reach POS6
M6_STABLE_TOL       = 50     # max reg25 drift to count as "stopped"
M6_STABLE_DWELL_SEC = 1.5    # must be stable this long after motion was seen

MAX_STEPS         = 200      # safety cap on jogs in either phase
LIN_TOL_UM        = 10.0     # max thickness deviation from segment median [um]
LIN_MIN_POINTS    = 5

EXCEL_COLUMNS = [
    "timestamp", "phase", "step", "position_mm",
    "top_mm", "bottom_mm", "thickness_mm",
    "top_in_range", "bottom_in_range", "in_linear_region",
]


# =============================================================================
# Plc
# =============================================================================
class Plc:
    """Modbus link and M6/M7 motion. All writes share one 20-register frame
    that includes the livesign, so the PLC sees a heartbeat with every command."""

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

    def idle(self):     return self._write()
    def stop_vert(self):
        self._write(reg10=VCMD_STOP)
        time.sleep(0.01)
        self._write()

    def jog(self, direction, duration=JOG_DURATION_SEC):
        """Start the jog, hold for `duration`, then STOP. STOP is also sent
        on any exception so the axis never keeps driving on an error path."""
        try:
            self._write(reg10=direction)
            time.sleep(duration)
        finally:
            self.stop_vert()

    def go_pos6(self, timeout=M6_TIMEOUT_SEC):
        """Drive M6 to POS6. Two arrival paths so this works even if the
        controller doesn't publish HST_IN_POS6 on reg24:
            (a) reg24 == HST_IN_POS6, OR
            (b) reg25 (actual position) stays put for M6_STABLE_DWELL_SEC
                AFTER we've seen real motion (reg25 moved >= 500, or status
                went to going-fwd/bwd).
        Returns (ok, final_reg25)."""
        regs = self.read()
        if regs is None:
            return False, None
        init_pos, init_stat = regs[REG_HORIZ_POS], regs[REG_HORIZ_STATUS]
        print(f"[m6] start: reg24={init_stat}, reg25={init_pos}")
        if init_stat == HST_IN_POS6:
            print("[m6] already at POS6")
            return True, init_pos

        self._write(reg6=HCMD_GO_POS6)
        t0 = time.time()
        last_resend  = t0
        stable_since = t0
        last_stable  = init_pos
        motion_seen  = False
        last_pos, last_stat = init_pos, init_stat

        while time.time() - t0 < timeout:
            regs = self.read()
            if regs is not None:
                last_pos, last_stat = regs[REG_HORIZ_POS], regs[REG_HORIZ_STATUS]
                if abs(last_pos - init_pos) >= 500 or last_stat in (7, 8):
                    motion_seen = True
                if abs(last_pos - last_stable) > M6_STABLE_TOL:
                    last_stable = last_pos
                    stable_since = time.time()
                if last_stat == HST_IN_POS6:
                    self.idle()
                    print(f"[m6] arrived (status): reg25={last_pos}")
                    return True, last_pos
                if motion_seen and time.time() - stable_since >= M6_STABLE_DWELL_SEC:
                    self.idle()
                    print(f"[m6] arrived (position stable): reg25={last_pos}")
                    return True, last_pos
            if time.time() - last_resend >= 1.0:
                self._write(reg6=HCMD_GO_POS6)
                last_resend = time.time()
            time.sleep(0.05)

        self.idle()
        print(f"[m6] TIMEOUT: reg24={last_stat}, reg25={last_pos}, "
              f"motion_seen={motion_seen}")
        return False, last_pos


# =============================================================================
# Sensors
# =============================================================================
def flush(module):
    """Drop buffered samples - the next read reflects only the current
    (stationary) position, not the trail collected while moving."""
    with module.state.lock:
        module.state.samples["TOP"].clear()
        module.state.samples["BOTTOM"].clear()


def wait_fresh(module, n=FRESH_MIN, timeout=FRESH_WAIT_SEC):
    """Block until both sensor buffers have at least n samples, or timeout."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        with module.state.lock:
            nt = len(module.state.samples["TOP"])
            nb = len(module.state.samples["BOTTOM"])
        if nt >= n and nb >= n:
            return True, nt, nb
        time.sleep(0.02)
    return False, nt, nb


def read_position(module, cfg):
    """Take one robust reading at the current (stationary) axis position.

    Flush, settle, wait for fresh frames, return the median of the newest
    SAMPLE_N samples per sensor. A sensor with too few fresh frames is
    reported as None (distinct from out-of-range)."""
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
# Sweep
# =============================================================================
def run_sweep(module, plc):
    """Clear down through the band (recording), then sweep up through it
    (recording), ending when both sensors are out of range at the top.
    Returns the list of recorded rows."""
    cfg = module.cfg
    rows = []

    def record(phase, step, r):
        rows.append({
            "timestamp":    datetime.now().isoformat(timespec="milliseconds"),
            "phase":        phase,
            "step":         step,
            "position_mm":  step * JOG_MM,
            "top_mm":       r["top_mm"],
            "bottom_mm":    r["bottom_mm"],
            "thickness_mm": r["thickness_mm"],
            "top_in_range":    r["top_in_range"],
            "bottom_in_range": r["bottom_in_range"],
        })

    def both_out(r): return not r["top_in_range"] and not r["bottom_in_range"]

    # --- Clear: descend until both sensors out of range -----------------------
    r = read_position(module, cfg)
    record("clear", 0, r)
    if not both_out(r):
        print("[clear] jogging down to clear of the gauge...")
        for s in range(1, MAX_STEPS + 1):
            plc.jog(VCMD_JOG_DOWN)
            r = read_position(module, cfg)
            record("clear", -s, r)
            if both_out(r):
                print(f"[clear] clear after {s} down-jogs")
                break
        else:
            raise RuntimeError("clear: never both-out within MAX_STEPS")
    else:
        print("[clear] already clear")

    # --- Sweep: ascend recording until we've crossed the band -----------------
    print("[sweep] sweeping up, recording...")
    crossed = False  # turned True the first time both sensors are in range
    for s in range(1, MAX_STEPS + 1):
        plc.jog(VCMD_JOG_UP)
        r = read_position(module, cfg)
        record("sweep", s, r)
        if r["top_in_range"] and r["bottom_in_range"]:
            crossed = True
        if crossed and both_out(r):
            print(f"[sweep] band crossed after {s} up-jogs")
            break
    else:
        print("[sweep] WARNING: hit MAX_STEPS before both-out at top")

    return rows


# =============================================================================
# Honest X-axis from the TOP sensor (no reliance on jog timing)
# =============================================================================
def calibrate_position_from_sensor(rows):
    """Replace position_mm in every row with one derived from the TOP sensor.

    TOP changes 1:1 with the assembly's vertical motion, so the median per-step
    delta across in-range sweep rows is the true physical step in mm. The
    resulting X-axis is correct regardless of jog ramp behaviour or how well
    JOG_MM was guessed. If we don't have enough in-range data to measure,
    we leave the nominal values alone."""
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
# Linearity analysis
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


def analyze_linearity(rows, tol_um=LIN_TOL_UM):
    """Longest contiguous in-range run where the thickness reading stays
    within tol_um of the segment median. Shrink from whichever end is over
    the tolerance until both ends are inside."""
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
        if d_lo <= tol_um and d_hi <= tol_um:
            break
        if d_lo >= d_hi: lo += 1
        else:            hi -= 1
    else:
        return {"ok": False, "reason": f"no flat region within {tol_um:.1f}um",
                "indices": set()}

    sel = run[lo:hi + 1]
    pos_lin = pos[lo:hi + 1]
    thk_lin = thk[lo:hi + 1]
    return {
        "ok": True,
        "indices": {i for i, _ in sel},
        "start_mm": float(pos_lin[0]),
        "end_mm":   float(pos_lin[-1]),
        "length_mm": float(pos_lin[-1] - pos_lin[0]),
        "n_points": len(sel),
        "mean_mm":  float(np.mean(thk_lin)),
        "std_um":   float(np.std(thk_lin) * 1000.0),
        "tol_um":   tol_um,
    }


# =============================================================================
# Output: console, Excel, plot
# =============================================================================
def print_summary(s):
    print("\n" + "=" * 60)
    print("Linearity region (longest stable-thickness segment)")
    print("=" * 60)
    if not s["ok"]:
        print(f"  not found: {s['reason']}")
        return
    print(f"  position: {s['start_mm']:.2f} -> {s['end_mm']:.2f} mm  "
          f"({s['length_mm']:.2f} mm, {s['n_points']} points)")
    print(f"  thickness: mean = {s['mean_mm']:.3f} mm   "
          f"std = {s['std_um']:.2f} um   (tol = {s['tol_um']:.1f} um)")
    print("=" * 60)


def export_excel(rows, summary, path):
    """Write the sweep rows (with in_linear_region flag) and a summary sheet."""
    df = pd.DataFrame(rows)
    df["in_linear_region"] = [i in summary.get("indices", set()) for i in range(len(df))]
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
        print(f"[export] {len(df)} rows -> {path}")
    except Exception as e:
        csv_path = os.path.splitext(path)[0] + ".csv"
        df.to_csv(csv_path, index=False)
        print(f"[export] openpyxl unavailable ({e}); wrote CSV -> {csv_path}")


def export_plot(rows, summary, path):
    """TOP/BOTTOM/thickness vs position. Bottom plot has two-tier Y grid:
    labels every 5 um, dotted minor lines every 1 um so micron-scale
    variation is visible. Silent if matplotlib is unavailable."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.ticker import FormatStrFormatter, MultipleLocator
    except Exception:
        return
    sw = [r for r in rows if r["phase"] == "sweep"
          and r["top_in_range"] and r["bottom_in_range"]
          and r["thickness_mm"] is not None]
    if not sw:
        return

    pos = [r["position_mm"]  for r in sw]
    fig, ax = plt.subplots(2, 1, figsize=(9, 7), sharex=True)
    ax[0].plot(pos, [r["top_mm"]    for r in sw], "o-", label="TOP [mm]",    markersize=3)
    ax[0].plot(pos, [r["bottom_mm"] for r in sw], "s-", label="BOTTOM [mm]", markersize=3)
    ax[0].set_ylabel("sensor absolute height [mm]")
    ax[0].grid(True, alpha=0.3); ax[0].legend(loc="best")

    ax[1].plot(pos, [r["thickness_mm"] for r in sw], "o-", color="C2",
               markersize=3, label="thickness [mm]")
    ax[1].set_xlabel("vertical position [mm]")
    ax[1].set_ylabel("thickness [mm]   (minor gridlines = 1 um)")
    ax[1].yaxis.set_major_formatter(FormatStrFormatter("%.3f"))
    ax[1].yaxis.set_major_locator(MultipleLocator(0.005))
    ax[1].yaxis.set_minor_locator(MultipleLocator(0.001))
    ax[1].grid(True, which="major", alpha=0.40)
    ax[1].grid(True, which="minor", alpha=0.15, linestyle=":")

    if summary["ok"]:
        for a in ax:
            a.axvspan(summary["start_mm"], summary["end_mm"],
                      color="green", alpha=0.12, label="linear region")
        ax[1].axhline(summary["mean_mm"], color="C3", linestyle="--", alpha=0.6,
                      label=f"mean = {summary['mean_mm']:.3f} mm")
        ax[1].legend(loc="best")

    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    print(f"[export] plot -> {path}")


# =============================================================================
# Main
# =============================================================================
def main():
    ap = argparse.ArgumentParser(description="Vertical sweep calibration")
    ap.add_argument("--host",   default="192.168.3.169",
                    help="PLC Modbus TCP host (live system: 192.168.3.169)")
    ap.add_argument("--port",   type=int, default=502)
    ap.add_argument("--config", default=None,
                    help="thickness module config JSON (defaults to module's own)")
    ap.add_argument("--out",    default=None, help="output .xlsx path")
    ap.add_argument("--tol-um", type=float, default=LIN_TOL_UM,
                    help="max thickness deviation in the linear region [um]")
    args = ap.parse_args()

    out_path = args.out or os.path.join(
        "calibration_sweeps",
        f"vertical_sweep_{datetime.now():%Y%m%d_%H%M%S}.xlsx",
    )

    module = thck.ThicknessModule(config_path=args.config)
    module.start()
    plc = Plc(args.host, args.port)

    # SAFETY: M7 must be told to STOP on every exit path - normal end, exception,
    # Ctrl+C, kill. The JOG command is continuous-while-set; leaving it dangling
    # would keep the motor driving toward its mechanical limit.
    def _emergency_stop(*_):
        try: plc.stop_vert()
        except Exception: pass
    atexit.register(_emergency_stop)
    signal.signal(signal.SIGINT,  lambda *_: (_emergency_stop(), os._exit(130)))
    signal.signal(signal.SIGTERM, lambda *_: (_emergency_stop(), os._exit(143)))

    try:
        print(f"[init] connecting to PLC {args.host}:{args.port} ...")
        plc.connect()
        plc.stop_vert()
        print("[init] PLC link OK; M7 idled")

        print("[init] waiting for sensor data stream...")
        flush(module)
        ok, nt, nb = wait_fresh(module, timeout=8.0)
        if not ok:
            raise RuntimeError(
                f"sensors not streaming (TOP={nt}, BOTTOM={nb}). Check the sensors "
                f"and that no other app (e.g. 457_test.py) is bound to the UDP ports.")
        print(f"[init] sensors streaming (TOP={nt}, BOTTOM={nb})")

        print("[m6] commanding M6 -> POS6 ...")
        ok, hpos = plc.go_pos6()
        if not ok:
            raise RuntimeError(f"M6 did not reach POS6 (last reg25={hpos})")
        print(f"[m6] at POS6 (reg25={hpos})")

        r = read_position(module, module.cfg)
        if not (r["top_in_range"] or r["bottom_in_range"]):
            print("\n" + "=" * 60)
            print("NO OBJECT DETECTED at POS6 - calibration NOT started.")
            print(f"  TOP    raw = {r['top_mm']}")
            print(f"  BOTTOM raw = {r['bottom_mm']}")
            print("  (check the gauge is present and the start vertical position)")
            print("=" * 60)
            return
        print(f"[m6] gauge detected (top={r['top_in_range']}, bot={r['bottom_in_range']})"
              f" - starting sweep")

        rows = run_sweep(module, plc)
        step_mm = calibrate_position_from_sensor(rows)
        if step_mm is not None:
            print(f"[scale] physical step measured from TOP = {step_mm:.3f} mm")
        summary = analyze_linearity(rows, tol_um=args.tol_um)
        print_summary(summary)
        export_excel(rows, summary, out_path)
        export_plot(rows, summary, os.path.splitext(out_path)[0] + ".png")
    finally:
        plc.stop_vert()
        module.stop()


if __name__ == "__main__":
    main()
