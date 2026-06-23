import argparse
import atexit
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
# Horizontal Beam Alignment Test  --  v4 (encoder-only, time-independent)
# =============================================================================
# What changed vs v3:
#   v3 still used time as the medium to measure distances ("how long the gauge
#   was in range"). If a sensor briefly glitched out and back in (as BOTTOM
#   did at 75 ms in the last run), its EXIT timestamp landed in the wrong
#   place and the result was rejected. The time medium is fragile in two
#   ways: it carries clock-sync error between TOP/BOTTOM, and it lets short
#   in/out flickers corrupt the result.
#
#   v4 drops time entirely. At each detected sensor edge we read the encoder
#   (reg25) directly. The encoder counts physical motion regardless of speed,
#   stalls, or jitter, so every "distance" is a difference of two encoder
#   readings - no clocks involved.
#
#       A1 = reg25 at BOTTOM enter      A2 = reg25 at BOTTOM exit
#       B1 = reg25 at TOP    enter      B2 = reg25 at TOP    exit
#
#       dx_via_BOTTOM = GAUGE_WIDTH_MM * (B1 - A1) / (A2 - A1)
#       dx_via_TOP    = GAUGE_WIDTH_MM * (B2 - A2) / (B2 - B1)
#
#   The encoder's mm-per-count never appears in the formula: counts cancel
#   between numerator and denominator, leaving only mm * (counts/counts).
#   The only assumption is that the encoder is LINEAR over the short stroke
#   of one gauge crossing - which the operator declared trustworthy.
#
#   Three independent consistency checks, all in encoder counts:
#     (i)  gauge width measured by BOTTOM ~= gauge width measured by TOP
#          (rejects mid-crossing glitches like the 75-ms one we saw)
#     (ii) X-offset at enter ~= X-offset at exit
#          (B1-A1 should equal B2-A2 for parallel-edged gauges)
#     (iii) dx_via_BOTTOM ~= dx_via_TOP
#          (derived from i and ii, but reported as the final acceptance test)
#
# Operator prerequisites (unchanged from v3 except for tolerance names):
#   1. GAUGE_WIDTH_MM = actual measured width of the gauge along M6 motion.
#   2. M6 stroke long enough that BOTH sensors fully cross the gauge.


# =============================================================================
# >>> EDIT THESE VALUES <<<
# =============================================================================
GAUGE_WIDTH_MM = 9.000          # Physical width of the calibration gauge along
                                # the M6 motion direction. Measured by a
                                # trusted reference (micrometer / CMM).

GAUGE_CONSISTENCY_TOL = 0.05    # Max allowed disagreement between the gauge
                                # width measured by BOTTOM (A2-A1) and by TOP
                                # (B2-B1), as a fraction of their mean.
                                # 0.05 = 5%. A bigger gap means one sensor
                                # glitched mid-crossing - reject the run.

OFFSET_CONSISTENCY_TOL_COUNTS = 30  # Max allowed disagreement between the
                                    # enter-edge offset (B1-A1) and the
                                    # exit-edge offset (B2-A2), in encoder
                                    # counts. For parallel gauge edges these
                                    # must match. ~30 counts is loose enough
                                    # to absorb edge-detection quantization.


# =============================================================================
# Edge detection parameters (unchanged from v3)
# =============================================================================
EDGE_CONFIRM_SEC = 0.030
EDGE_CONFIRM_RATIO = 0.80
MIN_EDGE_TIME_SEC = 0.050

MAX_REASONABLE_DX_MM = 1.0
EDGE_PLOT_WINDOW_BEFORE_SEC = 0.250
EDGE_PLOT_WINDOW_AFTER_SEC = 0.550

HCMD_GO_POS = 6
HST_IN_POS = 6


# =============================================================================
# PLC register map
# =============================================================================
REG_LIVESIGN     = 0
REG_HORIZ_CMD    = 6
REG_VERT_CMD     = 10
REG_HORIZ_STATUS = 24
REG_HORIZ_POS    = 25

VCMD_STOP = 7


# =============================================================================
# Acquisition parameters
# =============================================================================
READ_PERIOD_SEC      = 0.005
MOTION_TIMEOUT_SEC   = 60.0
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
        self.c = ModbusClient(
            host=host,
            port=port,
            auto_open=True,
            auto_close=False,
            timeout=timeout,
        )

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
# Edge detection: FIRST transition + retrospective confirmation (unchanged)
# =============================================================================
def _confirmed_transition(samples, *, transition):
    if not samples:
        return None

    if transition == "enter":
        old_state, new_state = False, True
    elif transition == "exit":
        old_state, new_state = True, False
    else:
        raise ValueError("transition must be 'enter' or 'exit'")

    samples = sorted(samples, key=lambda r: r["t_sec"])
    prev_state = samples[0]["in_range"]

    for i in range(1, len(samples)):
        candidate = samples[i]

        if candidate["t_sec"] < MIN_EDGE_TIME_SEC:
            prev_state = candidate["in_range"]
            continue

        if prev_state == old_state and candidate["in_range"] == new_state:
            t0 = candidate["t_sec"]
            t1 = t0 + EDGE_CONFIRM_SEC
            window = [r for r in samples[i:] if t0 <= r["t_sec"] <= t1]

            if len(window) < 3:
                prev_state = candidate["in_range"]
                continue

            ratio = sum(r["in_range"] == new_state for r in window) / len(window)
            if ratio >= EDGE_CONFIRM_RATIO:
                return candidate

        prev_state = candidate["in_range"]

    return None


# =============================================================================
# Analysis: encoder-only, time-independent
# =============================================================================
def analyze_edges(rows):
    by_sensor = {"TOP": [], "BOTTOM": []}
    for r in rows:
        by_sensor[r["sensor"]].append(r)
    for sensor in by_sensor:
        by_sensor[sensor].sort(key=lambda r: r["t_sec"])

    edges = {
        (s, t): _confirmed_transition(by_sensor[s], transition=t)
        for s in ("TOP", "BOTTOM")
        for t in ("enter", "exit")
    }

    results = {}

    # Per-transition records: report both time and encoder so the operator can
    # eyeball what happened. The math below uses ENCODER only.
    for transition in ("enter", "exit"):
        top_edge = edges[("TOP", transition)]
        bottom_edge = edges[("BOTTOM", transition)]

        if top_edge is None or bottom_edge is None:
            results[transition] = {
                "ok": False,
                "reason": (
                    f"missing {transition} edge: "
                    f"TOP={top_edge is not None}, "
                    f"BOTTOM={bottom_edge is not None}"
                ),
            }
            continue

        results[transition] = {
            "ok": True,
            "top_t_sec": top_edge["t_sec"],
            "bottom_t_sec": bottom_edge["t_sec"],
            "dt_sec_top_minus_bottom": top_edge["t_sec"] - bottom_edge["t_sec"],
            "top_reg25": top_edge["reg25_pos"],
            "bottom_reg25": bottom_edge["reg25_pos"],
            "dreg25_top_minus_bottom": (
                None
                if top_edge["reg25_pos"] is None or bottom_edge["reg25_pos"] is None
                else top_edge["reg25_pos"] - bottom_edge["reg25_pos"]
            ),
        }

    missing = [
        f"{s}_{t}"
        for s in ("BOTTOM", "TOP")
        for t in ("enter", "exit")
        if edges[(s, t)] is None
    ]
    if missing:
        results["official"] = {
            "ok": False,
            "reason": (
                f"need all 4 edges. Missing: {', '.join(missing)}. "
                f"Extend the M6 stroke so both sensors fully cross the gauge."
            ),
        }
        return results

    A1 = edges[("BOTTOM", "enter")]["reg25_pos"]
    A2 = edges[("BOTTOM", "exit")] ["reg25_pos"]
    B1 = edges[("TOP",    "enter")]["reg25_pos"]
    B2 = edges[("TOP",    "exit")] ["reg25_pos"]

    if None in (A1, A2, B1, B2):
        results["official"] = {
            "ok": False,
            "reason": f"missing reg25 reading at one of the edges (A1={A1}, A2={A2}, B1={B1}, B2={B2})",
        }
        return results

    bottom_gauge_counts = A2 - A1
    top_gauge_counts    = B2 - B1
    enter_offset_counts = B1 - A1
    exit_offset_counts  = B2 - A2

    if bottom_gauge_counts <= 0 or top_gauge_counts <= 0:
        results["official"] = {
            "ok": False,
            "reason": (
                f"non-positive gauge traversal counts "
                f"(BOTTOM={bottom_gauge_counts}, TOP={top_gauge_counts}). "
                f"Edge order wrong or motion ran backwards."
            ),
        }
        return results

    # The two independent dx estimates - encoder-only, no time, no speed.
    dx_via_B_mm = GAUGE_WIDTH_MM * enter_offset_counts / bottom_gauge_counts
    dx_via_T_mm = GAUGE_WIDTH_MM *  exit_offset_counts / top_gauge_counts
    dx_mean_mm  = 0.5 * (dx_via_B_mm + dx_via_T_mm)

    # ---- Consistency check (i): gauge widths must agree ----
    gauge_mean_counts = 0.5 * (bottom_gauge_counts + top_gauge_counts)
    gauge_disagreement_frac = (
        abs(bottom_gauge_counts - top_gauge_counts) / max(abs(gauge_mean_counts), 1.0)
    )

    if gauge_disagreement_frac > GAUGE_CONSISTENCY_TOL:
        results["official"] = {
            "ok": False,
            "reason": (
                f"gauge width disagreement: BOTTOM saw {bottom_gauge_counts} counts, "
                f"TOP saw {top_gauge_counts} counts -- diff = "
                f"{gauge_disagreement_frac*100:.1f}% > {GAUGE_CONSISTENCY_TOL*100:.0f}%. "
                f"One sensor glitched mid-crossing (lost or re-acquired sight of the "
                f"gauge). Check the live plot to see which."
            ),
            "bottom_gauge_counts": bottom_gauge_counts,
            "top_gauge_counts": top_gauge_counts,
            "enter_offset_counts": enter_offset_counts,
            "exit_offset_counts": exit_offset_counts,
            "dx_via_BOTTOM_mm": dx_via_B_mm,
            "dx_via_TOP_mm": dx_via_T_mm,
            "gauge_disagreement_frac": gauge_disagreement_frac,
        }
        return results

    # ---- Consistency check (ii): X-offset must be the same at enter and exit ----
    offset_diff_counts = abs(enter_offset_counts - exit_offset_counts)

    if offset_diff_counts > OFFSET_CONSISTENCY_TOL_COUNTS:
        results["official"] = {
            "ok": False,
            "reason": (
                f"enter offset ({enter_offset_counts} counts) and exit offset "
                f"({exit_offset_counts} counts) disagree by {offset_diff_counts} "
                f"counts > {OFFSET_CONSISTENCY_TOL_COUNTS}. The gauge edges may "
                f"not be parallel, or one transition was mis-detected."
            ),
            "bottom_gauge_counts": bottom_gauge_counts,
            "top_gauge_counts": top_gauge_counts,
            "enter_offset_counts": enter_offset_counts,
            "exit_offset_counts": exit_offset_counts,
            "dx_via_BOTTOM_mm": dx_via_B_mm,
            "dx_via_TOP_mm": dx_via_T_mm,
            "offset_disagreement_counts": offset_diff_counts,
        }
        return results

    if abs(dx_mean_mm) > MAX_REASONABLE_DX_MM:
        results["official"] = {
            "ok": False,
            "reason": (
                f"dx = {dx_mean_mm*1000:+.1f} um exceeds sanity limit of "
                f"{MAX_REASONABLE_DX_MM*1000:.0f} um."
            ),
            "dx_mean_mm": dx_mean_mm,
        }
        return results

    if dx_mean_mm > 0:
        direction = "Move TOP sensor OPPOSITE the M6 motion direction"
    elif dx_mean_mm < 0:
        direction = "Move TOP sensor WITH the M6 motion direction"
    else:
        direction = "No X correction needed"

    # Effective counts-per-mm at this gauge, for the operator's sanity (NOT
    # used by the math). Derived purely from the gauge crossing - the encoder
    # spec is never consulted.
    effective_counts_per_mm = gauge_mean_counts / GAUGE_WIDTH_MM

    results["official"] = {
        "ok": True,
        "source": "encoder counts (time-independent)",
        "gauge_width_mm": GAUGE_WIDTH_MM,
        "bottom_gauge_counts": bottom_gauge_counts,
        "top_gauge_counts": top_gauge_counts,
        "gauge_disagreement_frac": gauge_disagreement_frac,
        "enter_offset_counts": enter_offset_counts,
        "exit_offset_counts": exit_offset_counts,
        "offset_disagreement_counts": offset_diff_counts,
        "dx_via_BOTTOM_mm": dx_via_B_mm,
        "dx_via_TOP_mm": dx_via_T_mm,
        "dx_mean_mm": dx_mean_mm,
        "abs_correction_mm": abs(dx_mean_mm),
        "effective_counts_per_mm": effective_counts_per_mm,
        "recommendation": direction,
    }
    return results


# =============================================================================
# Motion recording (unchanged from v3)
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

    msg = f"[m6] command -> POS{HCMD_GO_POS}; recording continuously..."
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
            msg = f"[m6] arrived at POS{HST_IN_POS} by status; reg25={pos}"
            print(msg)
            if ui_queue:
                ui_queue.put(("status", msg))
            break

        if motion_seen and time.time() - stable_since >= STABLE_DWELL_SEC:
            msg = f"[m6] position stable; assuming arrived/stopped; reg25={pos}, status={status}"
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
            summary_rows.append({
                "edge": edge_name,
                "key": key,
                "value": value,
            })

    sdf = pd.DataFrame(summary_rows)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="samples")
        sdf.to_excel(writer, index=False, sheet_name="summary")

    print(f"[export] {len(df)} rows -> {path}")


def export_edge_plot(rows, results, path):
    """Plot showing the full gauge crossing for both sensors with all four
    edges marked. X axis is encoder counts (reg25) so the picture matches the
    math: encoder distance is what we measure."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return

    enter = results.get("enter", {})
    exit_ = results.get("exit", {})
    if not rows or not (enter.get("ok") and exit_.get("ok")):
        return

    df = pd.DataFrame(rows)
    if df.empty or df["reg25_pos"].isna().all():
        return

    # Window of interest in reg25 around the gauge crossing.
    A1 = enter.get("bottom_reg25")
    B2 = exit_.get("top_reg25")
    if A1 is None or B2 is None:
        return

    span = max(1, B2 - A1)
    pad = int(0.25 * span)
    r_min = A1 - pad
    r_max = B2 + pad

    cut = df[(df["reg25_pos"] >= r_min) & (df["reg25_pos"] <= r_max)].copy()
    if cut.empty:
        return

    fig, ax = plt.subplots(figsize=(10, 5))

    for sensor in ("TOP", "BOTTOM"):
        s = cut[cut["sensor"] == sensor]
        ax.plot(s["reg25_pos"], s["value_mm"], ".", label=sensor, markersize=4)

    for sensor, transition, style in (
        ("BOTTOM", "enter", ":"),
        ("BOTTOM", "exit",  ":"),
        ("TOP",    "enter", "--"),
        ("TOP",    "exit",  "--"),
    ):
        r = results.get(transition, {})
        if not r.get("ok"):
            continue
        x = r["bottom_reg25"] if sensor == "BOTTOM" else r["top_reg25"]
        ax.axvline(x, linestyle=style, alpha=0.7, label=f"{sensor} {transition.upper()}")

    o = results.get("official", {})
    if o.get("ok"):
        title = (
            f"Encoder-only gauge crossing (W = {GAUGE_WIDTH_MM:.3f} mm)\n"
            f"BOTTOM gauge: {o['bottom_gauge_counts']} cnt  |  "
            f"TOP gauge: {o['top_gauge_counts']} cnt  "
            f"(disagree {o['gauge_disagreement_frac']*100:.1f}%)\n"
            f"enter offset: {o['enter_offset_counts']} cnt  |  "
            f"exit offset: {o['exit_offset_counts']} cnt  "
            f"(diff {o['offset_disagreement_counts']} cnt)\n"
            f"dx_B = {o['dx_via_BOTTOM_mm']*1000:+.2f} um  |  "
            f"dx_T = {o['dx_via_TOP_mm']*1000:+.2f} um  |  "
            f"mean = {o['dx_mean_mm']*1000:+.2f} um  ->  "
            f"move TOP {o['abs_correction_mm']*1000:.2f} um"
        )
    else:
        title = f"INVALID: {o.get('reason', 'unknown')}"

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
    print("Horizontal beam alignment result (v4, encoder-only)")
    print("=" * 76)
    print(f"GAUGE_WIDTH_MM                  = {GAUGE_WIDTH_MM:.6f} mm")
    print(f"GAUGE_CONSISTENCY_TOL           = {GAUGE_CONSISTENCY_TOL*100:.0f}%")
    print(f"OFFSET_CONSISTENCY_TOL_COUNTS   = {OFFSET_CONSISTENCY_TOL_COUNTS} counts")
    print(f"EDGE_CONFIRM_SEC                = {EDGE_CONFIRM_SEC:.3f} s")
    print(f"EDGE_CONFIRM_RATIO              = {EDGE_CONFIRM_RATIO:.2f}")

    for transition in ("enter", "exit"):
        r = results.get(transition, {})
        print(f"\n{transition.upper()} edge:")
        if not r.get("ok"):
            print(f"  not found: {r.get('reason', 'unknown reason')}")
            continue
        print(f"  TOP reg25:       {r['top_reg25']}")
        print(f"  BOTTOM reg25:    {r['bottom_reg25']}")
        print(f"  TOP - BOTTOM:    {r['dreg25_top_minus_bottom']:+} counts")
        print(f"  (informational) dt TOP-BOTTOM: {r['dt_sec_top_minus_bottom']*1000:+.3f} ms")

    o = results.get("official", {})
    print("\nOFFICIAL CORRECTION (encoder-only, time-independent):")
    if o.get("ok"):
        print(f"  BOTTOM gauge crossing: {o['bottom_gauge_counts']} counts")
        print(f"  TOP    gauge crossing: {o['top_gauge_counts']} counts")
        print(f"  gauge disagreement:    {o['gauge_disagreement_frac']*100:.2f}%")
        print(f"  enter offset (B-A):    {o['enter_offset_counts']:+} counts")
        print(f"  exit  offset (B-A):    {o['exit_offset_counts']:+} counts")
        print(f"  offset disagreement:   {o['offset_disagreement_counts']} counts")
        print(f"  effective scale:       {o['effective_counts_per_mm']:.2f} counts/mm")
        print()
        print(f"  dx via BOTTOM:         {o['dx_via_BOTTOM_mm']*1000:+.2f} um")
        print(f"  dx via TOP:            {o['dx_via_TOP_mm']*1000:+.2f} um")
        print(f"  dx final (mean):       {o['dx_mean_mm']*1000:+.2f} um")
        print(f"  move amount:           {o['abs_correction_mm']*1000:.2f} um")
        print(f"  recommendation:        {o['recommendation']}")
    else:
        print(f"  INVALID: {o.get('reason', 'unknown reason')}")
        for k in ("bottom_gauge_counts", "top_gauge_counts",
                  "enter_offset_counts", "exit_offset_counts",
                  "dx_via_BOTTOM_mm", "dx_via_TOP_mm"):
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
        f"horizontal_beam_alignment_v4_{datetime.now():%Y%m%d_%H%M%S}.xlsx",
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

        if nt < 5 or nb < 5:
            msg = "[warn] sensors not streaming enough samples yet, continuing anyway..."
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
        self.root.title("Horizontal Beam Alignment Test  (v4, encoder-only)")
        self.root.geometry("980x720")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self.root.after(100, self._process_queue)

    def _on_close(self):
        # Stop scheduling new after() calls before destroying the root, so
        # the polling loop doesn't fire against a dead widget.
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

        result_frame = ttk.LabelFrame(self.root, text="Official result (encoder-only)", padding=10)
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
            run_test(
                host=self.host,
                port=self.port,
                config=self.config,
                timeout=self.timeout,
                ui_queue=self.q,
            )
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
                self.ax.plot(s["t_sec"], s["value_mm"], ".", label=sensor, markersize=3)
        self.ax.set_xlabel("time [s]")
        self.ax.set_ylabel("value [mm]")
        self.ax.set_title("Live TOP/BOTTOM samples")
        self.ax.grid(True, alpha=0.3)
        self.ax.legend(loc="best")
        self.canvas.draw_idle()

    def _show_result(self, results, xlsx_path, plot_path):
        o = results.get("official", {})

        if o.get("ok"):
            dx_um = o["dx_mean_mm"] * 1000.0
            move_um = o["abs_correction_mm"] * 1000.0
            self.dx_var.set(
                f"dx = {dx_um:+.2f} um   "
                f"(B: {o['dx_via_BOTTOM_mm']*1000:+.2f}  "
                f"T: {o['dx_via_TOP_mm']*1000:+.2f}  "
                f"Δgauge {o['gauge_disagreement_frac']*100:.1f}%)"
            )
            self.move_var.set(f"Move TOP: {move_um:.2f} um")
            self.rec_var.set(f"Recommendation: {o['recommendation']}")
            self.status_var.set("Done")
        else:
            self.dx_var.set("dx: INVALID")
            self.move_var.set("Move TOP: ---")
            self.rec_var.set(f"Reason: {o.get('reason', 'unknown reason')}")
            self.status_var.set("Done - invalid result")

        lines = []
        for edge in ("enter", "exit"):
            r = results.get(edge, {})
            if r.get("ok"):
                lines.append(
                    f"{edge.upper()}: reg25 TOP-BOTTOM = {r['dreg25_top_minus_bottom']:+} counts"
                )
            else:
                lines.append(f"{edge.upper()}: not found")

        if o.get("ok"):
            lines.append("")
            lines.append(
                f"BOTTOM gauge: {o['bottom_gauge_counts']} cnt   "
                f"TOP gauge: {o['top_gauge_counts']} cnt   "
                f"(disagree {o['gauge_disagreement_frac']*100:.2f}%)"
            )
            lines.append(
                f"enter offset: {o['enter_offset_counts']:+} cnt   "
                f"exit offset: {o['exit_offset_counts']:+} cnt   "
                f"(diff {o['offset_disagreement_counts']} cnt)"
            )
            lines.append(f"effective scale: {o['effective_counts_per_mm']:.2f} counts/mm")

        self.edge_var.set("\n".join(lines))
        self.files_var.set(f"Excel: {xlsx_path}\nPlot: {plot_path}")

        self._show_final_crossing_plot(results)

    def _show_final_crossing_plot(self, results):
        if self.ax is None or not self.rows_for_live:
            return

        enter = results.get("enter", {})
        exit_ = results.get("exit", {})
        if not (enter.get("ok") and exit_.get("ok")):
            return

        df = pd.DataFrame(self.rows_for_live)
        if df.empty or df["reg25_pos"].isna().all():
            return

        A1 = enter.get("bottom_reg25")
        B2 = exit_.get("top_reg25")
        if A1 is None or B2 is None:
            return

        span = max(1, B2 - A1)
        pad = int(0.25 * span)
        r_min = A1 - pad
        r_max = B2 + pad

        cut = df[(df["reg25_pos"] >= r_min) & (df["reg25_pos"] <= r_max)].copy()
        if cut.empty:
            return

        self.ax.clear()
        for sensor in ("TOP", "BOTTOM"):
            s = cut[cut["sensor"] == sensor]
            if not s.empty:
                self.ax.plot(s["reg25_pos"], s["value_mm"], ".",
                             label=sensor, markersize=4)

        for sensor, transition, style in (
            ("BOTTOM", "enter", ":"),
            ("BOTTOM", "exit",  ":"),
            ("TOP",    "enter", "--"),
            ("TOP",    "exit",  "--"),
        ):
            r = results.get(transition, {})
            if not r.get("ok"):
                continue
            x = r["bottom_reg25"] if sensor == "BOTTOM" else r["top_reg25"]
            self.ax.axvline(x, linestyle=style, linewidth=1.5, alpha=0.85,
                            label=f"{sensor} {transition.upper()}")

        o = results.get("official", {})
        if o.get("ok"):
            title = (
                f"Encoder-only crossing (W = {GAUGE_WIDTH_MM:.3f} mm)\n"
                f"dx_B = {o['dx_via_BOTTOM_mm']*1000:+.2f}  "
                f"dx_T = {o['dx_via_TOP_mm']*1000:+.2f}  "
                f"mean = {o['dx_mean_mm']*1000:+.2f} um"
            )
        else:
            title = f"INVALID: {o.get('reason', 'unknown')}"

        self.ax.set_title(title, fontsize=9)
        self.ax.set_xlabel("encoder position reg25 [counts]")
        self.ax.set_ylabel("sensor value [mm]")
        self.ax.grid(True, alpha=0.3)
        self.ax.legend(loc="best", fontsize=8)
        self.canvas.draw_idle()

    def run(self):
        self.root.mainloop()


# =============================================================================
# Main
# =============================================================================
def main():
    ap = argparse.ArgumentParser(description="Horizontal beam alignment test (v4, encoder-only)")
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
