
import os
import time
import tkinter as tk

import thickness_Module_457 as thck

UPDATE_MS = 50          # קצב רענון תצוגה (20Hz)
STALE_SEC = 1.0         # אם אין דגימות חדשות מעבר לזה - "אין נתונים"


class SimpleThicknessDisplay:
    def __init__(self):
        # --- מנוע המדידה: בונים קונפיג מפורשות מאותו קובץ שהמנוע הראשי משתמש בו ---
        # (במודול הלינאריזציה אין Cfg גלובלי, וה-state/החיישנים דורשים cfg)
        cfg_path = os.path.join(os.path.dirname(thck.__file__),
                                "config", "config457_thk.json")
        self.cfg = thck.ThicknessConfig(thck.load_config_json(cfg_path))
        self.state = thck.ThicknessRuntimeState(self.cfg)
        self.readers = [
            thck.SensorReader(self.cfg, self.state, name, info["port"])
            for name, info in self.cfg.SENSORS.items()
        ]
        self._last_change = 0.0
        self._last_ts_seen = {"TOP": 0.0, "BOTTOM": 0.0}

        # --- UI: חלון עם מלבן גדול וערך במרכזו ---
        self.root = tk.Tk()
        self.root.title("Real Time Measurement")
        self.root.geometry("900x420")
        self.root.configure(bg="#202020")

        box = tk.Frame(self.root, bg="white", highlightbackground="#0a64a0",
                       highlightthickness=8)
        box.pack(expand=True, fill="both", padx=30, pady=(30, 10))

        self.value_lbl = tk.Label(box, text="-.-----", bg="white", fg="#0a64a0",
                                  font=("Consolas", 110, "bold"))
        self.value_lbl.pack(expand=True)

        self.unit_lbl = tk.Label(box, text='mm', bg="white", fg="#555555",
                                 font=("Arial", 28))
        self.unit_lbl.pack(pady=(0, 20))

        # --- שני מלבנים קטנים יותר: המרחק שכל חיישן מודד ---
        sensors_row = tk.Frame(self.root, bg="#202020")
        sensors_row.pack(fill="both", padx=30, pady=(0, 30))

        self.sensor_lbls = {}
        for col, (name, title) in enumerate([("TOP", "Top sensor"),
                                             ("BOTTOM", "Bottom sensor"),]):
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

        self.root.protocol("WM_DELETE_WINDOW", self.close)

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
            return None, "No Data", None, None

        _, top_abs = top
        _, bot_abs = bot
        if top_abs >= self.cfg.ERROR_THRESHOLD_MM or bot_abs >= self.cfg.ERROR_THRESHOLD_MM:
            return None, "No part", top_abs, bot_abs

        # לינאריזציה: thickness = a*(top+bottom) + b ; a,b מגיעים מבלוק calibration בקונפיג.
        # apply_calibration מחזיר None כשאין כיול תקף (calibration.valid=false / חסרים a,b).
        sensors_sum_mm = top_abs + bot_abs
        thickness = self.state.apply_calibration(sensors_sum_mm)
        if thickness is None:
            return None, "No Calib", top_abs, bot_abs
        return thickness, None, top_abs, bot_abs

    def update_display(self):
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
                    text="No Data" if dist is None else "Out of Range",
                    fg="#aaaaaa")

        self.root.after(UPDATE_MS, self.update_display)

    def run(self):
        for r in self.readers:
            r.start()
        self.update_display()
        self.root.mainloop()

    def close(self):
        self.state.stop_event.set()
        self.root.destroy()


if __name__ == "__main__":
    SimpleThicknessDisplay().run()
