"""
simple_thickness_display.py
===========================
תוכנית פשוטה: מציגה מלבן גדול עם ערך העובי בזמן אמת.

- משתמשת במודול העובי (thickness_Module_V3_w) לקליטת הנתונים מהחיישנים:
  אותם SensorReader-ים, אותו קונפיג (config/config457_thk.json), אותו כיול.
- לא מריצה את ThicknessProcessor (אין זיהוי חלק/פסילה) - רק קוראת כל הזמן
  את הדגימה האחרונה של כל חיישן ומחשבת:
      thickness = sensor_distance_mm - (top_abs + bottom_abs)
  עם הכיול הליניארי (a, b) מהקונפיג.
- הערך מוצג במ"מ עם 5 ספרות אחרי הנקודה, ומתעדכן כל הזמן שהקוד רץ.
- מתחת למלבן הראשי: שני מלבנים קטנים יותר עם המרחק שכל חיישן מודד
  (TOP / BOTTOM, ערך אבסולוטי = reference_height_mm + הקריאה מהחיישן).
- גרף עובי למופע: כשנכנס עצם (עובי בטווח התקין) מתחילים לצבור דגימות,
  וכשהוא יוצא הגרף של המופע מוצג בתחתית החלון.

הרצה:  python simple_thickness_display.py
"""

import threading
import time
import tkinter as tk
import traceback

from PIL import Image, ImageTk
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg

import thickness_Module_V3_w as thck  # מייבא גם matplotlib במצב Agg

UPDATE_MS = 50          # קצב רענון תצוגה (20Hz)
STALE_SEC = 1.0         # אם אין דגימות חדשות מעבר לזה - "אין נתונים"
PASS_END_SEC = 0.2      # כמה זמן בלי דגימה תקינה נחשב "העצם יצא"
MIN_PASS_SAMPLES = 5    # פחות מזה - לא מופע אמיתי, לא מציירים
GRAPH_SIZE = (820, 300)  # גודל תמונת הגרף בפיקסלים


def render_pass_plot(t, y, size_px=GRAPH_SIZE):
    """מצייר את גרף המופע לתמונת PIL. רנדרר מקומי ובטוח-גודל:
    figsize*dpi עלול להתעגל פיקסל למטה (820 -> 819), לכן משתמשים
    בגודל האמיתי של ה-canvas ולא בגודל המבוקש."""
    w_px, h_px = size_px
    fig = Figure(figsize=(w_px / 100.0, h_px / 100.0), dpi=100)
    ax = fig.add_subplot(111)
    ax.plot(t, y, marker="o", markersize=2, linewidth=1.2)
    ax.set_xlabel("Time [s]")
    ax.set_ylabel("Thickness [mm]")
    ax.grid(True, alpha=0.3)
    fig.tight_layout(pad=0.6)
    canvas = FigureCanvasAgg(fig)
    canvas.draw()
    actual_size = canvas.get_width_height()
    im = Image.frombuffer("RGBA", actual_size, canvas.buffer_rgba(), "raw", "RGBA", 0, 1)
    return im.convert("RGB")


class PassCollector:
    """צובר דגימות עובי לאורך מופע (עצם נכנס -> יוצא), ברקע ובקצב גבוה.

    מופע מתחיל כשמתקבל עובי בטווח [THICKNESS_MIN, THICKNESS_MAX],
    ומסתיים אחרי PASS_END_SEC בלי אף דגימה תקינה.
    """

    def __init__(self, cfg, state):
        self.cfg = cfg
        self.state = state
        self.lock = threading.Lock()
        self.finished_pass = None       # (t_list, y_list) של המופע האחרון שהסתיים
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def poll_finished_pass(self):
        """מחזיר (t, y) של מופע שהסתיים מאז הקריאה הקודמת, או None."""
        with self.lock:
            p, self.finished_pass = self.finished_pass, None
        return p

    def run(self):
        last_seen_ts = {"TOP": 0.0, "BOTTOM": 0.0}
        in_pass = False
        pass_start = 0.0
        last_valid = 0.0
        acc_t, acc_y = [], []

        while not self.state.stop_event.is_set():
            time.sleep(0.005)

            with self.state.lock:
                top = self.state.samples["TOP"][-1] if self.state.samples["TOP"] else None
                bot = self.state.samples["BOTTOM"][-1] if self.state.samples["BOTTOM"] else None
                ts = dict(self.state.last_ts)

            now = time.time()
            new_data = (ts != last_seen_ts)
            last_seen_ts = ts

            thickness = None
            if new_data and top is not None and bot is not None:
                _, top_abs = top
                _, bot_abs = bot
                if (top_abs < self.cfg.ERROR_THRESHOLD_MM
                        and bot_abs < self.cfg.ERROR_THRESHOLD_MM):
                    raw = self.cfg.SENSOR_DISTANCE_MM - (top_abs + bot_abs)
                    thk = self.state.apply_calibration(raw)
                    if self.cfg.THICKNESS_MIN <= thk <= self.cfg.THICKNESS_MAX:
                        thickness = thk

            if thickness is not None:
                if not in_pass:
                    in_pass = True
                    pass_start = now
                    acc_t, acc_y = [], []
                acc_t.append(now - pass_start)
                acc_y.append(thickness)
                last_valid = now
            elif in_pass and (now - last_valid) > PASS_END_SEC:
                in_pass = False
                if len(acc_y) >= MIN_PASS_SAMPLES:
                    with self.lock:
                        self.finished_pass = (acc_t, acc_y)
                acc_t, acc_y = [], []


class SimpleThicknessDisplay:
    def __init__(self):
        # --- מנוע המדידה מתוך מודול העובי (ללא ה-Processor) ---
        cfg_dict = thck.load_config_json(
            str(thck.Path(__file__).parent / "config" / "config457_thk.json")
        )
        self.cfg = thck.ThicknessConfig(cfg_dict)
        self.state = thck.ThicknessRuntimeState(self.cfg)
        self.readers = [
            thck.SensorReader(self.cfg, self.state, name, info["port"])
            for name, info in self.cfg.SENSORS.items()
        ]
        self.collector = PassCollector(self.cfg, self.state)
        self._last_change = 0.0
        self._last_ts_seen = {"TOP": 0.0, "BOTTOM": 0.0}

        # --- UI: חלון עם מלבן גדול וערך במרכזו ---
        self.root = tk.Tk()
        self.root.title("עובי בזמן אמת")
        self.root.geometry("900x880")
        self.root.configure(bg="#202020")

        box = tk.Frame(self.root, bg="white", highlightbackground="#0a64a0",
                       highlightthickness=8)
        box.pack(expand=True, fill="both", padx=30, pady=(30, 10))

        self.value_lbl = tk.Label(box, text="-.-----", bg="white", fg="#0a64a0",
                                  font=("Consolas", 110, "bold"))
        self.value_lbl.pack(expand=True)

        self.unit_lbl = tk.Label(box, text='מ"מ', bg="white", fg="#555555",
                                 font=("Arial", 28))
        self.unit_lbl.pack(pady=(0, 20))

        # --- שני מלבנים קטנים יותר: המרחק שכל חיישן מודד ---
        sensors_row = tk.Frame(self.root, bg="#202020")
        sensors_row.pack(fill="both", padx=30, pady=(0, 10))

        self.sensor_lbls = {}
        for col, (name, title) in enumerate([("TOP", "חיישן עליון"),
                                             ("BOTTOM", "חיישן תחתון")]):
            sbox = tk.Frame(sensors_row, bg="white",
                            highlightbackground="#666666", highlightthickness=4)
            sbox.grid(row=0, column=col, sticky="nsew",
                      padx=(0, 15) if col == 0 else (15, 0))
            sensors_row.grid_columnconfigure(col, weight=1)

            tk.Label(sbox, text=title, bg="white", fg="#555555",
                     font=("Arial", 20)).pack(pady=(10, 0))
            lbl = tk.Label(sbox, text="-.-----", bg="white", fg="#333333",
                           font=("Consolas", 48, "bold"))
            lbl.pack(pady=(0, 10))
            self.sensor_lbls[name] = lbl

        # --- גרף עובי של המופע האחרון ---
        graph_box = tk.Frame(self.root, bg="white",
                             highlightbackground="#666666", highlightthickness=4)
        graph_box.pack(fill="both", padx=30, pady=(0, 30))

        tk.Label(graph_box, text="גרף עובי - מופע אחרון", bg="white",
                 fg="#555555", font=("Arial", 20)).pack(pady=(10, 0))
        self.graph_lbl = tk.Label(graph_box, text="ממתין למופע ראשון...",
                                  bg="white", fg="#aaaaaa", font=("Arial", 24),
                                  height=8)
        self.graph_lbl.pack(pady=(5, 0))
        self.graph_info_lbl = tk.Label(graph_box, text="", bg="white",
                                       fg="#333333", font=("Arial", 16))
        self.graph_info_lbl.pack(pady=(0, 10))
        self._graph_img = None  # שומר reference כדי ש-tk לא ישחרר את התמונה
        self._graph_lock = threading.Lock()
        self._pending_graph = None  # (PIL.Image, info_text) ממתין להצגה

        self.root.protocol("WM_DELETE_WINDOW", self.close)

    # --- ציור הגרף רץ ב-thread נפרד כדי לא לתקוע את ה-UI ---
    def _render_pass_bg(self, t, y):
        try:
            img = render_pass_plot(t, y)
            mean = sum(y) / len(y)
            dur = t[-1] if t else 0.0
            info = f'ממוצע: {mean:.5f} מ"מ   |   {len(y)} דגימות   |   {dur:.2f} שניות'
            with self._graph_lock:
                self._pending_graph = (img, info)
        except Exception:
            traceback.print_exc()
            with self._graph_lock:
                self._pending_graph = (None, "שגיאה בציור הגרף - ראה קונסול")

    def _show_pending_graph(self):
        with self._graph_lock:
            pending, self._pending_graph = self._pending_graph, None
        if pending is None:
            return
        img, info = pending
        if img is not None:
            # PhotoImage חייב להיווצר ב-thread הראשי של tk
            self._graph_img = ImageTk.PhotoImage(img)
            self.graph_lbl.config(image=self._graph_img, text="", height=img.height)
        self.graph_info_lbl.config(text=info)

    # --- קריאת הדגימה האחרונה מכל חיישן וחישוב עובי ---
    # מחזיר (עובי, הודעה, מרחק עליון, מרחק תחתון); מרחק None = אין נתונים מהחיישן
    def read_thickness_mm(self):
        with self.state.lock:
            top = self.state.samples["TOP"][-1] if self.state.samples["TOP"] else None
            bot = self.state.samples["BOTTOM"][-1] if self.state.samples["BOTTOM"] else None
            ts = dict(self.state.last_ts)

        now = time.time()
        if ts != self._last_ts_seen:
            self._last_ts_seen = ts
            self._last_change = now

        if top is None or bot is None or (now - self._last_change) > STALE_SEC:
            return None, "אין נתונים", None, None

        _, top_abs = top
        _, bot_abs = bot
        if top_abs >= self.cfg.ERROR_THRESHOLD_MM or bot_abs >= self.cfg.ERROR_THRESHOLD_MM:
            return None, "אין חלק", top_abs, bot_abs

        raw = self.cfg.SENSOR_DISTANCE_MM - (top_abs + bot_abs)
        return self.state.apply_calibration(raw), None, top_abs, bot_abs

    def update_display(self):
        # כל הגוף בתוך try/finally: שום חריגה לא תהרוג את לולאת הרענון
        try:
            thickness, msg, top_abs, bot_abs = self.read_thickness_mm()
            if thickness is not None:
                self.value_lbl.config(text=f"{thickness:.5f}", fg="#0a64a0")
            else:
                self.value_lbl.config(text=msg, fg="#aaaaaa")

            for name, dist in (("TOP", top_abs), ("BOTTOM", bot_abs)):
                if dist is not None and dist < self.cfg.ERROR_THRESHOLD_MM:
                    self.sensor_lbls[name].config(text=f"{dist:.5f}", fg="#333333")
                else:
                    self.sensor_lbls[name].config(
                        text="אין נתונים" if dist is None else "מחוץ לטווח",
                        fg="#aaaaaa")

            finished = self.collector.poll_finished_pass()
            if finished is not None:
                t, y = finished
                threading.Thread(target=self._render_pass_bg, args=(t, y),
                                 daemon=True).start()

            self._show_pending_graph()
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
    SimpleThicknessDisplay().run()
