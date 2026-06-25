# -*- coding: utf-8 -*-
"""
Thickness DRIFT logger (remote-control) - v2: per-sensor std.

In addition to the std of the combined THICKNESS, every sample now also records
the standard deviation of EACH sensor on its own (top_std_um / bot_std_um). This
lets you study how each head behaves independently - a noisy or drifting single
sensor is visible directly instead of being hidden inside the combined number.
The per-sensor noise is written to the CSV, printed on the console line,
summarised at the end of the run, and annotated on the per-sensor plot panel.
"""

import os
import sys
import time
import csv
import ssl
import platform
import urllib.request
import urllib.parse
from datetime import datetime

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

import thickness_Module_457 as thck


# =============================================================================
# CONFIG - edit these freely
# =============================================================================

# Nominal thickness of the measured object [mm]. CHANGE THIS when you swap the
# reference object. Kept hard-coded on purpose so it is trivial to update.
NOMINAL_THICKNESS_MM = 2.000

# A single reading deviating from NOMINAL by more than this raises an alert.
# This is a sanity flag (vibration / part moved / bad reading), NOT a drift alarm.
ALERT_DEVIATION_UM = 1.5

# Time between the START of consecutive samples [s].
# 10 s gives nice resolution for an hourly check and stays small over a full day
# (a 24 h run is ~8640 rows). Lower it for finer detail, raise it for very long runs.
SAMPLE_INTERVAL_SEC = 10.0

# Averaging window per sample [s]. Each logged value is the median over this
# window, which suppresses per-frame noise. Keep it well below SAMPLE_INTERVAL_SEC.
MEASURE_WINDOW_SEC = 2.0

# Per-sample window std [um] above which the window is flagged "unstable" -
# the value is still logged, just noted as noisy. Used only as a quality flag.
STABILITY_MAX_STD_UM = 5.0

# Total run length [hours]. Set to None to run until Ctrl+C (good for "let it run
# overnight"). Set e.g. 1.0 for an hourly test or 24.0 for a daily test.
MAX_DURATION_HOURS = None

# Re-draw the live PNG every N samples so you can open it while the run continues.
PLOT_REFRESH_EVERY = 30

# --- Warm-up & residual-drift analysis ---------------------------------------
# Sensors always keep some residual drift, so there is no single permanent
# "final value". Instead we detect when the fast warm-up transient ENDS - the
# drift RATE drops below WARMUP_RATE_UM_PER_H and stays below it for
# WARMUP_CONFIRM_MIN - take the value there as the post-warm-up reference, and
# report the slow residual drift relative to that reference.
# Threshold must sit clearly ABOVE the residual drift + rate-estimate noise,
# or warm-up never registers as "done". 1.0 um/h gives that margin; tighten it
# if your residual drift is much smaller.
WARMUP_RATE_UM_PER_H = 1.0      # rate threshold that defines "warm-up done"
WARMUP_RATE_WINDOW_MIN = 20.0   # trailing window for the local drift-rate fit
WARMUP_CONFIRM_MIN = 20.0       # rate must stay below threshold this long to confirm

# Beep on the Windows host when an alert fires (ignored on other platforms).
BEEP_ON_ALERT = True

# Output files (timestamped per run so successive runs never overwrite).
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drift_logs")

# --- Remote monitoring via Telegram (heartbeat + watchdog) -------------------
# Fill these in to get phone notifications while you are away. Leave empty to
# disable all remote messaging (the local status file is still written).
# Setup (~5 min): talk to @BotFather -> /newbot -> copy the token below; then
# message your new bot once, open
# https://api.telegram.org/bot<token>/getUpdates and copy the numeric "chat id".
TELEGRAM_BOT_TOKEN = "8383038489:AAFgj0-xjqPtlDwQe4OJWKrCoNHzXHjoyks"
TELEGRAM_CHAT_ID = "548093301"
# Send a periodic "still alive" heartbeat every N minutes. If you STOP receiving
# it, that absence is itself the warning that the run (or the PC) died.
HEARTBEAT_EVERY_MIN = 60

# Attach the current drift plot (PNG) to each heartbeat, so you can see at a
# glance that the data looks right - not just that the run is alive. Set False
# for text-only heartbeats.
HEARTBEAT_SEND_PLOT = True

# If no valid reading arrives for this many seconds, raise a one-shot PROBLEM
# alert (e.g. a sensor went silent). A RECOVERED message is sent when it resumes.
STALL_ALERT_SEC = 120

# Always-current human-readable status file (any remote-access method can read it).
STATUS_FILE = os.path.join(OUTPUT_DIR, "status.txt")

# Corporate networks often run a TLS-inspection proxy whose self-signed root CA
# Python does not trust, giving "CERTIFICATE_VERIFY_FAILED". Two ways to fix:
#   * Proper:  pip install truststore  -> code below auto-uses the OS/Windows
#     trust store (which already trusts the corporate root), verification stays on.
#   * Quick:   set TELEGRAM_INSECURE_SSL = True to skip verification for the
#     Telegram calls only. Acceptable here (heartbeat traffic only, and the
#     proxy already inspects it), but the proper fix above is preferred.
TELEGRAM_INSECURE_SSL = False

# =============================================================================
# Helpers
# =============================================================================
def _measure_window_direct(state, window_sec):

    # Mark the newest sample already present on each sensor, then watch only what
    # arrives during the window.
    with state.lock:
        top0 = state.samples["TOP"][-1][0] if state.samples["TOP"] else 0.0
        bot0 = state.samples["BOTTOM"][-1][0] if state.samples["BOTTOM"] else 0.0

    time.sleep(max(0.5, float(window_sec)))

    with state.lock:
        top = [v for (ts, v) in state.samples["TOP"] if ts > top0]
        bot = [v for (ts, v) in state.samples["BOTTOM"] if ts > bot0]

    # Keep only in-range readings (an out-of-range / no-object reading sits at or
    # above the error threshold).
    thr = float(thck.Cfg.ERROR_THRESHOLD_MM)
    top = [v for v in top if v < thr]
    bot = [v for v in bot if v < thr]

    n_top, n_bot = len(top), len(bot)
    n = min(n_top, n_bot)
    if n < 5:
        return {"ok": False, "reason": "too few samples", "n": n,
                "raw_median_mm": None, "std_um": None,
                "top_std_um": None, "bot_std_um": None,
                "top_abs_mm": None, "bot_abs_mm": None,
                "n_top": n_top, "n_bot": n_bot}

    top_arr = np.array(top, dtype=float)
    bot_arr = np.array(bot, dtype=float)
    top_med = float(np.median(top_arr))
    bot_med = float(np.median(bot_arr))
    raw = float(thck.Cfg.SENSOR_DISTANCE_MM - (top_med + bot_med))
    # Per-sensor in-window noise: the std of EACH sensor on its own [um]. This
    # exposes how steady each head is by itself, instead of only the combined
    # thickness number - a noisy TOP and a quiet BOTTOM look identical in the
    # combined figure but tell very different stories about each sensor.
    top_std_um = float(np.std(top_arr) * 1000.0)
    bot_std_um = float(np.std(bot_arr) * 1000.0)
    # Thickness noise combines both sensors' noise in quadrature.
    std_um = float(np.sqrt(np.var(top_arr) + np.var(bot_arr)) * 1000.0)
    ok = std_um <= float(STABILITY_MAX_STD_UM)
    return {"ok": ok, "reason": ("" if ok else "unstable (std too high)"),
            "n": n, "raw_median_mm": raw, "std_um": std_um,
            "top_std_um": top_std_um, "bot_std_um": bot_std_um,
            "top_abs_mm": top_med, "bot_abs_mm": bot_med,
            "n_top": n_top, "n_bot": n_bot}


# =============================================================================
# Remote monitoring helpers (Telegram heartbeat + watchdog) + status file
# =============================================================================
def _ssl_ctx():

    if TELEGRAM_INSECURE_SSL:
        return ssl._create_unverified_context()
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return ssl.create_default_context()


def _telegram_send(text):
    """Best-effort Telegram message. Never raises; returns True on success."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx()) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception as e:
        print(f"[warn] telegram send failed: {e}")
        return False


def _telegram_send_photo(png_path, caption=""):
    """Best-effort Telegram photo (multipart, stdlib only). Never raises."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    try:
        if not os.path.isfile(png_path):
            return False
        with open(png_path, "rb") as fh:
            img = fh.read()
        boundary = "----driftlogger" + str(int(time.time() * 1000))
        fields = [("chat_id", str(TELEGRAM_CHAT_ID))]
        if caption:
            fields.append(("caption", caption))
        body = b""
        for name, val in fields:
            body += (f"--{boundary}\r\n"
                     f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                     f"{val}\r\n").encode("utf-8")
        body += (f"--{boundary}\r\n"
                 f"Content-Disposition: form-data; name=\"photo\"; filename=\"drift.png\"\r\n"
                 f"Content-Type: image/png\r\n\r\n").encode("utf-8")
        body += img + f"\r\n--{boundary}--\r\n".encode("utf-8")
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        req = urllib.request.Request(url, data=body)
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        with urllib.request.urlopen(req, timeout=30, context=_ssl_ctx()) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception as e:
        print(f"[warn] telegram photo failed: {e}")
        return False


def _write_status_file(path, fields):
    """Overwrite a small human-readable status file. Never raises."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(f"{k}: {v}" for k, v in fields.items()) + "\n")
    except Exception:
        pass


def _build_heartbeat_text(rows, t_start, nominal_mm, top_alive, bot_alive, health):
    good = [r for r in rows if r["thickness_mm"] is not None]
    uptime_h = (time.time() - t_start) / 3600.0
    lines = [
        f"\U0001FAC0 Drift logger heartbeat - {health}",
        f"uptime: {uptime_h:.1f} h",
        f"samples: {len(rows)} (valid {len(good)})",
        f"sensors: TOP {'OK' if top_alive else 'SILENT'} | BOTTOM {'OK' if bot_alive else 'SILENT'}",
    ]
    if good:
        last = good[-1]["thickness_mm"]
        dev = (last - nominal_mm) * 1000.0
        thk = [r["thickness_mm"] for r in good]
        span_um = (max(thk) - min(thk)) * 1000.0
        lines.append(f"last: {last:.4f} mm (dev {dev:+.2f} um)")
        lines.append(f"total drift span: {span_um:.2f} um")
        # Per-sensor short-term noise, so the heartbeat shows each head on its own.
        top_noise = [r["top_std_um"] for r in good if r.get("top_std_um") is not None]
        bot_noise = [r["bot_std_um"] for r in good if r.get("bot_std_um") is not None]
        if top_noise and bot_noise:
            lines.append(f"sensor noise: TOP {np.mean(top_noise):.2f} um | "
                         f"BOTTOM {np.mean(bot_noise):.2f} um")
    return "\n".join(lines)


def _warmup_analysis(rows, nominal_mm):

    good = [r for r in rows if r["thickness_mm"] is not None]
    if len(good) < 20:
        return None
    times = [r["dt"] for r in good]
    t0 = times[0]
    t = np.array([(d - t0).total_seconds() for d in times], dtype=float)
    y = np.array([(r["thickness_mm"] - nominal_mm) * 1000.0 for r in good], dtype=float)
    n = len(y)

    # Cumulative sums for O(1) windowed least-squares slope.
    cs_t = np.insert(np.cumsum(t), 0, 0.0)
    cs_y = np.insert(np.cumsum(y), 0, 0.0)
    cs_ty = np.insert(np.cumsum(t * y), 0, 0.0)
    cs_tt = np.insert(np.cumsum(t * t), 0, 0.0)

    def slope_um_per_h(lo, hi):
        m = hi - lo
        if m < 2:
            return 0.0
        Sx = cs_t[hi] - cs_t[lo]
        Sy = cs_y[hi] - cs_y[lo]
        Sxy = cs_ty[hi] - cs_ty[lo]
        Sxx = cs_tt[hi] - cs_tt[lo]
        denom = m * Sxx - Sx * Sx
        if abs(denom) < 1e-9:
            return 0.0
        return ((m * Sxy - Sx * Sy) / denom) * 3600.0

    w = max(2, int(round(WARMUP_RATE_WINDOW_MIN * 60.0 / max(SAMPLE_INTERVAL_SEC, 1e-6))))
    rate = np.array([slope_um_per_h(max(0, i - w + 1), i + 1) for i in range(n)])

    c = max(1, int(round(WARMUP_CONFIRM_MIN * 60.0 / max(SAMPLE_INTERVAL_SEC, 1e-6))))
    thr = float(WARMUP_RATE_UM_PER_H)

    # First index where |rate| stays <= thr for a sustained run of length c.
    warm_idx = None
    run_start = None
    for i in range(n):
        if abs(rate[i]) <= thr:
            if run_start is None:
                run_start = i
            if i - run_start + 1 >= c:
                warm_idx = run_start
                break
        else:
            run_start = None

    res = {"warm_done": warm_idx is not None, "warm_min": None, "warm_dt": None,
           "ref_um": None, "ref_mm": None, "residual_rate_um_h": None,
           "residual_total_um": None, "current_rate_um_h": float(rate[-1])}
    if warm_idx is not None:
        res["warm_dt"] = times[warm_idx]
        res["warm_min"] = t[warm_idx] / 60.0
        conf_hi = min(n, warm_idx + c)
        ref_um = float(np.median(y[warm_idx:conf_hi]))
        res["ref_um"] = ref_um
        res["ref_mm"] = nominal_mm + ref_um / 1000.0
        res["residual_rate_um_h"] = slope_um_per_h(warm_idx, n)
        tail_lvl = float(np.median(y[max(warm_idx, n - c):n]))
        res["residual_total_um"] = tail_lvl - ref_um
    return res


def _beep():
    if not BEEP_ON_ALERT:
        return
    try:
        if platform.system() == "Windows":
            import winsound
            winsound.Beep(1200, 200)
        else:
            sys.stdout.write("\a")
            sys.stdout.flush()
    except Exception:
        pass


def _make_plot(rows, png_path, nominal_mm, alert_um):
    """Render thickness drift + per-sensor decomposition to png_path. Never raises."""
    try:
        good = [r for r in rows if r["thickness_mm"] is not None]
        if len(good) < 2:
            return
        times = [r["dt"] for r in good]
        thk_um = [(r["thickness_mm"] - nominal_mm) * 1000.0 for r in good]  # deviation in um

        # Bottom panel needs per-sensor data; only plot it if we have it.
        sensor_good = [r for r in good if r.get("top_abs_mm") is not None and r.get("bot_abs_mm") is not None]
        have_sensors = len(sensor_good) >= 2

        if have_sensors:
            fig, (ax, ax2) = plt.subplots(2, 1, figsize=(11, 8.5), sharex=True)
        else:
            fig, ax = plt.subplots(figsize=(11, 5.5))

        # --- Top panel: thickness deviation from nominal ---
        ax.plot(times, thk_um, "-", color="#1f77b4", linewidth=1.0, label="deviation from nominal")
        ax.axhline(0.0, color="#444444", linewidth=1.0, linestyle="--", label="nominal")
        ax.axhline(+alert_um, color="#d62728", linewidth=0.8, linestyle=":", label=f"+/-{alert_um} um alert band")
        ax.axhline(-alert_um, color="#d62728", linewidth=0.8, linestyle=":")

        # Warm-up end (rate-based) + post-warm-up reference + residual drift, so
        # the meaningful numbers are readable without assuming a fixed final value.
        wa = _warmup_analysis(good, nominal_mm)
        if wa is not None:
            if wa["warm_done"]:
                ax.axvline(wa["warm_dt"], color="#9467bd", linewidth=1.0, linestyle="-.",
                           label=f"warm-up end @ {wa['warm_min']:.0f} min")
                ax.axhline(wa["ref_um"], color="#9467bd", linewidth=1.2,
                           label=f"post-warm-up {wa['ref_um']:+.2f} um")
                txt = (f"warm-up: {wa['warm_min']:.0f} min  (rate < {WARMUP_RATE_UM_PER_H} um/h)\n"
                       f"post-warm-up: {wa['ref_um']:+.2f} um ({wa['ref_mm']:.4f} mm)\n"
                       f"residual drift: {wa['residual_rate_um_h']:+.2f} um/h "
                       f"({wa['residual_total_um']:+.2f} um since)")
            else:
                txt = (f"warm-up not finished\n"
                       f"current rate: {wa['current_rate_um_h']:+.2f} um/h")
            ax.text(0.99, 0.03, txt, transform=ax.transAxes, ha="right", va="bottom",
                    fontsize=8, bbox=dict(boxstyle="round", fc="white", ec="#9467bd", alpha=0.85))

        ax.set_ylabel("thickness dev. from nominal [um]")
        ax.set_title("Thickness drift over time")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best", fontsize=8)

        # --- Bottom panel: each sensor's drift from its OWN start ---
        # Reveals whether the two heads move together (common-mode) or apart
        # (differential / asymmetric thermal). Both referenced to their first value.
        if have_sensors:
            s_times = [r["dt"] for r in sensor_good]
            top0 = sensor_good[0]["top_abs_mm"]
            bot0 = sensor_good[0]["bot_abs_mm"]
            top_drift = [(r["top_abs_mm"] - top0) * 1000.0 for r in sensor_good]
            bot_drift = [(r["bot_abs_mm"] - bot0) * 1000.0 for r in sensor_good]

            # Annotate each trace with that sensor's own noise (mean per-sample
            # window std), so the per-sensor behaviour is readable at a glance.
            top_noise = [r["top_std_um"] for r in sensor_good if r.get("top_std_um") is not None]
            bot_noise = [r["bot_std_um"] for r in sensor_good if r.get("bot_std_um") is not None]
            top_lbl = "TOP sensor drift" + (f"  (noise {np.mean(top_noise):.2f} um)" if top_noise else "")
            bot_lbl = "BOTTOM sensor drift" + (f"  (noise {np.mean(bot_noise):.2f} um)" if bot_noise else "")
            ax2.plot(s_times, top_drift, "-", color="#2ca02c", linewidth=1.0, label=top_lbl)
            ax2.plot(s_times, bot_drift, "-", color="#ff7f0e", linewidth=1.0, label=bot_lbl)
            ax2.axhline(0.0, color="#444444", linewidth=0.8, linestyle="--")
            ax2.set_ylabel("per-sensor drift from start [um]")
            ax2.set_title("Per-sensor decomposition (each vs its own start)")
            ax2.grid(True, alpha=0.3)
            ax2.legend(loc="best", fontsize=8)
            ax2.set_xlabel("time")
            ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        else:
            ax.set_xlabel("time")
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

        fig.autofmt_xdate()
        fig.tight_layout()
        fig.savefig(png_path, dpi=110)
        plt.close(fig)
    except Exception as e:
        print(f"[warn] plot failed: {e}")


def _print_summary(rows, nominal_mm):
    good = [r for r in rows if r["thickness_mm"] is not None]
    n_alerts = sum(1 for r in rows if r["alert"])
    print("\n" + "=" * 60)
    print("DRIFT RUN SUMMARY")
    print("=" * 60)
    print(f"samples logged       : {len(rows)}")
    print(f"valid readings       : {len(good)}")
    print(f"alerts (> +/-um)     : {n_alerts}")
    if good:
        vals_mm = np.array([r["thickness_mm"] for r in good], dtype=float)
        dev_um = (vals_mm - nominal_mm) * 1000.0
        span_um = float(vals_mm.max() - vals_mm.min()) * 1000.0
        elapsed_h = (good[-1]["dt"] - good[0]["dt"]).total_seconds() / 3600.0
        drift_per_h = (span_um / elapsed_h) if elapsed_h > 0 else float("nan")
        print(f"nominal              : {nominal_mm:.4f} mm")
        print(f"mean thickness       : {vals_mm.mean():.4f} mm  ({dev_um.mean():+.2f} um vs nominal)")
        print(f"min / max            : {vals_mm.min():.4f} / {vals_mm.max():.4f} mm")
        print(f"total drift (span)   : {span_um:.2f} um over {elapsed_h:.2f} h")
        print(f"drift rate           : {drift_per_h:.2f} um/h")

        # --- Per-sensor behaviour (each head on its own) -------------------
        # Two distinct numbers per sensor:
        #   * window noise  = typical short-term jitter (mean of the per-sample
        #     window std) -> how steady the head is moment-to-moment.
        #   * run variation = std of that sensor's readings over the WHOLE run
        #     -> how much the head itself wandered/drifted end-to-end.
        for label, noise_key, abs_key in (("TOP", "top_std_um", "top_abs_mm"),
                                          ("BOTTOM", "bot_std_um", "bot_abs_mm")):
            noise = [r[noise_key] for r in good if r.get(noise_key) is not None]
            absv = [r[abs_key] for r in good if r.get(abs_key) is not None]
            mean_noise = float(np.mean(noise)) if noise else float("nan")
            run_var_um = float(np.std(np.array(absv, dtype=float)) * 1000.0) if absv else float("nan")
            print(f"{label:<6} window noise   : {mean_noise:.2f} um  (mean per-sample std)")
            print(f"{label:<6} run variation  : {run_var_um:.2f} um  (std over whole run)")

        wa = _warmup_analysis(good, nominal_mm)
        if wa is not None:
            if wa["warm_done"]:
                print(f"warm-up time         : {wa['warm_min']:.0f} min  (rate < {WARMUP_RATE_UM_PER_H} um/h)")
                print(f"post-warm-up value   : {wa['ref_mm']:.4f} mm  ({wa['ref_um']:+.2f} um vs nominal)")
                print(f"residual drift rate  : {wa['residual_rate_um_h']:+.2f} um/h")
                print(f"residual drift total : {wa['residual_total_um']:+.2f} um since warm-up")
            else:
                print(f"warm-up time         : not finished (current rate {wa['current_rate_um_h']:+.2f} um/h)")
    print("=" * 60)


# =============================================================================
# Main
# =============================================================================
def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"drift_{stamp}.csv")
    png_path = os.path.join(OUTPUT_DIR, f"drift_{stamp}.png")

    print(f"Nominal           : {NOMINAL_THICKNESS_MM:.4f} mm")
    print(f"Alert deviation   : +/-{ALERT_DEVIATION_UM} um")
    print(f"Sample interval   : {SAMPLE_INTERVAL_SEC} s   (window {MEASURE_WINDOW_SEC} s)")
    print(f"Duration          : {'until Ctrl+C' if MAX_DURATION_HOURS is None else f'{MAX_DURATION_HOURS} h'}")
    print(f"CSV               : {csv_path}")
    print(f"Plot              : {png_path}")
    print("-" * 60)

    # --- מנוע המדידה: נשענים על thck.Cfg (נטען אוטומטית בייבוא המודול) ---
    mod = thck.ThicknessModule()
    # Start ONLY the sensor readers, not the ThicknessProcessor: we read each
    # sensor's stream directly (see _measure_window_direct) and must not let the
    # processor drain the sample deques out from under us.
    for r in mod.readers:
        r.start()
    # Give the sensor threads a moment to start receiving packets.
    time.sleep(2.0)

    rows = []
    fieldnames = [
        "iso_time", "elapsed_sec",
        "thickness_mm", "deviation_um",
        "top_abs_mm", "bot_abs_mm",
        "window_std_um", "top_std_um", "bot_std_um", "n_samples",
        "alert", "note",
    ]

    t_start = time.time()
    deadline = None if MAX_DURATION_HOURS is None else t_start + MAX_DURATION_HOURS * 3600.0
    sample_idx = 0

    # --- Remote-monitoring state (heartbeat + watchdog) ---
    remote_on = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    print(f"Remote monitor    : {'Telegram ON' if remote_on else 'OFF (token/chat id not set)'}")
    last_heartbeat = t_start
    last_valid_ts = t_start
    health = "OK"
    n_top = n_bot = 0
    if remote_on:
        _telegram_send(
            f"▶️ Drift logger STARTED at {datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"nominal {NOMINAL_THICKNESS_MM:.4f} mm, interval {SAMPLE_INTERVAL_SEC:.0f}s\n"
            f"heartbeat every {HEARTBEAT_EVERY_MIN} min"
        )

    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            f.flush()

            while deadline is None or time.time() < deadline:
                loop_start = time.time()
                now = datetime.now()
                elapsed = loop_start - t_start

                thickness_mm = deviation_um = None
                std_um = None
                top_std_um = bot_std_um = None
                top_abs_mm = bot_abs_mm = None
                n = 0
                alert = False
                note = ""

                try:
                    res = _measure_window_direct(mod.state, MEASURE_WINDOW_SEC)
                    n = int(res.get("n") or 0)
                    std_um = res.get("std_um")
                    top_std_um = res.get("top_std_um")
                    bot_std_um = res.get("bot_std_um")
                    top_abs_mm = res.get("top_abs_mm")
                    bot_abs_mm = res.get("bot_abs_mm")
                    n_top = int(res.get("n_top") or 0)
                    n_bot = int(res.get("n_bot") or 0)
                    raw = res.get("raw_median_mm")
                    if raw is None:
                        note = res.get("reason") or "no reading"
                    else:
                        thickness_mm = float(raw)
                        deviation_um = (thickness_mm - NOMINAL_THICKNESS_MM) * 1000.0
                        if abs(deviation_um) > ALERT_DEVIATION_UM:
                            alert = True
                            note = f"deviation {deviation_um:+.2f} um > +/-{ALERT_DEVIATION_UM} um"
                        if not res.get("ok"):
                            # window was noisy/unstable - keep the value but flag it
                            note = (note + "; " if note else "") + (res.get("reason") or "unstable window")
                except Exception as e:
                    note = f"sample error: {e}"

                row = {
                    "iso_time": now.strftime("%Y-%m-%dT%H:%M:%S"),
                    "elapsed_sec": f"{elapsed:.1f}",
                    "thickness_mm": "" if thickness_mm is None else f"{thickness_mm:.5f}",
                    "deviation_um": "" if deviation_um is None else f"{deviation_um:+.2f}",
                    "top_abs_mm": "" if top_abs_mm is None else f"{top_abs_mm:.5f}",
                    "bot_abs_mm": "" if bot_abs_mm is None else f"{bot_abs_mm:.5f}",
                    "window_std_um": "" if std_um is None else f"{std_um:.2f}",
                    "top_std_um": "" if top_std_um is None else f"{top_std_um:.2f}",
                    "bot_std_um": "" if bot_std_um is None else f"{bot_std_um:.2f}",
                    "n_samples": n,
                    "alert": 1 if alert else 0,
                    "note": note,
                }
                writer.writerow(row)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass

                # Keep an in-memory copy (typed) for plotting/summary.
                rows.append({"dt": now, "thickness_mm": thickness_mm, "alert": alert,
                             "top_abs_mm": top_abs_mm, "bot_abs_mm": bot_abs_mm,
                             "top_std_um": top_std_um, "bot_std_um": bot_std_um})
                sample_idx += 1

                # Console line.
                if thickness_mm is None:
                    print(f"[{now:%H:%M:%S}] (no reading) {note}")
                else:
                    flag = "  <== ALERT" if alert else ""
                    sensor_noise = ""
                    if top_std_um is not None and bot_std_um is not None:
                        sensor_noise = f"  [TOP {top_std_um:.2f} | BOT {bot_std_um:.2f}]"
                    print(f"[{now:%H:%M:%S}] {thickness_mm:.5f} mm  "
                          f"dev {deviation_um:+.2f} um  std {std_um:.2f} um{sensor_noise}  n={n}{flag}")
                if alert:
                    _beep()

                # Periodic live plot refresh.
                if sample_idx % PLOT_REFRESH_EVERY == 0:
                    _make_plot(rows, png_path, NOMINAL_THICKNESS_MM, ALERT_DEVIATION_UM)

                # --- Watchdog: alert on a stall, recover when readings resume ---
                top_alive = n_top > 0
                bot_alive = n_bot > 0
                if thickness_mm is not None:
                    last_valid_ts = loop_start
                    if health == "STALL":
                        health = "OK"
                        _telegram_send(
                            f"✅ RECOVERED at {now:%H:%M:%S} - readings resumed. "
                            f"thickness {thickness_mm:.4f} mm"
                        )
                else:
                    if health == "OK" and (loop_start - last_valid_ts) > STALL_ALERT_SEC:
                        health = "STALL"
                        silent = []
                        if not top_alive:
                            silent.append("TOP")
                        if not bot_alive:
                            silent.append("BOTTOM")
                        which = (", ".join(silent) + " silent") if silent else (note or "no valid readings")
                        _telegram_send(
                            f"⚠️ PROBLEM at {now:%H:%M:%S} - no valid reading for "
                            f"{int(loop_start - last_valid_ts)}s ({which})"
                        )

                # --- Always-current status file (readable by any remote method) ---
                _write_status_file(STATUS_FILE, {
                    "last_update": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "health": health,
                    "uptime_h": f"{(loop_start - t_start) / 3600.0:.2f}",
                    "total_samples": len(rows),
                    "last_thickness_mm": "n/a" if thickness_mm is None else f"{thickness_mm:.5f}",
                    "last_deviation_um": "n/a" if deviation_um is None else f"{deviation_um:+.2f}",
                    "TOP_sensor": "OK" if top_alive else "SILENT",
                    "TOP_noise_um": "n/a" if top_std_um is None else f"{top_std_um:.2f}",
                    "BOTTOM_sensor": "OK" if bot_alive else "SILENT",
                    "BOTTOM_noise_um": "n/a" if bot_std_um is None else f"{bot_std_um:.2f}",
                    "csv": csv_path,
                })

                # --- Periodic heartbeat ("still alive") ---
                if loop_start - last_heartbeat >= HEARTBEAT_EVERY_MIN * 60:
                    last_heartbeat = loop_start
                    hb_text = _build_heartbeat_text(
                        rows, t_start, NOMINAL_THICKNESS_MM, top_alive, bot_alive, health)
                    if HEARTBEAT_SEND_PLOT:
                        # Refresh so the attached image is current as of this heartbeat,
                        # then send image+caption; fall back to text if the upload fails.
                        _make_plot(rows, png_path, NOMINAL_THICKNESS_MM, ALERT_DEVIATION_UM)
                        if not _telegram_send_photo(png_path, hb_text):
                            _telegram_send(hb_text)
                    else:
                        _telegram_send(hb_text)

                # Sleep the remainder of the interval (account for measurement time).
                sleep_left = SAMPLE_INTERVAL_SEC - (time.time() - loop_start)
                if sleep_left > 0:
                    time.sleep(sleep_left)

    except KeyboardInterrupt:
        print("\n[stopped by user]")
    finally:
        # Only the readers were started, so stop them directly (mod.stop() would
        # try to join the never-started processor thread).
        try:
            mod.state.stop_event.set()
            for r in mod.readers:
                try:
                    r.join(timeout=0.5)
                except Exception:
                    pass
        except Exception:
            pass
        _make_plot(rows, png_path, NOMINAL_THICKNESS_MM, ALERT_DEVIATION_UM)
        _print_summary(rows, NOMINAL_THICKNESS_MM)
        if remote_on:
            good = [r for r in rows if r["thickness_mm"] is not None]
            uptime_h = (time.time() - t_start) / 3600.0
            _telegram_send(
                f"⏹️ Drift logger STOPPED at {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                f"uptime {uptime_h:.1f} h, samples {len(rows)} (valid {len(good)})"
            )
        print(f"\nCSV : {csv_path}")
        print(f"Plot: {png_path}")


if __name__ == "__main__":
    main()
