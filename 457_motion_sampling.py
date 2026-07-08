# -*- coding: utf-8 -*-
"""
457 — Standalone Motion & Sampling
==================================
תוכנית פשוטה ועצמאית שמבצעת *רק* את תהליך התנועה והדגימה של החיישנים
לפי ה-POS שמופיעים בקוד המקורי (Main_prog), בדיוק אותו תהליך —
בלי כיול, בלי לינאריזציה, בלי חישוב ובלי שמירה לקובץ.

בלחיצה על "התחל":
  1. מזיז את מנוע M6 ל-POS 5  (מדיד 1)  וממתין להגעה
  2. דוגם את החיישנים ומציג את הקריאות על המסך
  3. מזיז ל-POS 6  (מדיד 2)  וממתין להגעה
  4. דוגם את החיישנים ומציג את הקריאות
  5. מחזיר את M6 ל-POS 1 (מיקום מדידה)

השליטה במנוע והתקשורת מול הבקר זהות למקור:
  * RegsToPlc[6] = מספר POS  ->  "M6 Go position N"     (כתיבה ל-PLC)
  * RegsFromPlc[24] = סטטוס M6  (10=מוכן, 7/8=בתנועה)   (קריאה מה-PLC)
  * RegsFromPlc[25] = מיקום בפועל (encoder)             (אבחון בלבד)
  * thread יחיד כותב 20 רגיסטרים / קורא 40 (כמו T4_plc_flush)
  * חיישני עובי TOP/BOTTOM משדרים UDP (כמו thickness_Module)
"""

import os
import json
import time
import socket
import struct
import threading
import tkinter as tk
from tkinter import ttk

from pyModbusTCP.client import ModbusClient


# ----------------------------------------------------------------------
# קבועים — זהים לקוד המקורי (Main_prog)
# ----------------------------------------------------------------------
PLC_HOST = "192.168.3.169"
PLC_PORT = 502
LIVE_SIGN_SEC = 2.0          # אות-חיים ל-PLC כשאין מה לשלוח

# מפת רגיסטרים של M6 (המנוע האופקי שנושא את החיישנים)
M6_CMD_REG = 6               # PC->PLC : כתיבת מספר POS  ("M6 Go position N")
M6_STATUS_REG = 24           # PLC->PC : סטטוס M6
M6_ACTPOS_REG = 25           # PLC->PC : מיקום בפועל (אבחון)
M6_STATUS_READY = 10         # "מוכן" / הגיע ליעד
M6_STATUS_MOVING = (7, 8)    # נע קדימה / אחורה
M6_MOVE_TIMEOUT_SEC = 30.0

# ה-POS מהקוד
POS_GAUGE_1 = 5              # מדיד 1
POS_GAUGE_2 = 6             # מדיד 2
POS_MEASURE = 1             # מיקום מדידה (חזרה)

# הפסקות
SETTLE_SEC = 0.5            # השהיה קצרה אחרי הגעה, לפני הדגימה
DEFAULT_SAMPLE_SEC = 3.0   # משך חלון הדגימה בכל מיקום


# ----------------------------------------------------------------------
# מצב תקשורת גלובלי (כמו במקור)
# ----------------------------------------------------------------------
RegsToPlc = [0] * 20
RegsFromPlc = [0] * 40
RegsLock = threading.Lock()
stop_event = threading.Event()   # סוגר את כל ה-threads ביציאה


# ----------------------------------------------------------------------
# קונפיגורציית חיישנים — נטענת מ-config457_thk.json (עם ברירת מחדל)
# ----------------------------------------------------------------------
def load_sensor_config():
    defaults = {
        "TOP_port": 5010,
        "BOTTOM_port": 5009,
        "reference_height_mm": 30.0,
        "sensor_distance_mm": 62.015,
    }
    path = os.path.join("config", "config457_thk.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        sensors = d.get("sensors", {})
        return {
            "TOP_port": int(sensors.get("TOP", {}).get("port", defaults["TOP_port"])),
            "BOTTOM_port": int(sensors.get("BOTTOM", {}).get("port", defaults["BOTTOM_port"])),
            "reference_height_mm": float(d.get("reference_height_mm", defaults["reference_height_mm"])),
            "sensor_distance_mm": float(d.get("sensor_distance_mm", defaults["sensor_distance_mm"])),
        }
    except Exception:
        return defaults


SCFG = load_sensor_config()


# ----------------------------------------------------------------------
# פענוח מנת UDP מהחיישן (זהה ל-thickness_Module.parse_push_packet)
# ----------------------------------------------------------------------
def parse_push_packet(data: bytes):
    if len(data) < 18:
        return None
    payload = data[14:]
    if len(payload) < 4 or (len(payload) % 4) != 0:
        return None
    total = len(payload) // 4
    vals_nm = struct.unpack(">" + "i" * total, payload)
    return [v / 1_000_000.0 for v in vals_nm]  # mm


# ----------------------------------------------------------------------
# מצב דגימה חי של החיישנים
# ----------------------------------------------------------------------
class SensorState:
    def __init__(self):
        self.lock = threading.Lock()
        self.top_mm = None       # קריאה אחרונה (mm מוחלט)
        self.bot_mm = None
        self.top_count = 0       # מונה דגימות בחלון הנוכחי
        self.bot_count = 0

    def reset_counts(self):
        with self.lock:
            self.top_count = 0
            self.bot_count = 0

    def snapshot(self):
        with self.lock:
            return self.top_mm, self.bot_mm, self.top_count, self.bot_count


def sensor_reader(state: SensorState, name: str, port: int, ref_mm: float):
    """קורא חבילות UDP מחיישן בודד ושומר את הקריאה האחרונה (mm מוחלט)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("", port))
        sock.settimeout(0.2)
    except Exception as e:
        print(f"[{name}] cannot open UDP {port}: {e}")
        return

    while not stop_event.is_set():
        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        vals = parse_push_packet(data)
        if not vals:
            continue
        abs_mm = ref_mm + vals[-1]   # הקריאה העדכנית ביותר במנה
        with state.lock:
            if name == "TOP":
                state.top_mm = abs_mm
                state.top_count += len(vals)
            else:
                state.bot_mm = abs_mm
                state.bot_count += len(vals)

    try:
        sock.close()
    except Exception:
        pass


# ----------------------------------------------------------------------
# Thread תקשורת יחיד מול ה-PLC (על בסיס T4_plc_flush)
# כותב 20 רגיסטרים כשיש משהו לשלוח, קורא 40 בכל סבב כדי לשמור סטטוס עדכני.
# ----------------------------------------------------------------------
def plc_thread(app):
    global RegsToPlc
    client = ModbusClient(
        host=PLC_HOST,
        port=PLC_PORT,
        auto_open=True,
        auto_close=False,
        timeout=0.3,
    )
    last_write = time.time() - 10.0

    while not stop_event.is_set():
        try:
            to_send = None
            with RegsLock:
                if any(v > 0 for v in RegsToPlc):
                    to_send = RegsToPlc.copy()
                    RegsToPlc = [0] * 20
            # אות-חיים כשאין מה לשלוח
            if to_send is None and (time.time() - last_write) > LIVE_SIGN_SEC:
                to_send = [0] * 20
                to_send[0] = 1

            if to_send is not None:
                ok = client.write_multiple_registers(0, to_send)
                if ok:
                    last_write = time.time()
                else:
                    app.set_plc(False)

            # קריאת 40 רגיסטרים בכל סבב -> סטטוס M6 נשאר עדכני ותגובתי
            read_40 = client.read_holding_registers(0, 40)
            if read_40 is not None and len(read_40) >= 40:
                RegsFromPlc[:] = list(read_40)[:40]
                app.set_plc(True)
            else:
                app.set_plc(False)
        except Exception as e:
            app.set_plc(False)
            try:
                client.close()
            except Exception:
                pass
            time.sleep(0.2)

        time.sleep(0.1)


# ----------------------------------------------------------------------
# פרימיטיב תנועה — זהה ל-_go_and_wait במקור
# ----------------------------------------------------------------------
def go_and_wait(pos, label, app):
    """שולח 'M6 -> Go position N' וממתין להגעה (סטטוס=10 אחרי שנראתה תנועה)."""
    with RegsLock:
        RegsToPlc[M6_CMD_REG] = pos
    app.log(f"→ M6 ל-POS {pos} ({label})")
    t0 = time.time()
    started = False
    last_log = 0.0
    while time.time() - t0 < M6_MOVE_TIMEOUT_SEC:
        if stop_event.is_set() or app.abort.is_set():
            return False
        st = RegsFromPlc[M6_STATUS_REG]
        actpos = RegsFromPlc[M6_ACTPOS_REG]
        app.set_m6(st, actpos)
        if st in M6_STATUS_MOVING:
            started = True
        if time.time() - last_log >= 1.0:
            last_log = time.time()
            app.log(f"   ...ממתין POS {pos} — status={st} actpos={actpos} started={started}")
        # הגיע: PLC מדווח 'מוכן' אחרי שנראתה תנועה; fallback של 5 שניות
        # מכסה מקרה שבו M6 כבר היה ביעד (ללא תנועה).
        if st == M6_STATUS_READY and (started or time.time() - t0 >= 5.0):
            app.log(f"✓ M6 הגיע ל-POS {pos} (actpos={actpos})")
            return True
        time.sleep(0.05)
    app.log(f"✗ TIMEOUT — M6 לא הגיע ל-POS {pos} (status={RegsFromPlc[M6_STATUS_REG]})")
    return False


# ----------------------------------------------------------------------
# חלון דגימה — דוגם ומציג את קריאות החיישנים (ללא חישוב/שמירה)
# ----------------------------------------------------------------------
def sample_window(state: SensorState, app, seconds, label):
    app.set_phase(f"דוגם — {label}")
    app.log(f"● דגימה במשך {seconds:.1f} שנ' ({label})")
    state.reset_counts()
    t0 = time.time()
    while time.time() - t0 < seconds:
        if stop_event.is_set() or app.abort.is_set():
            return
        top, bot, tc, bc = state.snapshot()
        # עובי רגעי = מרחק בין החיישנים פחות שתי הקריאות (קריאת חיישן ישירה,
        # בלי כיול/לינאריזציה). מוצג לנוחות בלבד.
        thick = None
        if top is not None and bot is not None:
            thick = SCFG["sensor_distance_mm"] - (top + bot)
        app.set_live(top, bot, thick, tc, bc, time.time() - t0)
        time.sleep(0.05)
    top, bot, tc, bc = state.snapshot()
    app.log(f"   סיום דגימה: TOP={tc} דגימות, BOTTOM={bc} דגימות")


# ----------------------------------------------------------------------
# רצף התהליך המלא (בדיוק כמו במקור, בלי הכיול)
# ----------------------------------------------------------------------
def run_sequence(state: SensorState, app):
    app.abort.clear()
    try:
        for pos, label in ((POS_GAUGE_1, "מדיד 1 (POS 5)"),
                           (POS_GAUGE_2, "מדיד 2 (POS 6)")):
            if app.abort.is_set():
                break
            app.set_phase(f"נע אל {label}")
            go_and_wait(pos, label, app)
            if app.abort.is_set():
                break
            time.sleep(SETTLE_SEC)        # הפסקת התייצבות
            sample_window(state, app, app.sample_seconds(), label)

        # תמיד מחזיר את M6 למיקום המדידה (POS 1)
        if not app.abort.is_set():
            app.set_phase("חוזר למיקום מדידה (POS 1)")
            go_and_wait(POS_MEASURE, "מדידה (POS 1)", app)

        app.set_phase("הופסק" if app.abort.is_set() else "הסתיים ✓")
        app.log("— התהליך הופסק —" if app.abort.is_set() else "— התהליך הסתיים —")
    finally:
        app.enable_start()


# ----------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self, state: SensorState):
        super().__init__()
        self.state = state
        self.abort = threading.Event()
        self._worker = None

        # מצב משותף בין ה-worker ל-UI (מוגן ב-after refresh)
        self._ui_lock = threading.Lock()
        self._plc_ok = False
        self._m6 = (0, 0)
        self._phase = "מוכן"
        self._live = (None, None, None, 0, 0, 0.0)
        self._log_lines = []

        self.title("457 — תנועה ודגימה (Standalone)")
        self.geometry("640x620")
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._refresh)

    # ---- בנייה ----
    def _build_ui(self):
        pad = dict(padx=10, pady=6)

        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="תהליך תנועה ודגימה 457", font=("Arial", 16, "bold")).pack(side="left")
        self.plc_lbl = ttk.Label(top, text="PLC: —", font=("Arial", 11, "bold"))
        self.plc_lbl.pack(side="right")

        # פקדים
        ctl = ttk.Frame(self, padding=(10, 0))
        ctl.pack(fill="x")
        ttk.Label(ctl, text="משך דגימה [שנ']:").pack(side="left")
        self.sample_var = tk.StringVar(value=str(DEFAULT_SAMPLE_SEC))
        ttk.Entry(ctl, textvariable=self.sample_var, width=6).pack(side="left", padx=(4, 12))
        self.start_btn = ttk.Button(ctl, text="▶ התחל", command=self._on_start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(ctl, text="■ עצור", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        # שלב נוכחי
        phase_box = ttk.LabelFrame(self, text="שלב", padding=10)
        phase_box.pack(fill="x", **pad)
        self.phase_lbl = ttk.Label(phase_box, text="מוכן", font=("Arial", 15, "bold"), foreground="#0a4")
        self.phase_lbl.pack(anchor="w")
        self.m6_lbl = ttk.Label(phase_box, text="M6 status=—  actpos=—", font=("Consolas", 11))
        self.m6_lbl.pack(anchor="w", pady=(4, 0))

        # קריאות חיים
        live = ttk.LabelFrame(self, text="דגימת חיישנים (חי)", padding=10)
        live.pack(fill="x", **pad)
        self.v_top = self._live_row(live, 0, "TOP  [mm]")
        self.v_bot = self._live_row(live, 1, "BOTTOM  [mm]")
        self.v_thk = self._live_row(live, 2, "עובי רגעי  [mm]")
        self.v_cnt = self._live_row(live, 3, "דגימות (TOP / BOTTOM)")
        self.v_elp = self._live_row(live, 4, "זמן בחלון  [שנ']")
        live.grid_columnconfigure(1, weight=1)

        # לוג
        logf = ttk.LabelFrame(self, text="יומן", padding=6)
        logf.pack(fill="both", expand=True, **pad)
        self.log_txt = tk.Text(logf, height=10, font=("Consolas", 9), state="disabled", wrap="none")
        self.log_txt.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(logf, orient="vertical", command=self.log_txt.yview)
        sb.pack(side="right", fill="y")
        self.log_txt.configure(yscrollcommand=sb.set)

    def _live_row(self, parent, r, title):
        ttk.Label(parent, text=title + ":", width=22, anchor="w").grid(row=r, column=0, sticky="w", pady=3)
        var = tk.StringVar(value="—")
        ttk.Label(parent, textvariable=var, font=("Consolas", 14, "bold")).grid(row=r, column=1, sticky="w", pady=3)
        return var

    # ---- קריאות מה-worker (thread-safe) ----
    def set_plc(self, ok):
        with self._ui_lock:
            self._plc_ok = bool(ok)

    def set_m6(self, status, actpos):
        with self._ui_lock:
            self._m6 = (status, actpos)

    def set_phase(self, text):
        with self._ui_lock:
            self._phase = text

    def set_live(self, top, bot, thick, tc, bc, elapsed):
        with self._ui_lock:
            self._live = (top, bot, thick, tc, bc, elapsed)

    def log(self, msg):
        stamp = time.strftime("%H:%M:%S")
        with self._ui_lock:
            self._log_lines.append(f"{stamp}  {msg}")

    def sample_seconds(self):
        try:
            return max(0.2, float(self.sample_var.get()))
        except ValueError:
            return DEFAULT_SAMPLE_SEC

    def enable_start(self):
        # נקרא מ-thread; מתוזמן ל-UI
        self.after(0, self._set_running, False)

    # ---- כפתורים ----
    def _on_start(self):
        if self._worker is not None and self._worker.is_alive():
            return
        self._set_running(True)
        self._worker = threading.Thread(target=run_sequence, args=(self.state, self), daemon=True)
        self._worker.start()

    def _on_stop(self):
        self.abort.set()
        self.log("בקשת עצירה מהמשתמש…")

    def _set_running(self, running):
        self.start_btn.config(state="disabled" if running else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")
        if not running:
            self.abort.clear()

    # ---- ריענון תצוגה ----
    def _refresh(self):
        with self._ui_lock:
            plc_ok = self._plc_ok
            m6 = self._m6
            phase = self._phase
            live = self._live
            new_lines = self._log_lines
            self._log_lines = []

        self.plc_lbl.config(
            text="PLC: OnLine" if plc_ok else "PLC: OffLine",
            foreground="#0a4" if plc_ok else "#c00",
        )
        self.phase_lbl.config(text=phase)
        self.m6_lbl.config(text=f"M6 status={m6[0]}  actpos={m6[1]}")

        top, bot, thk, tc, bc, elp = live
        self.v_top.set(f"{top:.4f}" if top is not None else "—")
        self.v_bot.set(f"{bot:.4f}" if bot is not None else "—")
        self.v_thk.set(f"{thk:.4f}" if thk is not None else "—")
        self.v_cnt.set(f"{tc} / {bc}")
        self.v_elp.set(f"{elp:.1f}")

        if new_lines:
            self.log_txt.config(state="normal")
            for ln in new_lines:
                self.log_txt.insert("end", ln + "\n")
            self.log_txt.see("end")
            self.log_txt.config(state="disabled")

        self.after(100, self._refresh)

    def _on_close(self):
        self.abort.set()
        stop_event.set()
        time.sleep(0.15)
        self.destroy()


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    state = SensorState()

    # threads: 2 קוראי חיישנים + 1 תקשורת PLC
    threading.Thread(
        target=sensor_reader,
        args=(state, "TOP", SCFG["TOP_port"], SCFG["reference_height_mm"]),
        daemon=True,
    ).start()
    threading.Thread(
        target=sensor_reader,
        args=(state, "BOTTOM", SCFG["BOTTOM_port"], SCFG["reference_height_mm"]),
        daemon=True,
    ).start()

    app = App(state)
    threading.Thread(target=plc_thread, args=(app,), daemon=True).start()

    app.mainloop()
    stop_event.set()


if __name__ == "__main__":
    main()
