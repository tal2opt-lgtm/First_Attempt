"""
sweep_range_analysis.py
=======================
הקלטת סריקה של חיישני העובי - תצוגה בלבד, בלי ניתוחים.

מעבירים את החלק על הטווח (או משאירים אותו סטטי); כל עוד שני החיישנים
רואים את החלק נצברות דגימות, וכשהוא יוצא מוצגים:
  - גרף העובי כפי שנמדד, לאורך זמן הסריקה
  - גרף ערכי שני החיישנים (מרחקים אבסולוטיים) כפי שהם
  - העובי הממוצע של המדידה

המסך מציג שני גופים זה לצד זה (ארבעה גרפים): כל סריקה מתעדכנת בתורה -
הסריקה הראשונה בגרפים של גוף 1, הבאה בגוף 2, וחוזר חלילה.

שום דבר לא נשמר לדיסק.

הרצה:  python sweep_range_analysis.py
"""

import threading
import time
import tkinter as tk
import traceback

import numpy as np
from PIL import Image, ImageTk
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.ticker import MultipleLocator

import thickness_Module_V3_w as thck  # מייבא גם matplotlib במצב Agg

UPDATE_MS = 50            # קצב רענון תצוגה
STALE_SEC = 1.0           # בלי דגימות חדשות מעבר לזה - "אין נתונים"
SWEEP_END_SEC = 0.3       # כמה זמן בלי דגימה תקפה נחשב "הסריקה הסתיימה"
MIN_SWEEP_SAMPLES = 100   # פחות מזה - לא סריקה אמיתית
EDGE_TRIM_FRAC = 0.10     # חיתוך שווה של 10% מכל קצה - מסלק את אירועי הכניסה/יציאה

GRAPH_SIZE = (820, 560)


# =============================================================================
# צבירת סריקה: כל עוד שני החיישנים רואים את החלק
# =============================================================================
class SweepCollector:
    """צובר (t, top, bottom, thickness) כל עוד שני החיישנים בטווח קריאה."""

    def __init__(self, cfg, state):
        self.cfg = cfg
        self.state = state
        self.lock = threading.Lock()
        self.finished_sweep = None    # רשימת (t, top, bot, thk) של סריקה שהסתיימה
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def poll_finished_sweep(self):
        with self.lock:
            s, self.finished_sweep = self.finished_sweep, None
        return s

    def run(self):
        last_seen_ts = {"TOP": 0.0, "BOTTOM": 0.0}
        in_sweep = False
        sweep_start = 0.0
        last_valid = 0.0
        acc = []

        while not self.state.stop_event.is_set():
            time.sleep(0.005)

            with self.state.lock:
                top = self.state.samples["TOP"][-1] if self.state.samples["TOP"] else None
                bot = self.state.samples["BOTTOM"][-1] if self.state.samples["BOTTOM"] else None
                ts = dict(self.state.last_ts)

            now = time.time()
            new_data = (ts != last_seen_ts)
            last_seen_ts = ts

            sample = None
            if new_data and top is not None and bot is not None:
                _, top_abs = top
                _, bot_abs = bot
                if (top_abs < self.cfg.ERROR_THRESHOLD_MM
                        and bot_abs < self.cfg.ERROR_THRESHOLD_MM):
                    raw = self.cfg.SENSOR_DISTANCE_MM - (top_abs + bot_abs)
                    sample = (top_abs, bot_abs, self.state.apply_calibration(raw))

            if sample is not None:
                if not in_sweep:
                    in_sweep = True
                    sweep_start = now
                    acc = []
                acc.append((now - sweep_start,) + sample)
                last_valid = now
            elif in_sweep and (now - last_valid) > SWEEP_END_SEC:
                in_sweep = False
                if len(acc) >= MIN_SWEEP_SAMPLES:
                    with self.lock:
                        self.finished_sweep = acc
                acc = []


# =============================================================================
# גרף: עובי + ערכי החיישנים, כפי שהם
# =============================================================================
def trim_edges(samples, frac=EDGE_TRIM_FRAC):
    """מתעלמים מאירועי הכניסה והיציאה: חיתוך שווה של frac מכל קצה."""
    k = int(len(samples) * frac)
    if k > 0 and len(samples) - 2 * k >= 10:
        return samples[k:-k]
    return samples


def render_sweep_plot(samples, size_px=GRAPH_SIZE):
    arr = np.asarray(samples, float)
    t, top, bot, thk = arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3]
    mean_thk = float(np.mean(thk))

    w_px, h_px = size_px
    fig = Figure(figsize=(w_px / 100.0, h_px / 100.0), dpi=100)
    # פאנל העובי מקבל פי 2 גובה - שם נדרשת רזולוציית המיקרון
    ax1, ax2 = fig.subplots(2, 1, sharex=True,
                            gridspec_kw={"height_ratios": [2, 1]})

    # העובי כפי שהוא, רק שציר ה-Y בסטייה במיקרונים מהממוצע -
    # כך מיקרון בודד הוא קו רשת קריא, לא תת-פיקסל
    dev_um = (thk - mean_thk) * 1000.0
    ax1.plot(t, dev_um, linewidth=0.8)
    ax1.axhline(0.0, linestyle="--", linewidth=0.9, color="red")
    ax1.set_ylabel("Thickness deviation [um]")
    ax1.set_title(f"Thickness (mean {mean_thk:.5f} mm)")
    span = max(2.0, 1.1 * float(np.max(np.abs(dev_um))))
    ax1.set_ylim(-span, +span)
    if span <= 40.0:
        # קו רשת על כל מיקרון בודד
        ax1.yaxis.set_minor_locator(MultipleLocator(1.0))
        ax1.grid(True, which="minor", alpha=0.15)
    ax1.grid(True, which="major", alpha=0.35)

    ax2.plot(t, top, linewidth=0.8, label="TOP [mm]")
    ax2.plot(t, bot, linewidth=0.8, label="BOTTOM [mm]")
    ax2.set_xlabel("Time from sweep start [s]")
    ax2.set_ylabel("Sensor distance [mm]")
    ax2.ticklabel_format(axis="y", useOffset=False)  # ערכים מלאים, בלי +3.03e1
    ax2.legend(loc="upper right", fontsize=8)
    ax2.grid(True, alpha=0.3)

    fig.tight_layout(pad=0.8)
    canvas = FigureCanvasAgg(fig)
    canvas.draw()
    actual = canvas.get_width_height()
    im = Image.frombuffer("RGBA", actual, canvas.buffer_rgba(), "raw", "RGBA", 0, 1)
    return im.convert("RGB"), mean_thk


# =============================================================================
# UI
# =============================================================================
class SweepRangeAnalysisApp:
    def __init__(self):
        cfg_dict = thck.load_config_json(
            str(thck.Path(__file__).parent / "config" / "config457_thk.json"))
        self.cfg = thck.ThicknessConfig(cfg_dict)
        self.state = thck.ThicknessRuntimeState(self.cfg)
        self.readers = [
            thck.SensorReader(self.cfg, self.state, name, info["port"])
            for name, info in self.cfg.SENSORS.items()
        ]
        self.collector = SweepCollector(self.cfg, self.state)
        self._last_change = 0.0
        self._last_ts_seen = {"TOP": 0.0, "BOTTOM": 0.0}

        self.root = tk.Tk()
        self.root.title("הקלטת סריקה - חיישני עובי")
        self.root.geometry("1800x950")
        self.root.configure(bg="#202020")

        tk.Label(self.root, bg="#202020", fg="#dddddd", font=("Arial", 18),
                 text="העבר את החלק על הטווח; בסוף הסריקה יוצגו הגרפים"
                 ).pack(pady=(15, 5))

        live_row = tk.Frame(self.root, bg="#202020")
        live_row.pack(fill="x", padx=30)
        self.live_lbls = {}
        for col, (key, title) in enumerate([("THK", "עובי"),
                                            ("TOP", "חיישן עליון"),
                                            ("BOTTOM", "חיישן תחתון")]):
            box = tk.Frame(live_row, bg="white", highlightbackground="#666666",
                           highlightthickness=3)
            box.grid(row=0, column=col, sticky="nsew", padx=8)
            live_row.grid_columnconfigure(col, weight=1)
            tk.Label(box, text=title, bg="white", fg="#555555",
                     font=("Arial", 14)).pack(pady=(6, 0))
            lbl = tk.Label(box, text="-.-----", bg="white", fg="#0a64a0",
                           font=("Consolas", 30, "bold"))
            lbl.pack(pady=(0, 6))
            self.live_lbls[key] = lbl

        # שני גופים, זה לצד זה - כל סריקה מתעדכנת בתורה: גוף 1, גוף 2, גוף 1...
        bodies_row = tk.Frame(self.root, bg="#202020")
        bodies_row.pack(fill="both", expand=True, padx=20, pady=(12, 8))

        self.graph_lbls = []
        self.result_lbls = []
        self._graph_imgs = [None, None]   # reference כדי ש-tk לא ישחרר את התמונות
        for slot in range(2):
            box = tk.Frame(bodies_row, bg="white",
                           highlightbackground="#666666", highlightthickness=4)
            box.grid(row=0, column=slot, sticky="nsew",
                     padx=(0, 10) if slot == 0 else (10, 0))
            bodies_row.grid_columnconfigure(slot, weight=1)

            tk.Label(box, text=f"גוף {slot + 1}", bg="white", fg="#555555",
                     font=("Arial", 20, "bold")).pack(pady=(8, 0))
            g = tk.Label(box, text="ממתין לסריקה...", bg="white", fg="#aaaaaa",
                         font=("Arial", 22), height=12)
            g.pack(pady=4)
            r = tk.Label(box, text="", bg="white", fg="#1a7a1a",
                         font=("Arial", 14, "bold"))
            r.pack(pady=(0, 8))
            self.graph_lbls.append(g)
            self.result_lbls.append(r)

        self._next_slot = 0               # לאיזה גוף שייכת הסריקה הבאה
        self._graph_lock = threading.Lock()
        self._pending = None   # (slot, PIL.Image, result_text)

        self.root.protocol("WM_DELETE_WINDOW", self.close)

    # --- ערכים חיים ---
    def read_live(self):
        with self.state.lock:
            top = self.state.samples["TOP"][-1] if self.state.samples["TOP"] else None
            bot = self.state.samples["BOTTOM"][-1] if self.state.samples["BOTTOM"] else None
            ts = dict(self.state.last_ts)
        now = time.time()
        if ts != self._last_ts_seen:
            self._last_ts_seen = ts
            self._last_change = now
        if top is None or bot is None or (now - self._last_change) > STALE_SEC:
            return None, None, None
        _, top_abs = top
        _, bot_abs = bot
        if top_abs >= self.cfg.ERROR_THRESHOLD_MM or bot_abs >= self.cfg.ERROR_THRESHOLD_MM:
            return None, top_abs, bot_abs
        raw = self.cfg.SENSOR_DISTANCE_MM - (top_abs + bot_abs)
        return self.state.apply_calibration(raw), top_abs, bot_abs

    def _render_bg(self, samples, slot):
        try:
            samples = trim_edges(samples)   # בלי הכניסה והיציאה
            img, mean_thk = render_sweep_plot(samples)
            dur = samples[-1][0] - samples[0][0]
            txt = (f'עובי ממוצע: {mean_thk:.5f} מ"מ | '
                   f'{len(samples)} דגימות (אחרי חיתוך 10% מכל קצה) | {dur:.1f} שניות')
            with self._graph_lock:
                self._pending = (slot, img, txt)
        except Exception:
            traceback.print_exc()
            with self._graph_lock:
                self._pending = (slot, None, "שגיאה בציור הגרף - ראה קונסול")

    def update_display(self):
        try:
            thk, top_abs, bot_abs = self.read_live()
            self.live_lbls["THK"].config(
                text=f"{thk:.5f}" if thk is not None else "—")
            for key, dist in (("TOP", top_abs), ("BOTTOM", bot_abs)):
                ok = dist is not None and dist < self.cfg.ERROR_THRESHOLD_MM
                self.live_lbls[key].config(text=f"{dist:.5f}" if ok else "—")

            sweep = self.collector.poll_finished_sweep()
            if sweep is not None:
                slot = self._next_slot
                self._next_slot = (self._next_slot + 1) % 2
                threading.Thread(target=self._render_bg, args=(sweep, slot),
                                 daemon=True).start()

            with self._graph_lock:
                pending, self._pending = self._pending, None
            if pending is not None:
                slot, img, txt = pending
                if img is not None:
                    self._graph_imgs[slot] = ImageTk.PhotoImage(img)
                    self.graph_lbls[slot].config(image=self._graph_imgs[slot],
                                                 text="", height=img.height)
                self.result_lbls[slot].config(text=txt)
        except Exception:
            traceback.print_exc()
        finally:
            self.root.after(UPDATE_MS, self.update_display)

    def run(self):
        for r in self.readers:
            r.start()
        self.collector.start()
        self.update_display()
        self.root.mainloop()

    def close(self):
        self.state.stop_event.set()
        self.root.destroy()


if __name__ == "__main__":
    SweepRangeAnalysisApp().run()
