import cv2 as cv2
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox
import random
import math
import json
from collections import deque
from PIL import Image, ImageTk
import time
import threading
import os
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
import Utils
import  thickness_Module_V4_hwts  as thck
import  mv457_MachineVision as mv
from pyModbusTCP.client import ModbusClient

# ----------------------------
# Config (global variables)
# ----------------------------
class Cfg_:
    """Configuration loaded from config\\config457.json on init."""
    ConfigPath = os.path.join("config", "config457.json")
    ConfigAttrs = ("WorkstationId", "PlcTcpip", "PlcPort", "PlcLiveSignIntervalSeconds", "SqlNt", "SqlPri", "MvFolder", "Password")

    def __init__(self):
        self.WorkstationId = ""
        self.PlcTcpip ="192.168.3.169"
        self.PlcPort = 502
        self.PlcLiveSignIntervalSeconds = 2.0
        self.SqlNt = ""
        self.SqlPri = ""
        self.MvFolder = ""
        self.Password = "1234567"
        self.plc_connected = False  # runtime only, not persisted to JSON
        self._load()

    def _load(self):
        if not os.path.isfile(self.ConfigPath):
            return
        with open(self.ConfigPath, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.WorkstationId = data.get("WorkstationId", self.WorkstationId)
        self.PlcTcpip = data.get("PlcTcpip", self.PlcTcpip)
        self.PlcPort = int(data.get("PlcPort", self.PlcPort))
        self.PlcLiveSignIntervalSeconds = float(data.get("PlcLiveSignIntervalSeconds", self.PlcLiveSignIntervalSeconds))
        self.SqlNt = data.get("SqlNt", self.SqlNt)
        self.SqlPri = data.get("SqlPri", self.SqlPri)
        self.MvFolder = data.get("MvFolder", self.MvFolder)
        self.Password = data.get("Password", "1234567")

    def save(self):
        """Persist current values to config457.json."""
        data = {
            "WorkstationId": self.WorkstationId,
            "PlcTcpip": self.PlcTcpip,
            "PlcPort": self.PlcPort,
            "PlcLiveSignIntervalSeconds": self.PlcLiveSignIntervalSeconds,
            "SqlNt": self.SqlNt,
            "SqlPri": self.SqlPri,
            "MvFolder": self.MvFolder,
            "Password": self.Password,
        }
        os.makedirs(os.path.dirname(self.ConfigPath), exist_ok=True)
        with open(self.ConfigPath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)

Cfg = Cfg_()
log_lock = threading.Lock()
RegsLock = threading.Lock()
# PLC registers: 20 integers (global); when any > 0, T4 stores them, zeros them, writes 200 regs to PLC
RegsToPlc = [0] * 20
RegsTemp = [0] * 20  # last 20 values stored when T4 flushes to PLC

RegsFromPlc = [0] * 40

# --- Thickness calibration PLC handshake registers (adjust indices to match the PLC) ---
# PLC -> Py  command (read from RegsFromPlc): 0 idle | 1 at gauge 1, measure | 2 at gauge 2, measure (auto-compute after)
CAL_CMD_REG = 24
# Py -> PLC  thickness verdict (per part): 0 none | 1 OK | 2 below nominal | 3 above nominal | 4 conicity high
THK_RESULT_REG = 5
# Py -> PLC  calibration status (separate register): 0 idle | 1 measuring (do not move) | 2 gauge 1 done (safe to move) | 3 calibration OK | 4 calibration FAIL
CAL_STAT_REG = 13
CAL_MEASURING, CAL_G1_DONE, CAL_OK, CAL_FAIL = 1, 2, 3, 4

"""
                ## 0 to 19 Py to PLC
                0:misc instructions  from Py to PLC :{0:null ,1:live signe,2:hold for gage calibration, 3:resume after gage calibration }
                1:Sorting result image : {0:null,1:OK,2:Rejecy}
                2:Sorting result thickness:{0:null,1:OK,2:Rejecy}
                3:Pause  mv conveyor
                4:Resume mv conveyor 
                5:Set mv illumination to Green
                6:Set mv illumination to White
       
                7:Servo Move to Command {0:null 1:axies X,2: axies Y }
                8: Move to target value 1unit=0.01mm
                9:Spped unit=mm/sec
                10:Pack size
                11:Override pack counter
                12..19:Spare
               
                ## 20..39 plc to py  
                20: status :{0:error,1:manual,2:auto,>10 : Error cod}
                21:Position X
                22:Position Y
                23: Servo stt { 0:null , 1:ok ready, 2:working,3:finished well  }
                24...39 free for future usage
"""

def log(msg, level="INFO"):
    t = time.time()
    s = time.strftime("%H:%M:%S", time.localtime(t))
    ms = int((t % 1) * 1000)

    line = f"{s}.{ms:03d} [{level}] {msg}"

    with log_lock:
        print(line)
        with open("system_log.txt", "a") as f:
            f.write(line + "\n")

class Job_settings_():
    # Keys persisted only in Job_settings.json (not from CSV/XLSX)
    JsonKeys = ("wo_order_number", "wo_part_number", "wo_order_quantity", "wo_object_counter", "current_pack_job")
    Job_settings_path = os.path.join("config", "Job_settings.json")
    job_pack_settings_path = os.path.join("config", "job_pack_settings.xlsx")
    last_mv_spec_path = os.path.join("config", "last_mv_spec.csv")

    def __init__(self):
        # PIL images (updated by threads / logic)
        self.pil1 = None   # engraving photot
        self.pil2 = None   # mv photo
        self.pil22 = None  # mv live photo
        self.pil3 = None   #TkcK pohoto

        # textual/debug (optional)
        self.txt1 = None
        self.txt2 = None
        self.txt3 = None

        # Work order header fields (defaults; overwritten by reload())
        self.wo_order_number   = "^ txt1"
        self.wo_part_number    = "txt2"
        self.wo_order_quantity = "1wo_order_quantity"
        self.wo_object_counter = "456"
        self.current_pack_job = 2

        # From XLSX/CSV (loaded in reload())
        self.pack_jobs = None
        self.spec = None

        # stop flag (shared)
        self.stop_event = threading.Event()

        self.thk_mean_mm = None
        self.thk_p2p_um = None
        self.thk_conicity_um = None

        # Thickness calibration view (written by T3, read by the UI). Reassigned as a
        # whole dict on each update so the cross-thread read is always consistent.
        self.thk_cal = {
            "active": False, "phase": "idle",
            "g1_raw_mm": None, "g1_std_um": None,
            "g2_raw_mm": None, "g2_std_um": None,
            "a": None, "b_um": None, "message": "",
        }

        self.reload()

    def reload(self):
        """Load from Job_settings.json, job_pack_settings.xlsx, last_mv_spec.csv."""
        if os.path.isfile(self.Job_settings_path):
            with open(self.Job_settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key in self.JsonKeys:
                if key in data:
                    setattr(self, key, data[key])
        if os.path.isfile(self.job_pack_settings_path):
            self.pack_jobs = pd.read_excel(self.job_pack_settings_path)
        if os.path.isfile(self.last_mv_spec_path):
            self.spec = pd.read_csv(self.last_mv_spec_path)

    def save(self):
        """Save to Job_settings.json, job_pack_settings.xlsx, last_mv_spec.csv. None defaults save as null (last value ignored)."""
        data = {}
        for key in self.JsonKeys:
            val = getattr(self, key, None)
            data[key] = val
        os.makedirs(os.path.dirname(self.Job_settings_path), exist_ok=True)
        with open(self.Job_settings_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        if self.pack_jobs is not None:
            self.pack_jobs.to_excel(self.job_pack_settings_path, index=False)
        if self.spec is not None:
            self.spec.to_csv(self.last_mv_spec_path, index=False)

Job_settings = Job_settings_()
 

# ----------------------------
# Random images (PIL only)
# ----------------------------
def Random_Image():
    x = random.randint(1, 3)


    img_path = f"icons\\engraving{x}.png"
    max_size = (320, 240)

    im = to_pil( Image.open(img_path))

    im.thumbnail(max_size, Image.LANCZOS)
    return im, img_path

def to_pil(im):
    # If it's already PIL, return as-is
    if isinstance(im, Image.Image):
        return im

    # If it's a numpy array, convert
    if isinstance(im, np.ndarray):
        # Grayscale: (H, W)
        if im.ndim == 2:
            return Image.fromarray(im.astype(np.uint8), mode="L")

        # Color: (H, W, 3) or (H, W, 4)
        if im.ndim == 3:
            arr = im
            if arr.dtype != np.uint8:
                arr = np.clip(arr, 0, 255).astype(np.uint8)

            # Many camera/OpenCV frames are BGR; convert to RGB if needed:
            if arr.shape[2] == 3:
                arr = arr[..., ::-1]   # BGR->RGB
                return Image.fromarray(arr, mode="RGB")


def T1():
    i = 0
    t = time.time()
    Job_settings.txt1 = 'xxx'
    Job_settings.pil1, Job_settings.txt1 = Random_Image()

    tl = t
    while not Job_settings.stop_event.is_set():
        tc = 0.001
        time.sleep(max(tc - (time.time() - tl), 0))
        i += 1
        tl = time.time()
        if i >= 1000:

         

            Job_settings.pil1, Job_settings.txt1 = Random_Image()

            i = 0
            Job_settings.txt1 += f"  {round(time.time() - t, 3)}"
            t = time.time()

def T2():
    t = time.time()
    t1 = 0
    state = 0
    t21 = None

    RegsToPlc[1] = 1
    RegsToPlc[2] = 2
    im = to_pil(mv.img.last_img)
    im.thumbnail((640, 480), Image.LANCZOS)
    Job_settings.pil2 = im


    while not Job_settings.stop_event.is_set():
        mv.img.refresh()
        if time.time()-t1>0.3 and state !=3:
            im2 = to_pil(mv.img.last_img)
            im2.thumbnail((320, 240), Image.LANCZOS)
            Job_settings.pil22 = im2
            t1=time.time()

        #print('mv.img.status:', mv.img.status,'state:',state)
        if state == 0 and mv.img.status == 3:
            log('conveyor was stopped')
            RegsToPlc[1]=3 #stop conveyor
            state = 1
        if state == 1 and mv.img.status == 4:
            mv.img.img_G=mv.img.last_img ##save current img as Green_img
            cv2.imwrite(f"native_img\\native_green{mv.Cfg_mv.counter+1}.jpg", mv.img.img_G)
            log('green light img is saved')
            #RegsToPlc[2:4] = [1, 2]
            RegsToPlc[2] = 1 ##turn green light off
            RegsToPlc[3] = 2 ##turn white light on
            state = 2
        if state == 2 and mv.img.WhiteOn == 1:
            mv.img.img_W = mv.img.last_img  ##save current img as White_img
            cv2.imwrite(f"native_img\\native_white{mv.Cfg_mv.counter +1}.jpg", mv.img.img_W)
            log('white light img is saved')
            with RegsLock:
                RegsToPlc[1:4]=[1,2,1]
            #RegsToPlc[1]=1 #start conveyor
            #RegsToPlc[2] = 2  ##turn green light on
            #RegsToPlc[3] = 1  ##turn white light off
            #print('RegsToPlc_change',RegsToPlc)

            #להוסיף בדיקה אם יש כבר תהליך ניתוח תמונה פעיל, להמתין עד שייסתיים
            log(f'start image processing prog-- No. {mv.Cfg_mv.counter+1}')
            t21 = threading.Thread(target=mv.run_prog, daemon=True)
            t21.start()
            state = 3
            mv.img.status = 0

        if state == 3 and t21 is not None and not t21.is_alive():  # analyze is completed
            RegsToPlc[4] = mv.Cfg_mv.sttprog ## report to plc if the bearing is valid/invalid
            if  mv.Cfg_mv.sttprog==2:
                stt_obj='OK'
            else:
                stt_obj='NOT OK'
            log(f'img processed done successfully-- object status is {stt_obj}','STATUS')
            ## refresh the current img_toshow at the UI
            im = to_pil(mv.img.img_toShow)
            im.thumbnail((640, 480), Image.LANCZOS)
            Job_settings.pil2 = im
            state = 0

        ctime=time.time() - t
        #print('ctime', round(ctime, 3))
        time.sleep(max(0,  0.05 -(time.time() - t) ) )

        t = time.time()
        #print(       f'state: {state}, mv.img.status:{mv.img.status}, looptime:{round(time.time() - t, 3)}sec')



def T3_thickness():
    ThckModule = thck.ThicknessModule()
    ThckModule.start()

    last_cal_cmd = None                 # edge-detect the PLC calibration command
    cal_raw = {"g1": None, "g2": None}  # raw gauge readings collected during a calibration sequence

    def _fresh_cal_view():
        return {"active": True, "phase": "measuring_g1",
                "g1_raw_mm": None, "g1_std_um": None,
                "g2_raw_mm": None, "g2_std_um": None,
                "a": None, "b_um": None, "message": ""}

    cal_view = dict(Job_settings.thk_cal)

    def _publish_cal(**changes):
        # Reassign the whole dict so the UI thread always reads a consistent snapshot.
        cal_view.update(changes)
        Job_settings.thk_cal = dict(cal_view)

    while not Job_settings.stop_event.is_set():
        try:
                Job_settings.pil3, Job_settings.thk_mean_mm, Job_settings.thk_p2p_um, Job_settings.thk_conicity_um = ThckModule.get_ui_packet(last_n=3, size_px=(420, 900))
                # Forward the thickness verdict to PLC reg 5, same way T2 reports the image verdict to reg 4
                code = ThckModule.poll_new_result_code()
                if code is not None:
                    RegsToPlc[THK_RESULT_REG] = code
                    verdict = {1: 'OK', 2: 'thickness low', 3: 'thickness high', 4: 'conicity high'}.get(code, 'unknown')
                    log(f'thickness verdict reg5={code} ({verdict})', 'STATUS')

                # --- Thickness calibration handshake with the PLC (edge-triggered) ---
                # PLC moves to each gauge and signals its position; the code measures and,
                # after gauge 2, automatically computes/applies and reports the OK/FAIL result.
                cal_cmd = RegsFromPlc[CAL_CMD_REG]
                if cal_cmd != last_cal_cmd:
                    last_cal_cmd = cal_cmd
                    if cal_cmd in (1, 2):
                        gauge = "g1" if cal_cmd == 1 else "g2"
                        if cal_cmd == 1:
                            cal_view = _fresh_cal_view()   # gauge 1 starts a fresh sequence
                            Job_settings.thk_cal = dict(cal_view)
                        else:
                            _publish_cal(active=True, phase="measuring_g2", message="")
                        RegsToPlc[CAL_STAT_REG] = CAL_MEASURING  # busy measuring (PLC must not move yet)
                        log(f'calibration: measuring gauge {gauge[-1]}', 'STATUS')
                        res = ThckModule.measure_gauge()

                        if not res.get("ok"):
                            cal_raw[gauge] = None
                            RegsToPlc[CAL_STAT_REG] = CAL_FAIL  # measurement failed -> abort sequence
                            _publish_cal(**{f"{gauge}_raw_mm": res.get("raw_median_mm"),
                                            f"{gauge}_std_um": res.get("std_um"),
                                            "phase": "done_fail",
                                            "message": f"gauge {gauge[-1]}: {res.get('reason')}"})
                            log(f"calibration: gauge {gauge[-1]} measurement failed ({res.get('reason')})", 'WARN')
                        else:
                            cal_raw[gauge] = res["raw_median_mm"]
                            _publish_cal(**{f"{gauge}_raw_mm": res["raw_median_mm"],
                                            f"{gauge}_std_um": res["std_um"],
                                            "phase": f"{gauge}_done"})
                            log(f"calibration: gauge {gauge[-1]} raw={res['raw_median_mm']:.4f}mm std={res['std_um']:.2f}um", 'STATUS')

                            if gauge == "g1":
                                RegsToPlc[CAL_STAT_REG] = CAL_G1_DONE  # gauge 1 done, safe to move to gauge 2
                            else:
                                # Gauge 2 done -> compute & apply automatically, then report the final result.
                                _publish_cal(phase="computing", message="")
                                ok, cinfo = ThckModule.compute_and_apply_calibration(cal_raw["g1"], cal_raw["g2"])
                                if ok:
                                    RegsToPlc[CAL_STAT_REG] = CAL_OK  # calibration OK
                                    _publish_cal(phase="done_ok", a=cinfo["a"], b_um=cinfo["b"] * 1000.0, message="")
                                    log(f"calibration applied: a={cinfo['a']:.4f} b={cinfo['b'] * 1000.0:+.1f}um", 'STATUS')
                                else:
                                    RegsToPlc[CAL_STAT_REG] = CAL_FAIL  # calibration FAIL
                                    _publish_cal(phase="done_fail", message=cinfo.get("reason", ""))
                                    log(f"calibration failed: {cinfo.get('reason')}", 'WARN')
                                cal_raw["g1"] = cal_raw["g2"] = None
                    elif cal_cmd == 0:
                        RegsToPlc[CAL_STAT_REG] = 0  # back to idle (window keeps showing the last result)
        except Exception as e:
            Job_settings.txt3 = f"THK update error: {e}"

        time.sleep(0.2)

    ThckModule.stop()


def T4_plc_flush():
    """Send to PLC 20 regs only when any > 0;
     if nothing written for > 2 s send live sign (reg 0 = 1).
     After each send, read 40 regs from PLC into plc_read_registers."""
    global RegsToPlc, RegsTemp, RegsFromPlc, LastSentRegs

    def _write_20_and_read_40(client, regs_20):

        if not client.write_multiple_registers(0, regs_20):
            return False
        time.sleep(0.003)
        read_40 = client.read_holding_registers(0, 40)

        if read_40 is not None and len(read_40) >= 40:
            RegsFromPlc[:] = list(read_40)[:40]
        return True



    global RegsToPlc, RegsFromPlc

    client = ModbusClient(
        host="192.168.3.169",
        port=Cfg.PlcPort,
        auto_open=True,
        auto_close=False,
        timeout=0.1
    )
    regs=[0] * 20
    LastPlcWriteTime = time.time() - 10
    while not Job_settings.stop_event.is_set():
        #print('RegsToPlc',RegsToPlc)
        # בדיקה אם יש משהו לשלוח
        if any(val > 0 for val in RegsToPlc):

            # העתקה כדי למנוע שינוי תוך כדי שליחה
            #regs = RegsToPlc.copy()
            with RegsLock:
                regs = RegsToPlc.copy()
                RegsToPlc = [0] * 20
            #print("Sent_b:", regs)

            try:
                # שליחה
                ok = _write_20_and_read_40(client, regs)
                #ok=client.write_multiple_registers(0, regs)
                if ok:
                    print("Sent successfully:", regs)


                else:
                    print("Write failed")
                    with RegsLock:
                        RegsToPlc = regs
            except Exception as e:
                print("PLC error:", e)
                client.close()

        else:
            if time.time() - LastPlcWriteTime > Cfg.PlcLiveSignIntervalSeconds:

                if RegsToPlc[0] == 0:
                    RegsToPlc[0] = 1
                LastPlcWriteTime = time.time()

        time.sleep(0.02)

        ##RegsToPlc value:
        ##RegsToPlc[0]: 1- Livesign
        ##RegsToPlc[1]: 1- run con forward 2- run con backwards 3- stop con
        ##RegsToPlc[2]: 1- turn on green light 2- turn off green light
        ##RegsToPlc[3]: 1- turn on white light 2- turn off white light
        ##RegsToPlc[4]: 3- Image analysis OK 4- Image analysis NOT OK
        ##RegsToPlc[5]: thickness verdict 0-none 1-OK 2-below nominal 3-above nominal 4-conicity too high
        ##RegsToPlc[13]: calibration status 0-idle 1-measuring 2-gauge1 done 3-cal OK 4-cal FAIL


def T4_plc_flush_1():
    """Send to PLC 20 regs only when any > 0;
     if nothing written for > 2 s send live sign (reg 0 = 1).
     After each send, read 40 regs from PLC into plc_read_registers."""
    global RegsToPlc, RegsTemp, RegsFromPlc, LastSentRegs

    def _write_20_and_read_40(client, regs_20):

        client.write_multiple_registers(0, regs_20)
        read_40 = client.read_holding_registers(0, 40)

        if read_40 is not None and len(read_40) >= 40:
            RegsFromPlc[:] = list(read_40)[:40]
            return True

        return False

    LastPlcWriteTime = time.time() - 10
    LastSentRegs = [0] * 20

    client = ModbusClient(
        host="192.168.3.169",
        port=Cfg.PlcPort,
        auto_open=False,
        auto_close=False,
        timeout=0.2
    )

    conn = client.open()
    Cfg.plc_connected = conn

    while not Job_settings.stop_event.is_set():

        # reconnect אם החיבור נפל
        if not conn:
            try:
                client.close()
            except:
                pass

            conn = client.open()
            Cfg.plc_connected = conn
            time.sleep(0.1)
            print("new connect is open")
            continue

        try:

            if RegsToPlc != LastSentRegs:


                Regs=RegsToPlc.copy()
                ok =_write_20_and_read_40(client, Regs)
                print("Send change:", RegsToPlc,time.time(),'status:',ok)
                if not ok:
                    time.sleep(0.02)
                    ok = _write_20_and_read_40(client, Regs)
                    print('resend',ok)
                LastSentRegs = Regs
                LastPlcWriteTime = time.time()

            else:
                if time.time() - LastPlcWriteTime > Cfg.PlcLiveSignIntervalSeconds:

                    RegsTemp = RegsToPlc.copy()
                    RegsTemp[0] = 1

                    ok=_write_20_and_read_40(client, RegsTemp)
                    LastPlcWriteTime = time.time()
                    #print("PLC LiveSign",RegsTemp,time.time())
                    RegsTemp[0] = 0


        except Exception as e:

            print("PLC communication error:", e)

            conn = False
            Cfg.plc_connected = False

        time.sleep(0.02)


        ##RegsToPlc value:
        ##RegsToPlc[0]: 1- Livesign
        ##RegsToPlc[1]: 1- run con forward 2- run con backwards 3- stop con
        ##RegsToPlc[2]: 1- turn on green light 2- turn off green light
        ##RegsToPlc[3]: 1- turn on white light 2- turn off white light
        ##RegsToPlc[4]: 3- Image analysis OK 4- Image analysis NOT OK
        ##RegsToPlc[5]: thickness verdict 0-none 1-OK 2-below nominal 3-above nominal 4-conicity too high
        ##RegsToPlc[13]: calibration status 0-idle 1-measuring 2-gauge1 done 3-cal OK 4-cal FAIL




def GetNewJob_OLDREV(value):
    """Activate new job: resolve order from SQL, update Job_settings, load MV spec. Returns (success, part_name)."""
    value = (value or "").strip()
    if not value or not (len(value) > 1 and value[1:].isdigit()):
        return False, None
    try:
        order_id = int(value[1:]) if value[1:].isdigit() else int(value)
    except ValueError:
        return False, None
    Utils.conn_string2 = "mssql+pyodbc://pyapps:kngapps887@king-nt1/KINGDB?driver=SQL+Server+Native+Client+11.0"
    sql = """   SELECT
          RTRIM(dbo.kprdord.part_no) + '-' + RTRIM(dbo.KSPEC_NO.descript) + '.csv' AS filnemae,
          RTRIM(dbo.kprdord.part_no) + '-' + RTRIM(dbo.KSPEC_NO.descript)+ ' ' + dbo.kprdord.mida AS PARTNAME,
          dbo.KINGHAZT.ordr_no,
          dbo.kprdord.ordline_no, 
          dbo.kprdord.SERIALNAME,
          dbo.kprdord.qty,
          dbo.kprdord.hs_nomi, 
          dbo.kprdord.thik_min,
           dbo.kprdord.length, dbo.kprdord.mida
    FROM            dbo.kprdord INNER JOIN
                                         dbo.KSPEC_NO ON dbo.kprdord.spec_no = dbo.KSPEC_NO.spec_no INNER JOIN
                                         dbo.KINGHAZT ON dbo.kprdord.ordheadid = dbo.KINGHAZT.ordheadid
    WHERE        dbo.kprdord.num_id =""" + str(order_id)

    df = Utils.trim_all_columns(Utils.SqlSelect(sql))
   
    if df.shape[0] != 1:
        return False, None
    F = df.loc[0, "filnemae"]
    Filename  = F.replace(chr(92), "-").replace("/", "-")+".csv"
    P = df.loc[0, "PARTNAME"]
    Job_settings.wo_order_number = value
    Job_settings.wo_part_number = F
   
    if ".CSV" not in Filename:
        Filename = Filename + ".CSV"
    spec_paths = [
        os.path.join("W:\\MachineVisionTemplates", Filename),
        os.path.join("config", Filename),
    ]
    spec_loaded = False
    for path in spec_paths:
        if os.path.isfile(path):
            try:
                spec_df = pd.read_csv(path)
                spec_df.to_csv("config\\last_mv_spec.csv", index=False)
                mv.GetSpec(spec_df)
                spec_loaded = True
                break
            except Exception:
                continue
    if not spec_loaded:
        pass
    return True, P


def GetNewJob(value):
    """Activate new job: resolve order from SQL, update Job_settings, load MV spec, refresh thickness limits. Returns (success, part_name)."""
    value = (value or "").strip()
    if not value or not (len(value) > 1 and value[1:].isdigit()):
        return False, None
    try:
        order_id = int(value[1:]) if value[1:].isdigit() else int(value)
    except ValueError:
        return False, None
    Utils.conn_string2 = "mssql+pyodbc://pyapps:kngapps887@king-nt1/KINGDB?driver=SQL+Server+Native+Client+11.0"
    sql = """   SELECT
          RTRIM(dbo.kprdord.part_no) + '-' + RTRIM(dbo.KSPEC_NO.descript) + '.csv' AS filnemae,
          RTRIM(dbo.kprdord.part_no) + '-' + RTRIM(dbo.KSPEC_NO.descript)+ ' ' + dbo.kprdord.mida AS PARTNAME,
          dbo.KINGHAZT.ordr_no,
          dbo.kprdord.ordline_no,
          dbo.kprdord.SERIALNAME,
          dbo.kprdord.qty,
          dbo.kprdord.hs_nomi,
          dbo.kprdord.thik_min,
          dbo.kprdord.thik_max,
          dbo.kprdord.length,
          dbo.kprdord.mida
    FROM            dbo.kprdord INNER JOIN
                                         dbo.KSPEC_NO ON dbo.kprdord.spec_no = dbo.KSPEC_NO.spec_no INNER JOIN
                                         dbo.KINGHAZT ON dbo.kprdord.ordheadid = dbo.KINGHAZT.ordheadid
    WHERE        dbo.kprdord.num_id =""" + str(order_id)

    df = Utils.trim_all_columns(Utils.SqlSelect(sql))
    if df.shape[0] != 1:
        return False, None

    F = df.loc[0, "filnemae"]
    P = df.loc[0, "PARTNAME"]
    Job_settings.wo_order_number = value
    Job_settings.wo_part_number = F

    # Save job thickness limits only in config\config457_thk_hwts.json (T3 reads from there via get_job_limits)
    default_min_mm = 0.3
    default_max_mm = 3.0
    try:
        thik_min_val = df.loc[0, "thik_min"]
        thik_min_mm = float(thik_min_val) if thik_min_val is not None and not (isinstance(thik_min_val, float) and pd.isna(thik_min_val)) else default_min_mm
    except (TypeError, ValueError):
        thik_min_mm = default_min_mm
    try:
        thik_max_val = df.loc[0, "thik_max"]
        thik_max_mm = float(thik_max_val) if thik_max_val is not None and not (isinstance(thik_max_val, float) and pd.isna(thik_max_val)) else default_max_mm
    except (TypeError, ValueError):
        thik_max_mm = default_max_mm

    thk_config_path = os.path.join("config", "config457_thk_hwts.json")
    if os.path.isfile(thk_config_path):
        try:
            with open(thk_config_path, "r", encoding="utf-8") as f:
                thk_data = json.load(f)
            thk_data["thickness_min_mm"] = thik_min_mm
            thk_data["thickness_max_mm"] = thik_max_mm
            with open(thk_config_path, "w", encoding="utf-8") as f:
                json.dump(thk_data, f, indent=2)
        except Exception:
            pass

    base = (F.replace(chr(92), "-").replace("/", "-")) if F else ""
    Filename = base + ".csv" if base and not base.upper().endswith(".CSV") else (base if base else "")
    spec_loaded = False
   
    if Cfg.MvFolder and Filename:
        path = os.path.join(Cfg.MvFolder, Filename)
        if os.path.isfile(path):
            try:
                spec_df = pd.read_csv(path)
                mv.GetSpec(spec_df)
                spec_loaded = True
            except Exception:
                pass
    if not spec_loaded:
        return False, None
    return True, P


# ----------------------------
# UI Form
# ----------------------------
class EL_UI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("King Engine Bearings End Line Machine 457")
        self.geometry("1650x850")
        self.resizable(False, False)

        self.pil_plc_off = Image.open("icons\\PLC_off.png").resize((48, 48), Image.LANCZOS)
        self.tk_plc_off = ImageTk.PhotoImage(self.pil_plc_off)
        self.pil_plc_on = Image.open("icons\\PLC_on.png").resize((48, 48), Image.LANCZOS)

        self.tk_plc_on = ImageTk.PhotoImage(self.pil_plc_on)
        self._build_ui()

        self.after(100, self._refresh_from_Job_settings)

    def NewJob(self, title: str = "Settings"):
        # Use separate _newjob_win so New Job dialog is independent of other settings windows
        if getattr(self, "_newjob_win", None) is not None and self._newjob_win.winfo_exists():
            try:
                self._newjob_win.deiconify()
                self._newjob_win.lift()
                self._newjob_win.focus_force()
            except Exception:
                pass
            return

        win = tk.Toplevel(self)
        self._newjob_win = win
        win.title(title)
        win.transient(self)
        win.grab_set()
        win.resizable(False, False)

        w, h = 1500, 750
        try:
            self.update_idletasks()
            x = self.winfo_rootx() + (self.winfo_width() - w) // 2
            y = self.winfo_rooty() + (self.winfo_height() - h) // 2
            win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            win.geometry(f"{w}x{h}")

        def _close():

            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()
            self._newjob_win = None

        win.protocol("WM_DELETE_WINDOW", _close)

        container = ttk.Frame(win, padding=12)
        container.pack(fill="both", expand=True)

        body = ttk.Frame(container, borderwidth=1, relief="solid")
        body.pack(fill="both", expand=True)

        # ===============================
        # Top input (upper-left)
        # ===============================
        top_row = ttk.Frame(body, padding=(10, 10, 10, 0))
        top_row.pack(anchor="nw", fill="x")

        ttk.Label(top_row, text="New Order Nbr:", font=("Arial", 18, "bold")).pack(side="left")

        self.newjob_text_var = tk.StringVar()

        entry = tk.Entry(
            top_row,
            textvariable=self.newjob_text_var,
            font=("Arial", 22, "bold"),
            width=20,  # 20 characters visible
            justify="left",
        )
        entry.pack(side="left", padx=(10, 0))
        entry.focus_set()

        # ===============================
        # Two info lines below
        # ===============================
        info_frame = ttk.Frame(body, padding=(20, 15, 10, 0))
        info_frame.pack(anchor="nw", fill="x")

        # existing var1/var2 placeholders (StringVar so they refresh automatically)
        if not hasattr(self, "var_order_nbr"):
            self.var_order_nbr = tk.StringVar(value="—")  # var1
        if not hasattr(self, "var_part_nbr"):
            self.var_part_nbr = tk.StringVar(value="—")  # var2

        # line1: ['Order Nbr:'] [var1]
        row1 = ttk.Frame(info_frame)
        row1.pack(anchor="w", pady=4)
        ttk.Label(row1, text="Order Nbr:", font=("Arial", 14, "bold")).pack(side="left")
        ttk.Label(row1, textvariable=self.var_order_nbr, font=("Arial", 14)).pack(side="left", padx=8)

        # line2: ['Part Nbr:'] [var2]
        row2 = ttk.Frame(info_frame)
        row2.pack(anchor="w", pady=4)
        ttk.Label(row2, text="Part Nbr:", font=("Arial", 14, "bold")).pack(side="left")
        ttk.Label(row2, textvariable=self.var_part_nbr, font=("Arial", 14)).pack(side="left", padx=8)

        # ===============================
        # Bottom buttons (same size/shape)
        # ===============================
        bottom = ttk.Frame(container)
        bottom.pack(fill="x", pady=(12, 0))

        btn_opts = dict(padding=(14, 10))

        btn_save = ttk.Button(
            bottom,
            text="Save and exit",
            command=_close,
            state="disabled",  # disabled until valid

        )
        btn_save.pack(side="left", expand=True, fill="x", padx=6)

        ttk.Button(
            bottom,
            text="Exit without saving",
            command=_close,

        ).pack(side="left", expand=True, fill="x", padx=6)

        # ===============================
        # Validation on Enter
        # ===============================
        def _on_validate_enter(_evt=None):
            value = self.newjob_text_var.get().strip()

            # enforce max length 20
            if len(value) > 20:
                value = value[:20]
                self.newjob_text_var.set(value)

            if not value:
                win.bell()
                btn_save.config(state="disabled")
                return "break"

            # valid only if txt[1:] all digits
            is_valid = (len(value) > 1 and value[1:].isdigit())

            if is_valid:
                success, part_name =     GetNewJob(value)
                if success:
                    print("WO number format OK:", value)
                    self.var_order_nbr.set(value)
                    self.var_part_nbr.set(part_name if part_name else f"P-{value[1:]}")
                    btn_save.config(state="normal")
                else:
                    win.bell()
                    btn_save.config(state="disabled")
            else:
                win.bell()
                btn_save.config(state="disabled")

            return "break"

        entry.bind("<Return>", _on_validate_enter)

        # Bring New Job dialog to front so it is visible
        win.lift()
        win.focus_force()

    def _open_settings_screen(self, title: str = "Settings"):
        if title == "Thickness Adjustment Settings":
            self._open_thickness_adjustment_screen()
            return
        if getattr(self, "_settings_win", None) is not None and self._settings_win.winfo_exists():
            try:
                self._settings_win.deiconify()
                self._settings_win.lift()
                self._settings_win.focus_force()
            except Exception:
                pass
            return

        win = tk.Toplevel(self)
        self._settings_win = win
        win.title(title)
        win.transient(self)
        win.grab_set()
        win.resizable(False, False)

        w, h = 900, 550
        try:
            self.update_idletasks()
            x = self.winfo_rootx() + (self.winfo_width() - w) // 2
            y = self.winfo_rooty() + (self.winfo_height() - h) // 2
            win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            win.geometry(f"{w}x{h}")

        def _close():
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()
            self._settings_win = None

        win.protocol("WM_DELETE_WINDOW", _close)

        container = ttk.Frame(win, padding=12)
        container.pack(fill="both", expand=True)

        body = ttk.Frame(container, borderwidth=1, relief="solid")
        body.pack(fill="both", expand=True)

        bottom = ttk.Frame(container)
        bottom.pack(fill="x", pady=(12, 0))

        btn_opts = dict(padding=(14, 10))
        ttk.Button(bottom, text="Save and exit", command=_close, **btn_opts).pack(side="left", expand=True, fill="x", padx=6)
        ttk.Button(bottom, text="Exit without saving", command=_close, **btn_opts).pack(side="left", expand=True, fill="x", padx=6)
        ttk.Button(bottom, text="Resolution Calibration", command=mv.Image_calibration, **btn_opts).pack(side="left", expand=True, fill="x", padx=6)

    def _open_thickness_adjustment_screen(self):
        """Open form to modify all thck.Cfg (config457_thk_hwts.json) properties. Save / Reload / Close. Protected by Password from config457.json."""
        if getattr(self, "_thk_settings_win", None) is not None:
            try:
                if self._thk_settings_win.winfo_exists():
                    self._thk_settings_win.lift()
                    self._thk_settings_win.focus_force()
                    return
            except Exception:
                self._thk_settings_win = None

        entered = simpledialog.askstring("Thickness Adjustment", "Enter password:", show="*")
        if entered is None:
            return
        if entered != getattr(Cfg, "Password", '9114'):
            messagebox.showerror("Access denied", "Incorrect password.")
            return

        ThkConfigPath = os.path.join("config", "config457_thk_hwts.json")

        # Flat keys for form: (label, key_path). key_path "sensors.TOP.ip" etc. or scalar key.
        def _thk_flat_keys():
            return [
                ("schema_version", "schema_version"),
                ("TOP ip", "sensors.TOP.ip"),
                ("TOP port", "sensors.TOP.port"),
                ("BOTTOM ip", "sensors.BOTTOM.ip"),
                ("BOTTOM port", "sensors.BOTTOM.port"),
                ("socket_timeout_sec", "socket_timeout_sec"),
                ("frame_interval_sec", "frame_interval_sec"),
                ("live_update_hz", "live_update_hz"),
                ("reference_height_mm", "reference_height_mm"),
                ("sensor_distance_mm", "sensor_distance_mm"),
                ("error_threshold_mm", "error_threshold_mm"),
                ("axial_span_mm", "axial_span_mm"),
                ("thickness_min_mm", "thickness_min_mm"),
                ("thickness_max_mm", "thickness_max_mm"),
                ("target_points", "target_points"),
                ("trim_lo", "trim_lo"),
                ("trim_hi", "trim_hi"),
                ("p2p_thresh_um", "p2p_thresh_um"),
                ("conicity_thresh_um", "conicity_thresh_um"),
                ("nominal_thickness_mm_default", "nominal_thickness_mm_default"),
                ("tol_um_default", "tol_um_default"),
                # --- Method B (hardware-timestamp pairing) tuning ---
                ("pairing_max_skew_sec", "pairing_max_skew_sec"),
                ("pairing_period_ema_alpha", "pairing_period_ema_alpha"),
                ("pairing_max_drop_resync", "pairing_max_drop_resync"),
                # --- Feature-aware measurement (holes / grooves) ---
                ("end_of_part_gap_sec", "end_of_part_gap_sec"),
                ("feature_depth_um", "feature_depth_um"),
                ("feature_edge_um", "feature_edge_um"),
                ("feature_min_baseline_samples", "feature_min_baseline_samples"),
            ]

        def _get_nested(data, path):
            parts = path.split(".")
            cur = data
            for p in parts:
                cur = cur.get(p) if isinstance(cur, dict) else cur
                if cur is None:
                    return None
            return cur

        def _set_nested(data, path, value):
            parts = path.split(".")
            cur = data
            for i, p in enumerate(parts[:-1]):
                if p not in cur:
                    cur[p] = {}
                cur = cur[p]
            cur[parts[-1]] = value

        def _load_thk_data():
            if not os.path.isfile(ThkConfigPath):
                return {}
            with open(ThkConfigPath, "r", encoding="utf-8") as f:
                return json.load(f)

        win = tk.Toplevel(self)
        self._thk_settings_win = win
        win.title("Thickness Adjustment Settings")
        win.transient(self)
        win.grab_set()
        win.resizable(True, True)

        w, h = 560, 620
        try:
            self.update_idletasks()
            x = self.winfo_rootx() + (self.winfo_width() - w) // 2
            y = self.winfo_rooty() + (self.winfo_height() - h) // 2
            win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            win.geometry(f"{w}x{h}")

        def close_win():
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()
            self._thk_settings_win = None

        win.protocol("WM_DELETE_WINDOW", close_win)

        container = ttk.Frame(win, padding=12)
        container.pack(fill="both", expand=True)

        form_frame = ttk.LabelFrame(container, text="thck.Cfg (config457_thk_hwts.json)", padding=8)
        form_frame.pack(fill="both", expand=True)

        flat = _thk_flat_keys()
        mid = (len(flat) + 1) // 2
        left_frame = ttk.Frame(form_frame)
        right_frame = ttk.Frame(form_frame)
        left_frame.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right_frame.pack(side="left", fill="both", expand=True)
        left_frame.grid_columnconfigure(1, weight=1)
        right_frame.grid_columnconfigure(1, weight=1)

        thk_vars = {}
        data = _load_thk_data()
        for idx, (label, key_path) in enumerate(flat):
            parent = left_frame if idx < mid else right_frame
            row_idx = idx if idx < mid else idx - mid
            val = _get_nested(data, key_path)
            if val is None and "." not in key_path:
                val = data.get(key_path)
            disp = "" if val is None else str(val)
            ttk.Label(parent, text=label + ":", width=22, anchor="w").grid(row=row_idx, column=0, sticky="w", padx=(0, 6), pady=2)
            var = tk.StringVar(value=disp)
            thk_vars[key_path] = var
            entry = ttk.Entry(parent, textvariable=var, width=20)
            entry.grid(row=row_idx, column=1, sticky="ew", pady=2)
            if key_path == "schema_version":
                entry.config(state="disabled")

        bottom_btns = ttk.Frame(container)
        bottom_btns.pack(fill="x", pady=(12, 0))

        def save_thk():
            data = _load_thk_data()
            for label, key_path in flat:
                var = thk_vars.get(key_path)
                if var is None:
                    continue
                raw = var.get().strip()
                if key_path == "schema_version":
                    continue
                # Method B / feature keys: never overwrite with an empty/zero value — a
                # blank pairing tolerance or end-of-part gap would silently break things.
                if key_path in ("pairing_max_skew_sec", "pairing_period_ema_alpha", "pairing_max_drop_resync",
                                "end_of_part_gap_sec", "feature_depth_um", "feature_edge_um", "feature_min_baseline_samples") and not raw:
                    continue
                if key_path in ("sensors.TOP.port", "sensors.BOTTOM.port", "target_points", "live_update_hz",
                                "pairing_max_drop_resync", "feature_min_baseline_samples"):
                    try:
                        val = int(raw) if raw else 0
                    except ValueError:
                        val = 0
                elif key_path in ("socket_timeout_sec", "frame_interval_sec", "reference_height_mm",
                                  "sensor_distance_mm", "error_threshold_mm", "axial_span_mm", "thickness_min_mm", "thickness_max_mm",
                                  "trim_lo", "trim_hi", "p2p_thresh_um", "conicity_thresh_um", "nominal_thickness_mm_default", "tol_um_default",
                                  "pairing_max_skew_sec", "pairing_period_ema_alpha", "end_of_part_gap_sec", "feature_depth_um", "feature_edge_um"):
                    try:
                        val = float(raw) if raw else 0.0
                    except ValueError:
                        val = 0.0
                else:
                    val = raw if raw else ""
                _set_nested(data, key_path, val)
            os.makedirs(os.path.dirname(ThkConfigPath), exist_ok=True)
            with open(ThkConfigPath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            # Note: thickness_Module_V4_hwts no longer exposes a module-level Cfg.
            # Edits to the JSON take effect on the next run of T3_thickness.

        def reload_thk():
            data = _load_thk_data()
            for label, key_path in flat:
                var = thk_vars.get(key_path)
                if var is None:
                    continue
                val = _get_nested(data, key_path)
                if val is None and "." not in key_path:
                    val = data.get(key_path)
                var.set("" if val is None else str(val))

        btn_opts = dict(padding=(14, 10))
        ttk.Button(bottom_btns, text="Save", command=save_thk, **btn_opts).pack(side="left", expand=True, fill="x", padx=6)
        ttk.Button(bottom_btns, text="Reload", command=reload_thk, **btn_opts).pack(side="left", expand=True, fill="x", padx=6)
        ttk.Button(bottom_btns, text="Close", command=close_win, **btn_opts).pack(side="left", expand=True, fill="x", padx=6)

    def _build_ui(self):
        footer = ttk.Frame(self, padding=10)
        footer.pack(side="bottom", fill="x")

        main = ttk.Frame(self, padding=10)
        main.pack(side="top", fill="both", expand=True)

        header = ttk.Frame(main)
        header.pack(fill="x")

        pil = Image.open("icons\\logo.png").resize((220, 60), Image.LANCZOS)
        self.logo_img = ImageTk.PhotoImage(pil)
        ttk.Label(header, image=self.logo_img).pack(side="left")

        wo_header = ttk.Frame(header)
        wo_header.pack(side="left", padx=(12, 0))
        wo_header.bind("<Button-1>", lambda e: self.NewJob())
        wo_header.bind("<Double-Button-1>", lambda e: self.NewJob())

        def add_hdr_row(r, title):
            ttk.Label(wo_header, text=title + ":", width=14).grid(row=r, column=0, sticky="w")
            val = ttk.Label(wo_header, text="", width=18)
            val.grid(row=r, column=1, sticky="w")
            return val

        self.hdr_wo_order_num   = add_hdr_row(0, "Order Number")
        self.hdr_wo_part_num    = add_hdr_row(1, "Part Number")
        self.hdr_wo_order_qty   = add_hdr_row(2, "Order Quantity")
        self.hdr_wo_obj_counter = add_hdr_row(3, "Object Counter")
        for lbl in (self.hdr_wo_order_num, self.hdr_wo_part_num):
            lbl.bind("<Button-1>", lambda e: self.NewJob())
        wo_header.grid_columnconfigure(1, weight=1)

        ### Upper Right
        right_bar = ttk.Frame(header)
        right_bar.pack(side="right")

        plc_frame = ttk.Frame(right_bar)
        plc_frame.pack(side="left", padx=(0, 12))
#
        self.plc_icon_off = os.path.join("icons", "PLC_off.png")
        self.plc_icon_on = os.path.join("icons", "PLC_on.png")

        self.plc_icon_lbl = ttk.Label(plc_frame)
        self.plc_icon_lbl.pack(side="left")
        self.plc_text_lbl = ttk.Label(plc_frame, text="")
        self.plc_text_lbl.pack(side="left", padx=(6, 0))

        ttk.Button(right_bar, text="Refresh", command=self._on_refresh).pack(side="left", padx=(0, 6))
        ttk.Button(right_bar, text="Close", command=self._on_close).pack(side="left")

        ### end plc

        imgs = ttk.Frame(main)
        imgs.pack(side="top", fill="both", expand=True)

        self._info_block(imgs, 0)
        self._analysis_block(imgs, 1)
        self._thickness_block(imgs, 2)

        imgs.grid_columnconfigure(0, weight=2, uniform='panels')
        imgs.grid_columnconfigure(1, weight=3, uniform='panels')
        imgs.grid_columnconfigure(2, weight=2, uniform='panels')
        imgs.grid_rowconfigure(0, weight=1)

        buttons = ttk.Frame(footer)
        buttons.pack(fill="x")

        ttk.Button(buttons, text="⚙ Engraving Adjustment",
                   command=lambda: self._open_settings_screen("Engraving Adjustment Settings")).pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(buttons, text="⚙ Camera Adjustment",
                   command=lambda: self._open_settings_screen("Camera Adjustment Settings")).pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(buttons, text="⚙ Thck Adjustment",
                   command=lambda: self._open_settings_screen("Thickness Adjustment Settings")).pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(buttons, text="⚙ NewJob",
                   #command=lambda: self._open_settings_screen("Select Work Order")).pack(side="left", expand=True, fill="x", padx=5)
                   command=lambda: self.NewJob ()).pack(side="left", expand=True,fill="x", padx=5)
        ttk.Button(buttons, text="Calibration",
                   command=self._open_view_calibration).pack(side="left", expand=True, fill="x", padx=5)
        ttk.Button(buttons, text="Modify Settings",
                   command=self._open_modify_settings).pack(side="left", expand=True, fill="x", padx=5)

    def _open_modify_settings(self):
        """Open form to modify all settings (scalars + Cfg). Save/Reload/Close. Only non-None values shown for editing."""
        if getattr(self, "_modify_settings_win", None) is not None:
            try:
                self._modify_settings_win.lift()
                self._modify_settings_win.focus_force()
            except Exception:
                self._modify_settings_win = None
            if self._modify_settings_win is not None:
                return

        win = tk.Toplevel(self)
        self._modify_settings_win = win
        win.title("Modify Settings")
        win.transient(self)
        win.grab_set()
        win.resizable(True, True)

        w, h = 900, 420
        try:
            self.update_idletasks()
            x = self.winfo_rootx() + (self.winfo_width() - w) // 2
            y = self.winfo_rooty() + (self.winfo_height() - h) // 2
            win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            win.geometry(f"{w}x{h}")

        def close_win():
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()
            self._modify_settings_win = None

        win.protocol("WM_DELETE_WINDOW", close_win)

        # Bottom button bar: pack first so it stays visible at bottom
        bottom_btns = ttk.Frame(win, padding=(12, 0))
        bottom_btns.pack(side="bottom", fill="x", pady=(8, 12))

        container = ttk.Frame(win, padding=12)
        container.pack(fill="both", expand=True)

        # --- Scalars (all 5; empty = None on save) ---
        scalars_frame = ttk.LabelFrame(container, text="Scalar settings", padding=8)
        scalars_frame.pack(fill="x", pady=(0, 8))

        scalar_vars = {}
        row_idx = 0
        for key in Job_settings_.JsonKeys:
            val = getattr(Job_settings, key, None)
            ttk.Label(scalars_frame, text=key + ":", width=22, anchor="w").grid(row=row_idx, column=0, sticky="w", padx=(0, 6), pady=2)
            var = tk.StringVar(value="" if val is None else str(val))
            scalar_vars[key] = var
            ttk.Entry(scalars_frame, textvariable=var, width=30).grid(row=row_idx, column=1, sticky="ew", pady=2)
            row_idx += 1
        scalars_frame.grid_columnconfigure(1, weight=1)

        # --- Cfg (config457.json) ---
        cfg_frame = ttk.LabelFrame(container, text="Cfg (config457.json)", padding=8)
        cfg_frame.pack(fill="x", pady=(0, 8))

        cfg_vars = {}
        cfg_row = 0
        for attr in Cfg_.ConfigAttrs:
            val = getattr(Cfg, attr, "")
            ttk.Label(cfg_frame, text=attr + ":", width=18, anchor="w").grid(row=cfg_row, column=0, sticky="w", padx=(0, 6), pady=2)
            var = tk.StringVar(value="" if val is None else str(val))
            cfg_vars[attr] = var
            ttk.Entry(cfg_frame, textvariable=var, width=50).grid(row=cfg_row, column=1, sticky="ew", pady=2)
            cfg_row += 1
        # plc_connected: runtime only (Cfg), read-only in UI
        ttk.Label(cfg_frame, text="plc_connected:", width=18, anchor="w").grid(row=cfg_row, column=0, sticky="w", padx=(0, 6), pady=2)
        plc_status = "OnLine" if getattr(Cfg, "plc_connected", False) else "OffLine"
        plc_connected_lbl = ttk.Label(cfg_frame, text=plc_status, width=50, anchor="w")
        plc_connected_lbl.grid(row=cfg_row, column=1, sticky="w", pady=2)
        cfg_row += 1
        cfg_frame.grid_columnconfigure(1, weight=1)

        def save_form():
            for key in Job_settings_.JsonKeys:
                var = scalar_vars.get(key)
                if var is None:
                    continue
                raw = var.get().strip()
                if key == "current_pack_job":
                    try:
                        setattr(Job_settings, key, int(raw) if raw else None)
                    except ValueError:
                        setattr(Job_settings, key, None)
                else:
                    setattr(Job_settings, key, raw if raw else None)

            for attr in Cfg_.ConfigAttrs:
                var = cfg_vars.get(attr)
                if var is None:
                    continue
                raw = var.get().strip()
                if attr == "PlcPort":
                    try:
                        setattr(Cfg, attr, int(raw) if raw else 502)
                    except ValueError:
                        setattr(Cfg, attr, 502)
                elif attr == "PlcLiveSignIntervalSeconds":
                    try:
                        setattr(Cfg, attr, float(raw) if raw else 2.0)
                    except ValueError:
                        setattr(Cfg, attr, 2.0)
                else:
                    setattr(Cfg, attr, raw if raw else "")

            Cfg.save()

            Job_settings.save()
            if hasattr(self, "_update_t1_packjob_view"):
                self._update_t1_packjob_view()
            if hasattr(self, "hdr_wo_order_num"):
                self.hdr_wo_order_num.config(text=Job_settings.wo_order_number or "")
                self.hdr_wo_part_num.config(text=Job_settings.wo_part_number or "")
                self.hdr_wo_order_qty.config(text=Job_settings.wo_order_quantity or "")
                self.hdr_wo_obj_counter.config(text=Job_settings.wo_object_counter or "")

        def reload_form():
            Job_settings.reload()
            Cfg._load()
            for key in Job_settings_.JsonKeys:
                var = scalar_vars.get(key)
                if var is not None:
                    val = getattr(Job_settings, key, None)
                    var.set("" if val is None else str(val))
            for attr in Cfg_.ConfigAttrs:
                var = cfg_vars.get(attr)
                if var is not None:
                    val = getattr(Cfg, attr, "")
                    var.set("" if val is None else str(val))

        btn_opts = dict(padding=(14, 10))
        ttk.Button(bottom_btns, text="Save", command=save_form, **btn_opts).pack(side="left", expand=True, fill="x", padx=6)
        ttk.Button(bottom_btns, text="Reload", command=reload_form, **btn_opts).pack(side="left", expand=True, fill="x", padx=6)
        ttk.Button(bottom_btns, text="Close", command=close_win, **btn_opts).pack(side="left", expand=True, fill="x", padx=6)

    def _info_block(self, parent, col):
        frame = ttk.Frame(parent, borderwidth=1, relief="solid", padding=8)
        frame.grid(row=0, column=col, padx=10, sticky="nsew")
        ttk.Label(frame, text="Work Order Info", font=("Arial", 10, "bold")).pack(anchor="w", pady=(0, 6))

        t1_table_wrap = ttk.Frame(frame)
        t1_table_wrap.pack(fill="x", pady=(0, 8))

        self.t1_tree = ttk.Treeview(t1_table_wrap, show="headings", selectmode="none", height=4)
        self.t1_tree.pack(side="left", fill="both", expand=True)

        t1_scroll = ttk.Scrollbar(t1_table_wrap, orient="vertical", command=self.t1_tree.yview)
        self.t1_tree.configure(yscrollcommand=t1_scroll.set)
        t1_scroll.pack(side="right", fill="y")

        self.t1_tree.tag_configure("highlight", background="yellow")

        # --- Image holder (same behavior style as analysis/thickness) ---
        bottom = ttk.Frame(frame, borderwidth=2, relief="sunken")
        bottom.pack(fill="both", expand=True)

        self.info_img_lbl = ttk.Label(bottom, text="(No image)")
        self.info_img_lbl.pack(expand=True)

        return frame

    def _update_t1_packjob_view(self):
        if not hasattr(self, "t1_tree"):
            return

        for iid in self.t1_tree.get_children():
            self.t1_tree.delete(iid)

        df = Job_settings.pack_jobs

        if df is None or df.empty:
            self.t1_tree["columns"] = []
            if hasattr(self, "info_img_lbl"):
                self.info_img_lbl.configure(text="(No pack-jobs data)", image="")
                self.info_img_lbl.image = None
            return

        if df.shape[1] < 5:
            self.t1_tree["columns"] = []
            if hasattr(self, "info_img_lbl"):
                self.info_img_lbl.configure(text="(Pack-jobs Excel must have >= 5 columns)", image="")
                self.info_img_lbl.image = None
            return

        headers = [str(c) for c in df.columns[:4]]
        self.t1_tree["columns"] = list(range(4))
        for i, h in enumerate(headers):
            self.t1_tree.heading(i, text=h)
            self.t1_tree.column(i, width=110, stretch=True, anchor="w")

        try:
            raw = getattr(Job_settings, "current_pack_job", None)
            highlight_idx = int(raw) if raw is not None and str(raw).strip() != "" else 0
        except (TypeError, ValueError):
            highlight_idx = 0
        max_rows = min(len(df), 4)
        highlight_idx = max(0, min(highlight_idx, max_rows - 1)) if max_rows else 0

        for r in range(max_rows):
            vals = [str(df.iat[r, c]) for c in range(4)]
            tags = ("highlight",) if r == highlight_idx else ()
            self.t1_tree.insert("", "end", values=vals, tags=tags)

        self.t1_tree.update_idletasks()

        def _show_pil1_fallback():
            """When pack-job has no valid image, show engraving (pil1) instead of 'not found'."""
            if not hasattr(self, "info_img_lbl"):
                return
            if Job_settings.pil1 is not None:
                Job_settings.img1 = ImageTk.PhotoImage(Job_settings.pil1)
                self.info_img_lbl.configure(image=Job_settings.img1, text="")
                self.info_img_lbl.image = Job_settings.img1
            else:
                self.info_img_lbl.configure(text="(No image)", image="")
                self.info_img_lbl.image = None

        img_path = str(df.iat[highlight_idx, 4]) if 0 <= highlight_idx < len(df) and df.shape[1] > 4 else ""
        if not img_path or str(img_path).lower() == "nan":
            _show_pil1_fallback()
            return

        img_path = os.path.normpath(img_path)
        if not os.path.isabs(img_path):
            img_path = os.path.normpath(os.path.join(os.getcwd(), img_path))
        if not os.path.exists(img_path):
            _show_pil1_fallback()
            return

        try:
            pil = Image.open(img_path)
            pil = pil.resize((220, 220), Image.LANCZOS)
            self._info_pack_photo = ImageTk.PhotoImage(pil)
            self.info_img_lbl.configure(image=self._info_pack_photo, text="")
            self.info_img_lbl.image = self._info_pack_photo
        except Exception:
            _show_pil1_fallback()

    def _analysis_block(self, parent, col):
        frame = ttk.Frame(parent, borderwidth=1, relief="solid", padding=5)
        frame.grid(row=0, column=col, padx=10, sticky="nsew")
        ttk.Label(frame, text="Image Analysis", font=("Arial", 10, "bold")).pack(anchor="w", pady=(0, 6))

        status_box = ttk.Frame(frame, borderwidth=1, relief="solid", padding=6)
        status_box.pack(fill="x", pady=(0, 8))
        self.analysis_status_lbl = ttk.Label(status_box, text="👍 / 👎", font=("Arial", 14))
        self.analysis_status_lbl.pack(anchor="center")

        img_holder = ttk.Frame(frame, borderwidth=2, relief="sunken")
        img_holder.pack(fill="both", expand=True)
        self.analysis_live_img_lbl = ttk.Label(img_holder)
        self.analysis_live_img_lbl.pack(expand=True)
        self.analysis_img_lbl = ttk.Label(img_holder)
        self.analysis_img_lbl.pack(expand=True)

        return frame

    def _thickness_block(self, parent, col):
        outer = ttk.Frame(parent, borderwidth=1, relief="solid", padding=5)
        outer.grid(row=0, column=col, padx=10, sticky="nsew")

        title_row = ttk.Frame(outer)
        title_row.pack(fill="x", pady=(0, 6))

        ttk.Label(title_row, text="Thickness Measurement", font=("Arial", 10, "bold")).pack(side="left")

        big = ttk.Frame(outer, borderwidth=2, relief="sunken")
        big.pack(fill="both", expand=True)

        self.thk_img_lbl = ttk.Label(big)
        self.thk_img_lbl.pack(expand=True, fill="both")

        return outer

    def _on_refresh(self):
        Job_settings.wo_order_number = "Default0"
        Job_settings.wo_part_number = "Default1"
        Job_settings.wo_order_quantity = "Default2"
        Job_settings.wo_object_counter = "Default3"
        self._refresh_from_Job_settings()

    def _on_close(self):
        global RegsToPlc
        RegsToPlc[1] = 3  # stop conveyor
        RegsToPlc[2] = 1  ##turn green light off
        RegsToPlc[3] = 1  ##turn white light off
        time.sleep(0.2)
        Job_settings.stop_event.set()
        self.destroy()

    def _update_plc_indicator(self):
        if not hasattr(self, "plc_text_lbl"):
            return

        connected = bool(getattr(Cfg, "plc_connected", False))
        status_text = "PLC: OnLine" if connected else "PLC: OffLine"
        self.plc_text_lbl.config(text=status_text)

        tk_img = self.tk_plc_on if connected else self.tk_plc_off

        # update only when state changes
        if getattr(self, "_plc_last_state", None) != connected:
            self.plc_icon_lbl.config(image=tk_img)
            self.plc_icon_lbl.image = tk_img  # keep reference!
            self._plc_last_state = connected

    # ----------------------------
    # Thickness calibration window (auto-opens during a calibration sequence)
    # ----------------------------
    # phase -> (display text, color). Driven by Job_settings.thk_cal["phase"] (set by T3).
    _CAL_PHASES = {
        "measuring_g1": ("מודד מדיד 1…", "#000000"),
        "g1_done":      ("מדיד 1 נמדד — ממתין למדיד 2", "#000000"),
        "measuring_g2": ("מודד מדיד 2…", "#000000"),
        "g2_done":      ("מדיד 2 נמדד — ממתין לחישוב", "#000000"),
        "computing":    ("מחשב כיול…", "#000000"),
        "done_ok":      ("כיול הושלם בהצלחה ✓", "#127a1f"),
        "done_fail":    ("כיול נכשל ✗", "#b00000"),
        "view":         ("כיול נוכחי", "#000000"),
        "idle":         ("", "#000000"),
    }

    def _build_thk_cal_window(self):
        win = tk.Toplevel(self)
        self._thk_cal_win = win
        win.title("Thickness Calibration")
        win.transient(self)
        win.resizable(False, False)

        w, h = 460, 320
        try:
            self.update_idletasks()
            x = self.winfo_rootx() + (self.winfo_width() - w) // 2
            y = self.winfo_rooty() + (self.winfo_height() - h) // 2
            win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            win.geometry(f"{w}x{h}")

        def _close():
            # Mark inactive so the refresh loop won't immediately reopen it.
            cal = dict(getattr(Job_settings, "thk_cal", {}) or {})
            cal["active"] = False
            Job_settings.thk_cal = cal
            try:
                win.destroy()
            except Exception:
                pass
            self._thk_cal_win = None

        win.protocol("WM_DELETE_WINDOW", _close)

        container = ttk.Frame(win, padding=14)
        container.pack(fill="both", expand=True)

        # Big phase/status line
        self._cal_phase_lbl = ttk.Label(container, text="", font=("Arial", 16, "bold"))
        self._cal_phase_lbl.pack(anchor="center", pady=(0, 12))

        grid = ttk.LabelFrame(container, text="Measurements", padding=10)
        grid.pack(fill="x")

        self._cal_g1_var = tk.StringVar(value="—")
        self._cal_g2_var = tk.StringVar(value="—")
        self._cal_result_var = tk.StringVar(value="—")

        def _row(parent, r, title, var):
            ttk.Label(parent, text=title, width=14, anchor="w").grid(row=r, column=0, sticky="w", pady=3)
            ttk.Label(parent, textvariable=var, font=("Consolas", 12)).grid(row=r, column=1, sticky="w", pady=3)

        _row(grid, 0, "Gauge 1:", self._cal_g1_var)
        _row(grid, 1, "Gauge 2:", self._cal_g2_var)
        _row(grid, 2, "Result:", self._cal_result_var)
        grid.grid_columnconfigure(1, weight=1)

        self._cal_msg_lbl = ttk.Label(container, text="", foreground="#444444", wraplength=420)
        self._cal_msg_lbl.pack(anchor="w", pady=(10, 0))

        ttk.Button(container, text="Close", command=_close, padding=(14, 8)).pack(side="bottom", pady=(12, 0))

        win.lift()

    def _open_view_calibration(self):
        """Operator-triggered: read the last saved calibration from JSON, populate the
        shared view, and open the window. If a calibration sequence later starts on the
        PLC, the same window will receive the live updates automatically."""
        path = os.path.join("config", "config457_thk_hwts.json")
        saved = {}
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    saved = (json.load(f) or {}).get("calibration", {}) or {}
            except Exception:
                saved = {}

        a = float(saved.get("a", 1.0))
        b_mm = float(saved.get("b", 0.0))
        ts = saved.get("last_calibration_iso")
        view = {
            "active": False, "phase": "view",
            "g1_raw_mm": saved.get("raw_g1_mm"), "g1_std_um": None,
            "g2_raw_mm": saved.get("raw_g2_mm"), "g2_std_um": None,
            "a": a, "b_um": b_mm * 1000.0,
            "message": f"Last calibration: {ts}" if ts else "No calibration applied yet (defaults a=1, b=0).",
        }
        Job_settings.thk_cal = view

        if getattr(self, "_thk_cal_win", None) is None or not self._thk_cal_win.winfo_exists():
            self._build_thk_cal_window()
        self._update_thk_cal_window()
        try:
            self._thk_cal_win.lift()
            self._thk_cal_win.focus_force()
        except Exception:
            pass

    def _update_thk_cal_window(self):
        cal = getattr(Job_settings, "thk_cal", None)
        if not cal:
            return

        win = getattr(self, "_thk_cal_win", None)
        win_open = win is not None and win.winfo_exists()

        # Auto-open only when a calibration sequence is actually running.
        # View mode (button) opens the window itself before calling here.
        if not win_open:
            if cal.get("active"):
                self._build_thk_cal_window()
            else:
                return

        def _fmt_gauge(raw, std):
            if raw is None:
                return "—"
            s = f"{float(raw):.4f} mm"
            if std is not None:
                s += f"   (±{float(std):.2f} µm)"
            return s

        phase = cal.get("phase", "idle")
        text, color = self._CAL_PHASES.get(phase, ("", "#000000"))
        self._cal_phase_lbl.config(text=text, foreground=color)

        self._cal_g1_var.set(_fmt_gauge(cal.get("g1_raw_mm"), cal.get("g1_std_um")))
        self._cal_g2_var.set(_fmt_gauge(cal.get("g2_raw_mm"), cal.get("g2_std_um")))

        a, b_um = cal.get("a"), cal.get("b_um")
        if a is not None and b_um is not None:
            self._cal_result_var.set(f"gain a={float(a):.4f}   offset b={float(b_um):+.1f} µm")
        else:
            self._cal_result_var.set("—")

        # Tint the message red only on a failure phase; otherwise keep it neutral.
        msg_color = "#b00000" if cal.get("phase") == "done_fail" else "#444444"
        self._cal_msg_lbl.config(text=str(cal.get("message") or ""), foreground=msg_color)

    def _refresh_from_Job_settings(self):
        try:
            self.hdr_wo_order_num.config(text=str(getattr(Job_settings, 'wo_order_number', '')))
            self.hdr_wo_part_num.config(text=str(getattr(Job_settings, 'wo_part_number', '')))
            self.hdr_wo_order_qty.config(text=str(getattr(Job_settings, 'wo_order_quantity', '')))
            self.hdr_wo_obj_counter.config(text=str(getattr(Job_settings, 'wo_object_counter', '')))

            self._update_plc_indicator()

            # Snapshot each shared PIL once (the producing threads may swap it mid-refresh).
            pil1 = Job_settings.pil1
            if pil1 is not None and hasattr(self, "info_img_lbl"):
                Job_settings.img1 = ImageTk.PhotoImage(pil1)
                self.info_img_lbl.configure(image=Job_settings.img1, text="")
                self.info_img_lbl.image = Job_settings.img1

            pil2 = Job_settings.pil2
            if pil2 is not None and hasattr(self, "analysis_img_lbl"):
                Job_settings.img2 = ImageTk.PhotoImage(pil2)
                self.analysis_img_lbl.configure(image=Job_settings.img2)
                self.analysis_img_lbl.image = Job_settings.img2

            pil22 = Job_settings.pil22
            if pil22 is not None and hasattr(self, "analysis_live_img_lbl"):
                Job_settings.img22 = ImageTk.PhotoImage(pil22)
                self.analysis_live_img_lbl.configure(image=Job_settings.img22)
                self.analysis_live_img_lbl.image = Job_settings.img22

            pil3 = Job_settings.pil3
            if pil3 is not None and hasattr(self, "thk_img_lbl"):
                Job_settings.img3 = ImageTk.PhotoImage(pil3)
                self.thk_img_lbl.configure(image=Job_settings.img3)
                self.thk_img_lbl.image = Job_settings.img3

            if hasattr(self, 't1_tree'):
                try:
                    self._update_t1_packjob_view()
                except Exception:
                    pass

            self._update_thk_cal_window()
        except Exception as e:
            log(f"UI refresh error: {e}", "WARN")
        finally:
            # Always reschedule, so a single failed frame can never freeze the UI.
            self.after(200, self._refresh_from_Job_settings)


# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    t1 = threading.Thread(target=T1, daemon=True)
    t2 = threading.Thread(target=T2, daemon=True)
    t3 = threading.Thread(target=T3_thickness, daemon=True)
    t4 = threading.Thread(target=T4_plc_flush, daemon=True)

    t4.start()
    t1.start()
    t2.start()
    t3.start()


    app = EL_UI()
    app.mainloop()
    Job_settings.stop_event.set()


