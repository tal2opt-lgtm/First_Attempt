import argparse
import os
import queue
import signal
import threading
import time
from datetime import datetime

import numpy as np
import pandas as pd

import thickness_Module_V3_w as thck
from pyModbusTCP.client import ModbusClient


# =============================================================================
# Horizontal Beam Alignment Test  --  v5 (longest-in-range edge detection)
# =============================================================================
# What changed vs v4:
#   v4 found edges by "first transition with 30 ms / 80% confirmation". That
#   is too easily fooled by two real-world artifacts:
#     - a brief glitch BEFORE the gauge crossing (a 90 ms BOTTOM blip in the
#       last run), which the algorithm treated as the gauge enter+exit
#     - edge flickering AT the gauge boundary (TOP flickered IN/OUT a few
#       times in the first 100 ms after entering), which prevented the
#       confirmation window from latching the real boundary
#   Result: tiny "gauge crossings" of 20-113 counts instead of the actual
#   ~9000 counts, and the consistency checks correctly rejected the run.
#
#   v5 uses a robust heuristic: for each sensor, find the LONGEST CONTIGUOUS
#   in_range=True region. That IS the gauge crossing. Short glitches (in or
#   out) are simply not the longest run. ENTER = first sample of that run,
#   EXIT = first out-of-range sample after the run.
#
#   The math, the consistency checks, and the encoder-only philosophy are
#   unchanged from v4. We just identify the boundaries correctly.
#
#   Also: the PNG is now always exported, even when the analysis is rejected,
#   so the operator can always see what the sensors saw.


# =============================================================================
# >>> EDIT THESE VALUES <<<
# =============================================================================
GAUGE_WIDTH_MM = 9.000          # Physical width of the calibration gauge.

# Encoder calibration: reg25 is in micrometres divided by 1, i.e. reg25 = 1000
# means the assembly has moved 1.000 mm from zero. The user verified this
# directly against the machine, so we trust it and use it as the absolute
# scale for dx. The gauge crossing now serves as a SANITY CHECK on the
# encoder rather than as the scale reference.
ENCODER_COUNTS_PER_MM = 1000.0

# Slower M6 motion (0.1 mm/s instead of 1 mm/s) gives 10x more sensor
# samples per encoder count, so the gauge-edge ramp is resolved by many
# samples instead of one or two. This is the dominant noise reduction
# lever for a single-shot test.
#
# Note: GAUGE_WIDTH_MM is kept for operator reference but is not used in
# the dx calculation - ENTER-only mode does not measure the gauge width.


# =============================================================================
# Other parameters (mostly unchanged from v4)
# =============================================================================
MIN_RUN_SEC = 0.20      # Below this, an "in_range" run is not even considered
                        # for the longest-run search. Filters pure noise.

MAX_REASONABLE_DX_MM = 1.0

HCMD_GO_POS = 6
HST_IN_POS = 6

REG_LIVESIGN     = 0
REG_HORIZ_CMD    = 6
REG_VERT_CMD     = 10
REG_HORIZ_STATUS = 24
REG_HORIZ_POS    = 25
VCMD_STOP = 7

READ_PERIOD_SEC      = 0.005
MOTION_TIMEOUT_SEC   = 300.0   # 5 minutes. Long enough to handle 0.1 mm/s
                                # motion across the full M6 stroke (~25 mm
                                # at 0.1 mm/s = 250 s) with safety margin.
                                # The motion is normally terminated long
                                # before this by the arrival/stable-stop
                                # detection below; this is just the safety
                                # cap if the PLC never signals arrival.
STABLE_DWELL_SEC     = 1.0
STABLE_TOL_COUNTS    = 50

EXCEL_COLUMNS = [
    "timestamp_pc", "t_sec", "reg24_status", "reg25_pos",
    "sensor", "value_mm", "in_range",
]


# =============================================================================
# PLC
# =============================================================================
class Plc:
    def __init__(self, host, port, timeout=0.3):
        self.host = host
        self.port = port
        self.c = ModbusClient(host=host, port=port, auto_open=True,
                              auto_close=False, timeout=timeout)

    def connect(self):
        if not self.c.open():
            raise RuntimeError(f"cannot open Modbus TCP to {self.host}:{self.port}")
        if self.read() is None:
            raise RuntimeError(
                f"connected to {self.host}:{self.port}, but registers 0-39 are empty"
            )

    def read(self):
        r = self.c.read_holding_registers(0, 40)
        return list(r) if r and len(r) >= 40 else None

    def _write(self, *, reg6=0, reg10=0):
        regs = [0] * 20
        regs[REG_LIVESIGN]  = 1
        regs[REG_HORIZ_CMD] = int(reg6)
        regs[REG_VERT_CMD]  = int(reg10)
        return bool(self.c.write_multiple_registers(0, regs))

    def idle(self):
        return self._write()

    def stop_vert(self):
        self._write(reg10=VCMD_STOP)
        time.sleep(0.01)
        self._write()

    def go_pos_command(self):
        return self._write(reg6=HCMD_GO_POS)


# =============================================================================
# Sensor helpers
# =============================================================================
def flush(module):
    with module.state.lock:
        module.state.samples["TOP"].clear()
        module.state.samples["BOTTOM"].clear()


def snapshot_new_samples(module, last_ts):
    fresh_samples = []
    new_last_ts = dict(last_ts)

    with module.state.lock:
        data = {
            "TOP": list(module.state.samples["TOP"]),
            "BOTTOM": list(module.state.samples["BOTTOM"]),
        }

    for sensor, samples in data.items():
        for ts, value in samples:
            if ts <= last_ts.get(sensor, -float("inf")):
                continue
            fresh_samples.append((sensor, float(ts), float(value)))
            if ts > new_last_ts.get(sensor, -float("inf")):
                new_last_ts[sensor] = ts

    return fresh_samples, new_last_ts


# =============================================================================
# Edge detection: longest contiguous in_range run
# =============================================================================
def _longest_in_range_run(samples):
    """Return (enter_sample, exit_sample) describing the longest contiguous
    in_range=True region across `samples`. ENTER is the first in-range sample
    of that region; EXIT is the first out-of-range sample immediately after.

    Returns (None, None) if no run is at least MIN_RUN_SEC long. This is
    robust to two real failure modes:
      - brief in-range blips before/after the actual crossing (BOTTOM had a
        90 ms one before its real entry)
      - rapid IN/OUT flickering at the gauge boundary (TOP flickered for the
        first ~100 ms after entering and again at exit)
    Whichever sensor flickers, the longest stable run still dominates.
    """
    samples = sorted(samples, key=lambda r: r["t_sec"])

    best_enter = best_exit = None
    best_len_sec = -1.0

    cur_enter = None
    cur_last_in = None

    for r in samples:
        if r["in_range"]:
            if cur_enter is None:
                cur_enter = r
            cur_last_in = r
        else:
            if cur_enter is not None:
                length = cur_last_in["t_sec"] - cur_enter["t_sec"]
                if length > best_len_sec:
                    best_len_sec = length
                    best_enter = cur_enter
                    best_exit = r          # first out sample after the run
                cur_enter = None
                cur_last_in = None

    # Run that extends to the end of the data (no closing out-of-range sample).
    if cur_enter is not None:
        length = cur_last_in["t_sec"] - cur_enter["t_sec"]
        if length > best_len_sec:
            best_len_sec = length
            best_enter = cur_enter
            best_exit = None

    if best_len_sec < MIN_RUN_SEC:
        return None, None

    return best_enter, best_exit


# =============================================================================
# Analysis: ENTER-only, encoder-scaled
# =============================================================================
def analyze_edges(rows):
    by_sensor = {"TOP": [], "BOTTOM": []}
    for r in rows:
        by_sensor[r["sensor"]].append(r)

    # The longest in-range run still localizes the gauge crossing robustly;
    # we just don't use its end (exit). Only the start (enter) of each
    # sensor's longest run matters - and that's what the dx is built from.
    bottom_enter, _ = _longest_in_range_run(by_sensor["BOTTOM"])
    top_enter,    _ = _longest_in_range_run(by_sensor["TOP"])

    results = {}

    def _pack(top_e, bot_e):
        if top_e is None or bot_e is None:
            return {"ok": False,
                    "reason": f"missing enter edge: TOP={top_e is not None}, "
                              f"BOTTOM={bot_e is not None}"}
        return {
            "ok": True,
            "top_t_sec": top_e["t_sec"],
            "bottom_t_sec": bot_e["t_sec"],
            "dt_sec_top_minus_bottom": top_e["t_sec"] - bot_e["t_sec"],
            "top_reg25": top_e["reg25_pos"],
            "bottom_reg25": bot_e["reg25_pos"],
            "dreg25_top_minus_bottom": (
                None
                if top_e["reg25_pos"] is None or bot_e["reg25_pos"] is None
                else top_e["reg25_pos"] - bot_e["reg25_pos"]
            ),
        }

    results["enter"] = _pack(top_enter, bottom_enter)

    if top_enter is None or bottom_enter is None:
        results["official"] = {
            "ok": False,
            "reason": (
                f"no stable in-range run >= {MIN_RUN_SEC*1000:.0f} ms on at "
                f"least one sensor. Check the PNG to see what the sensors saw."
            ),
        }
        return results

    A1 = bottom_enter["reg25_pos"]
    B1 = top_enter   ["reg25_pos"]

    if A1 is None or B1 is None:
        results["official"] = {
            "ok": False,
            "reason": f"missing reg25 at enter edge (A1={A1}, B1={B1})",
        }
        return results

    enter_offset_counts = B1 - A1
    dx_mm = enter_offset_counts / ENCODER_COUNTS_PER_MM

    if abs(dx_mm) > MAX_REASONABLE_DX_MM:
        results["official"] = {
            "ok": False,
            "reason": f"dx = {dx_mm*1000:+.1f} um exceeds sanity limit",
            "enter_offset_counts": enter_offset_counts,
            "dx_mm": dx_mm,
        }
        return results

    if dx_mm > 0:
        direction = "Move TOP sensor WITH the M6 motion direction"
    elif dx_mm < 0:
        direction = "Move TOP sensor OPPOSITE the M6 motion direction"
    else:
        direction = "No X correction needed"

    results["official"] = {
        "ok": True,
        "source": "ENTER edge only, encoder-scaled (single deterministic offset)",
        "encoder_counts_per_mm": ENCODER_COUNTS_PER_MM,
        "enter_offset_counts": enter_offset_counts,
        "dx_mm": dx_mm,
        "abs_correction_mm": abs(dx_mm),
        "recommendation": direction,
    }
    return results


# =============================================================================
# Motion recording (unchanged from v4)
# =============================================================================
def record_motion_to_pos(module, plc, cfg, timeout=MOTION_TIMEOUT_SEC, ui_queue=None):
    rows = []
    regs = plc.read()
    if regs is None:
        raise RuntimeError("cannot read PLC registers before motion")

    init_pos = regs[REG_HORIZ_POS]
    init_status = regs[REG_HORIZ_STATUS]

    msg = f"[m6] start: reg24={init_status}, reg25={init_pos}"
    print(msg)
    if ui_queue:
        ui_queue.put(("status", msg))

    flush(module)
    last_ts = {"TOP": -float("inf"), "BOTTOM": -float("inf")}

    t0 = time.time()
    last_resend = t0
    stable_since = t0
    last_stable_pos = init_pos
    motion_seen = False

    msg = f"[m6] command -> POS{HCMD_GO_POS}; recording..."
    print(msg)
    if ui_queue:
        ui_queue.put(("status", msg))

    for _ in range(5):
        plc.go_pos_command()
        time.sleep(0.03)

    while time.time() - t0 < timeout:
        now = time.time()

        regs = plc.read()
        status = regs[REG_HORIZ_STATUS] if regs is not None else None
        pos = regs[REG_HORIZ_POS] if regs is not None else None

        if pos is not None:
            if abs(pos - init_pos) >= 500 or status in (7, 8):
                motion_seen = True
            if abs(pos - last_stable_pos) > STABLE_TOL_COUNTS:
                last_stable_pos = pos
                stable_since = now

        fresh, last_ts = snapshot_new_samples(module, last_ts)

        new_rows = []
        for sensor, _ts_sensor, value in fresh:
            row = {
                "timestamp_pc": datetime.now().isoformat(timespec="milliseconds"),
                "t_sec": now - t0,
                "reg24_status": status,
                "reg25_pos": pos,
                "sensor": sensor,
                "value_mm": value,
                "in_range": bool(value < cfg.ERROR_THRESHOLD_MM),
            }
            rows.append(row)
            new_rows.append(row)

        if ui_queue and new_rows:
            ui_queue.put(("samples", new_rows))

        if now - last_resend >= 1.0:
            plc.go_pos_command()
            last_resend = now

        if motion_seen and status == HST_IN_POS:
            msg = f"[m6] arrived at POS{HST_IN_POS}; reg25={pos}"
            print(msg)
            if ui_queue:
                ui_queue.put(("status", msg))
            break

        if motion_seen and time.time() - stable_since >= STABLE_DWELL_SEC:
            msg = f"[m6] position stable; assumed stopped; reg25={pos}"
            print(msg)
            if ui_queue:
                ui_queue.put(("status", msg))
            break

        time.sleep(READ_PERIOD_SEC)
    else:
        msg = "[m6] WARNING: timeout before arrival/stable-stop"
        print(msg)
        if ui_queue:
            ui_queue.put(("status", msg))

    plc.idle()
    return rows


# =============================================================================
# Export
# =============================================================================
def export_excel(rows, results, path):
    df = pd.DataFrame(rows).reindex(columns=EXCEL_COLUMNS)
    summary_rows = []
    for edge_name, result in results.items():
        for key, value in result.items():
            summary_rows.append({"edge": edge_name, "key": key, "value": value})
    sdf = pd.DataFrame(summary_rows)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="samples")
        sdf.to_excel(writer, index=False, sheet_name="summary")
    print(f"[export] {len(df)} rows -> {path}")


def export_edge_plot(rows, results, path):
    """Always render the gauge crossing (or the whole run if analysis failed)
    so the operator can see the raw situation. X axis is encoder counts."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return
    if not rows:
        return

    df = pd.DataFrame(rows)
    if df.empty or df["reg25_pos"].isna().all():
        return

    enter = results.get("enter", {})
    o = results.get("official", {})

    # Zoom around the ENTER pair if we have it, else show full encoder range.
    if (enter.get("ok") and enter.get("bottom_reg25") is not None
            and enter.get("top_reg25") is not None):
        rs = [enter["bottom_reg25"], enter["top_reg25"]]
        r_lo, r_hi = min(rs), max(rs)
        pad = max(500, int(2.0 * (r_hi - r_lo + 1)))
        r_min, r_max = r_lo - pad, r_hi + pad
    else:
        r_min = int(df["reg25_pos"].min())
        r_max = int(df["reg25_pos"].max())

    cut = df[(df["reg25_pos"] >= r_min) & (df["reg25_pos"] <= r_max)].copy()
    if cut.empty:
        cut = df.copy()

    fig, ax = plt.subplots(figsize=(10, 5))
    for sensor in ("TOP", "BOTTOM"):
        s = cut[cut["sensor"] == sensor]
        if not s.empty:
            ax.plot(s["reg25_pos"], s["value_mm"], ".",
                    label=sensor, markersize=4)

    if enter.get("ok"):
        if enter.get("bottom_reg25") is not None:
            ax.axvline(enter["bottom_reg25"], linestyle=":", alpha=0.85,
                       label="BOTTOM ENTER")
        if enter.get("top_reg25") is not None:
            ax.axvline(enter["top_reg25"], linestyle="--", alpha=0.85,
                       label="TOP ENTER")

    if o.get("ok"):
        title = (
            f"v5 ENTER-only  |  enter offset {o['enter_offset_counts']:+} counts  "
            f"->  dx = {o['dx_mm']*1000:+.2f} um  "
            f"->  move TOP {o['abs_correction_mm']*1000:.2f} um"
        )
    else:
        title = f"v5 INVALID: {o.get('reason', 'unknown')}"

    ax.set_title(title, fontsize=10)
    ax.set_xlabel("encoder position reg25 [counts]")
    ax.set_ylabel("sensor value [mm]")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    print(f"[export] encoder-space plot -> {path}")


# =============================================================================
# Console summary
# =============================================================================
def print_summary(results):
    print("\n" + "=" * 76)
    print("Horizontal beam alignment result (v5, longest in-range run)")
    print("=" * 76)
    print(f"ENCODER_COUNTS_PER_MM           = {ENCODER_COUNTS_PER_MM:.1f}")
    print(f"MIN_RUN_SEC                     = {MIN_RUN_SEC} s")
    print(f"GAUGE_WIDTH_MM (informational)  = {GAUGE_WIDTH_MM:.3f} mm")

    r = results.get("enter", {})
    print(f"\nENTER edge:")
    if not r.get("ok"):
        print(f"  not found: {r.get('reason', 'unknown')}")
    else:
        print(f"  TOP reg25:       {r['top_reg25']}")
        print(f"  BOTTOM reg25:    {r['bottom_reg25']}")
        print(f"  TOP - BOTTOM:    {r['dreg25_top_minus_bottom']:+} counts")

    o = results.get("official", {})
    print("\nOFFICIAL CORRECTION (ENTER-only):")
    if o.get("ok"):
        print(f"  enter offset (B-A):  {o['enter_offset_counts']:+} counts "
              f"({o['enter_offset_counts']/ENCODER_COUNTS_PER_MM*1000:+.1f} um)")
        print(f"  dx:                  {o['dx_mm']*1000:+.2f} um")
        print(f"  move amount:         {o['abs_correction_mm']*1000:.2f} um")
        print(f"  recommendation:      {o['recommendation']}")
    else:
        print(f"  INVALID: {o.get('reason', 'unknown')}")
        for k in ("enter_offset_counts", "dx_mm"):
            if k in o:
                v = o[k]
                if k.endswith("_mm"):
                    print(f"    {k}: {v*1000:+.2f} um")
                else:
                    print(f"    {k}: {v}")
    print("=" * 76)


# =============================================================================
# Non-UI runner
# =============================================================================
def run_test(host, port, config, out_path=None, timeout=MOTION_TIMEOUT_SEC, ui_queue=None):
    out_path = out_path or os.path.join(
        "beam_alignment_scans",
        f"horizontal_beam_alignment_v5_{datetime.now():%Y%m%d_%H%M%S}.xlsx",
    )

    module = thck.ThicknessModule(config_path=config)
    module.start()
    plc = Plc(host, port)

    def _safe_stop():
        try:
            plc.stop_vert()
            plc.idle()
        except Exception:
            pass

    try:
        if ui_queue:
            ui_queue.put(("status", f"Connecting to PLC {host}:{port}..."))
        print(f"[init] connecting to PLC {host}:{port} ...")

        plc.connect()
        plc.stop_vert()
        if ui_queue:
            ui_queue.put(("status", "PLC link OK; M7 stopped"))

        print("[init] waiting briefly for sensor stream...")
        flush(module)
        time.sleep(1.0)

        with module.state.lock:
            nt = len(module.state.samples["TOP"])
            nb = len(module.state.samples["BOTTOM"])
        msg = f"[init] sensor buffer check: TOP={nt}, BOTTOM={nb}"
        print(msg)
        if ui_queue:
            ui_queue.put(("status", msg))

        rows = record_motion_to_pos(module, plc, module.cfg, timeout=timeout, ui_queue=ui_queue)
        if not rows:
            raise RuntimeError("no samples recorded during motion")

        results = analyze_edges(rows)
        print_summary(results)

        export_excel(rows, results, out_path)
        plot_path = os.path.splitext(out_path)[0] + "_encoder_crossing.png"
        export_edge_plot(rows, results, plot_path)

        if ui_queue:
            ui_queue.put(("result", results, out_path, plot_path))

        return results, out_path, plot_path

    finally:
        _safe_stop()
        module.stop()


# =============================================================================
# Simple Tkinter UI
# =============================================================================
class BeamAlignmentUI:
    def __init__(self, host, port, config, timeout):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk

        self.host = host
        self.port = port
        self.config = config
        self.timeout = timeout

        self.q = queue.Queue()
        self.rows_for_live = []
        self.running = False
        self._alive = True

        self.root = tk.Tk()
        self.root.title("Horizontal Beam Alignment Test  (v5)")
        self.root.geometry("980x720")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self.root.after(100, self._process_queue)

    def _on_close(self):
        self._alive = False
        try:
            self.root.destroy()
        except Exception:
            pass

    def _build_ui(self):
        tk = self.tk
        ttk = self.ttk

        top_frame = ttk.Frame(self.root, padding=10)
        top_frame.pack(fill="x")

        self.start_btn = ttk.Button(top_frame, text="Start Test", command=self.start_test)
        self.start_btn.pack(side="left", padx=5)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(top_frame, textvariable=self.status_var).pack(side="left", padx=20)

        self.gauge_var = tk.StringVar(value=f"Gauge W: {GAUGE_WIDTH_MM:.3f} mm")
        ttk.Label(top_frame, textvariable=self.gauge_var).pack(side="right", padx=5)

        result_frame = ttk.LabelFrame(self.root, text="Official result", padding=10)
        result_frame.pack(fill="x", padx=10, pady=8)

        self.dx_var = tk.StringVar(value="dx: ---")
        self.move_var = tk.StringVar(value="Move TOP: ---")
        self.rec_var = tk.StringVar(value="Recommendation: ---")

        ttk.Label(result_frame, textvariable=self.dx_var, font=("Arial", 16, "bold")).pack(anchor="w")
        ttk.Label(result_frame, textvariable=self.move_var, font=("Arial", 14)).pack(anchor="w")
        ttk.Label(result_frame, textvariable=self.rec_var, font=("Arial", 12)).pack(anchor="w")

        debug_frame = ttk.LabelFrame(self.root, text="Consistency checks", padding=10)
        debug_frame.pack(fill="x", padx=10, pady=8)

        self.edge_var = tk.StringVar(value="Details will appear here")
        ttk.Label(debug_frame, textvariable=self.edge_var, justify="left").pack(anchor="w")

        plot_frame = ttk.LabelFrame(self.root, text="Live samples preview", padding=10)
        plot_frame.pack(fill="both", expand=True, padx=10, pady=8)

        try:
            import matplotlib
            matplotlib.use("TkAgg")
            import matplotlib.pyplot as plt
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            self.plt = plt
            self.fig, self.ax = plt.subplots(figsize=(8, 4))
            self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
            self.canvas.get_tk_widget().pack(fill="both", expand=True)
            self.ax.set_xlabel("time [s]")
            self.ax.set_ylabel("value [mm]")
            self.ax.grid(True, alpha=0.3)
            self.canvas.draw()
        except Exception as e:
            self.plt = None
            self.fig = None
            self.ax = None
            self.canvas = None
            ttk.Label(plot_frame, text=f"Live plot unavailable: {e}").pack(anchor="w")

        bottom_frame = ttk.Frame(self.root, padding=10)
        bottom_frame.pack(fill="x")
        self.files_var = tk.StringVar(value="")
        ttk.Label(bottom_frame, textvariable=self.files_var).pack(anchor="w")

    def start_test(self):
        if self.running:
            return
        self.running = True
        self.rows_for_live.clear()
        self.start_btn.config(state="disabled")
        self.status_var.set("Starting test...")
        self.dx_var.set("dx: ---")
        self.move_var.set("Move TOP: ---")
        self.rec_var.set("Recommendation: ---")
        self.edge_var.set("Running...")
        self.files_var.set("")
        t = threading.Thread(target=self._worker, daemon=True)
        t.start()

    def _worker(self):
        try:
            run_test(host=self.host, port=self.port, config=self.config,
                     timeout=self.timeout, ui_queue=self.q)
        except Exception as e:
            self.q.put(("error", str(e)))

    def _process_queue(self):
        if not self._alive:
            return
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "status":
                    self.status_var.set(item[1])
                elif kind == "samples":
                    self.rows_for_live.extend(item[1])
                    if len(self.rows_for_live) > 1000:
                        self.rows_for_live = self.rows_for_live[-1000:]
                    self._update_live_plot()
                elif kind == "result":
                    _, results, xlsx_path, plot_path = item
                    self._show_result(results, xlsx_path, plot_path)
                    self.running = False
                    self.start_btn.config(state="normal")
                elif kind == "error":
                    self.status_var.set("ERROR")
                    self.edge_var.set(item[1])
                    self.running = False
                    self.start_btn.config(state="normal")
        except queue.Empty:
            pass
        if self._alive:
            try:
                self.root.after(100, self._process_queue)
            except Exception:
                pass

    def _update_live_plot(self):
        if self.ax is None or not self.rows_for_live:
            return
        self.ax.clear()
        df = pd.DataFrame(self.rows_for_live)
        for sensor in ("TOP", "BOTTOM"):
            s = df[df["sensor"] == sensor]
            if not s.empty:
                self.ax.plot(s["t_sec"], s["value_mm"], ".",
                             label=sensor, markersize=3)
        self.ax.set_xlabel("time [s]")
        self.ax.set_ylabel("value [mm]")
        self.ax.set_title("Live TOP/BOTTOM samples")
        self.ax.grid(True, alpha=0.3)
        self.ax.legend(loc="best")
        self.canvas.draw_idle()

    def _show_result(self, results, xlsx_path, plot_path):
        o = results.get("official", {})

        if o.get("ok"):
            dx_um = o["dx_mm"] * 1000.0
            move_um = o["abs_correction_mm"] * 1000.0
            self.dx_var.set(f"dx = {dx_um:+.2f} um")
            self.move_var.set(f"Move TOP: {move_um:.2f} um")
            self.rec_var.set(f"Recommendation: {o['recommendation']}")
            self.status_var.set("Done")
        else:
            self.dx_var.set("dx: INVALID")
            self.move_var.set("Move TOP: ---")
            self.rec_var.set(f"Reason: {o.get('reason', 'unknown')}")
            self.status_var.set("Done - invalid result")

        lines = []
        r = results.get("enter", {})
        if r.get("ok"):
            lines.append(
                f"ENTER: reg25 TOP-BOTTOM = "
                f"{r['dreg25_top_minus_bottom']:+} counts"
            )
        else:
            lines.append(f"ENTER: not found")

        if "enter_offset_counts" in o:
            lines.append(
                f"enter offset: {o.get('enter_offset_counts','?')} cnt   "
                f"({o.get('enter_offset_counts',0)/ENCODER_COUNTS_PER_MM*1000:+.1f} um)"
            )

        self.edge_var.set("\n".join(lines))
        self.files_var.set(f"Excel: {xlsx_path}\nPlot: {plot_path}")

        self._show_final_crossing_plot(results)

    def _show_final_crossing_plot(self, results):
        if self.ax is None or not self.rows_for_live:
            return
        df = pd.DataFrame(self.rows_for_live)
        if df.empty or df["reg25_pos"].isna().all():
            return

        enter = results.get("enter", {})
        if (enter.get("ok") and enter.get("bottom_reg25") is not None
                and enter.get("top_reg25") is not None):
            rs = [enter["bottom_reg25"], enter["top_reg25"]]
            r_lo, r_hi = min(rs), max(rs)
            pad = max(500, int(2.0 * (r_hi - r_lo + 1)))
            r_min, r_max = r_lo - pad, r_hi + pad
        else:
            r_min = int(df["reg25_pos"].min())
            r_max = int(df["reg25_pos"].max())

        cut = df[(df["reg25_pos"] >= r_min) & (df["reg25_pos"] <= r_max)].copy()
        if cut.empty:
            cut = df.copy()

        self.ax.clear()
        for sensor in ("TOP", "BOTTOM"):
            s = cut[cut["sensor"] == sensor]
            if not s.empty:
                self.ax.plot(s["reg25_pos"], s["value_mm"], ".",
                             label=sensor, markersize=4)

        if enter.get("ok"):
            if enter.get("bottom_reg25") is not None:
                self.ax.axvline(enter["bottom_reg25"], linestyle=":", alpha=0.85,
                                label="BOTTOM ENTER")
            if enter.get("top_reg25") is not None:
                self.ax.axvline(enter["top_reg25"], linestyle="--", alpha=0.85,
                                label="TOP ENTER")

        o = results.get("official", {})
        if o.get("ok"):
            title = f"v5 ENTER-only  |  dx = {o['dx_mm']*1000:+.2f} um"
        else:
            title = f"v5 INVALID: {o.get('reason', 'unknown')}"

        self.ax.set_title(title, fontsize=9)
        self.ax.set_xlabel("encoder position reg25 [counts]")
        self.ax.set_ylabel("sensor value [mm]")
        self.ax.grid(True, alpha=0.3)
        self.ax.legend(loc="best", fontsize=8)
        self.canvas.draw_idle()

    def run(self):
        self.root.mainloop()


def main():
    ap = argparse.ArgumentParser(description="Horizontal beam alignment test (v5)")
    ap.add_argument("--host", default="192.168.3.169")
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--config", default=None)
    ap.add_argument("--timeout", type=float, default=MOTION_TIMEOUT_SEC)
    ap.add_argument("--no-ui", action="store_true", help="run without UI")
    args = ap.parse_args()

    def _emergency_exit(*_):
        os._exit(130)
    signal.signal(signal.SIGINT, _emergency_exit)
    signal.signal(signal.SIGTERM, _emergency_exit)

    if args.no_ui:
        run_test(args.host, args.port, args.config, timeout=args.timeout)
    else:
        app = BeamAlignmentUI(args.host, args.port, args.config, args.timeout)
        app.run()


if __name__ == "__main__":
    main()
