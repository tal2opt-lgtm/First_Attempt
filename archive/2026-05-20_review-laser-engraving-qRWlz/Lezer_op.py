# laser_min.py
import json, sys, subprocess, time
from pathlib import Path
import win32gui, win32con, win32process
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget, QLabel, QLineEdit, QPushButton, QGridLayout, QComboBox
from PySide6.QtGui import QPixmap

INKSCAPE=r"C:\Program Files\Inkscape\bin\inkscape.com"
DEFAULT_SVG=r"C:\Users\lz1\AppData\Roaming\inkscape\templates\default.svg"
EZCAD_EXE=r"C:\Users\lz1\Desktop\LAZER\EzCad_program\Program\EzCad2.exe"
TITLE_SUB="EzCad-Lite  - No title"

IMPORT_ID=32807; PATH_EDIT_ID=1152; OPEN_BTN_ID=1; USE_DEFAULT_ID=3032
POWER_EDIT_ID=1432; LOOPS_EDIT_ID=1009; SIZE_X_EDIT_ID=1119; SIZE_Y_EDIT_ID=1120
MAIN_APPLY_ID=1105
TRANSFORM_CMD_ID=32818; ROTATION_BTN_ID=1261; ANGLE_EDIT_ID=1390; TRANSFORM_APPLY_ID=1105
POS_X_EDIT_ID=1066; POS_Y_EDIT_ID=1081 ; RED_LIGHT_CMD_ID=17023

def _enum_windows_of_pid(pid):
    a=[]; win32gui.EnumWindows(lambda h,_: a.append(h) if win32process.GetWindowThreadProcessId(h)[1]==pid else None, None); return a

def find_in_pid_by_id(pid, cid):
    for top in _enum_windows_of_pid(pid):
        out=[0]
        def c(h,_):
            try:
                if win32gui.GetDlgCtrlID(h)==cid: out[0]=h; return False
            except: pass
            return True
        try: win32gui.EnumChildWindows(top, c, None)
        except: pass
        if out[0]: return out[0]
    return 0

def find_main_by_title_sub(sub, timeout=8):
    t=time.time()
    while time.time()-t<timeout:
        got=[0]
        def cb(h,_):
            if win32gui.IsWindowVisible(h) and sub.lower() in win32gui.GetWindowText(h).lower(): got[0]=h
        win32gui.EnumWindows(cb,None)
        if got[0]: return got[0]
        time.sleep(0.2)
    return 0

def find_open_dialog_for_pid(pid, timeout=8):
    t=time.time()
    while time.time()-t<timeout:
        dlg=[0]
        def cb(h,_):
            if win32gui.IsWindowVisible(h) and win32gui.GetClassName(h)=="#32770" and win32process.GetWindowThreadProcessId(h)[1]==pid:
                try: win32gui.GetDlgItem(h, PATH_EDIT_ID); win32gui.GetDlgItem(h, OPEN_BTN_ID); dlg[0]=h
                except: pass
        win32gui.EnumWindows(cb,None)
        if dlg[0]: return dlg[0]
        time.sleep(0.2)
    return 0

def set_edit_value(h, v):
    if not h: return
    win32gui.SendMessage(h, win32con.WM_SETTEXT, 0, str(v))
    p=win32gui.GetParent(h); cid=win32gui.GetDlgCtrlID(h)&0xFFFF
    win32gui.PostMessage(p, win32con.WM_COMMAND, (win32con.EN_CHANGE<<16)|cid, h)
    win32gui.PostMessage(p, win32con.WM_COMMAND, (win32con.EN_KILLFOCUS<<16)|cid, h)

def find_transform_dialog(pid, timeout=2.0):
    t=time.time()
    while time.time()-t<timeout:
        out=[0]
        def cb(h,_):
            if win32gui.IsWindowVisible(h) and win32gui.GetClassName(h)=="#32770" and win32process.GetWindowThreadProcessId(h)[1]==pid:
                if "transform" in win32gui.GetWindowText(h).lower(): out[0]=h
        win32gui.EnumWindows(cb,None)
        if out[0]: return out[0]
        time.sleep(0.1)
    return 0

def close_transform_dialog(pid):
    dlg=find_transform_dialog(pid,1.5)
    if not dlg: return
    for bid in (1,2):
        try: b=win32gui.GetDlgItem(dlg,bid);
        except: b=0
        if b: win32gui.SendMessage(b, win32con.BM_CLICK, 0, 0); time.sleep(0.05)
    win32gui.PostMessage(dlg, win32con.WM_SYSCOMMAND, win32con.SC_CLOSE, 0)
    time.sleep(0.04); win32gui.PostMessage(dlg, win32con.WM_CLOSE, 0, 0)

def build_svg(t1,t2,x,y,h,out_svg):
    c=Path(DEFAULT_SVG).read_text(encoding="utf-8"); gap=h*1.2
    L=[]
    if (t1 or "")!="": L.append(f'      <text x="0" y="0" font-family="CNC Vector" font-size="{h}px">{t1}</text>')
    if (t2 or "")!="": L.append(f'      <text x="0" y="{gap:.1f}" font-family="CNC Vector" font-size="{h}px">{t2}</text>')
    inj=f'\n  <g>\n    <g transform="translate({x},{y})">\n'+"\n".join(L)+'\n    </g>\n  </g>\n'
    pos=c.lower().rfind("</svg>"); Path(out_svg).write_text(c[:pos]+inj+c[pos:], encoding="utf-8")

def svg_to_dxf(svg,dxf):
    subprocess.run([INKSCAPE, str(svg), "--export-type=dxf","--export-text-to-path", f"--export-filename={dxf}"], check=True)

def run_flow(jp:Path):
    print(f"[START] Reading config: {jp}")
    d=json.loads(jp.read_text(encoding="utf-8"))
    t1=d.get("text1") or ""; t2=d.get("text2") or ""
    x=float(d["x"]); y=float(d["y"]); h=float(d["height"]); rot=int(d.get("rotation") or 0)
    pwr=d.get("power"); lps=d.get("loops"); sx=d.get("size_x"); sy=d.get("size_y")
    print(f"[OK] Config loaded — text1='{t1}' text2='{t2}' x={x} y={y} h={h} rot={rot} pwr={pwr} lps={lps} sx={sx} sy={sy}")
    svg=jp.with_suffix(".svg"); dxf=jp.with_suffix(".dxf")

    print("[...] Building SVG...")
    try:
        build_svg(t1,t2,x,y,h,svg)
        print(f"[OK] SVG created: {svg}")
    except Exception as e:
        print(f"[ERROR] SVG build failed: {e}"); return

    print("[...] Converting SVG to DXF via Inkscape...")
    try:
        svg_to_dxf(svg,dxf)
        print(f"[OK] DXF created: {dxf}")
    except Exception as e:
        print(f"[ERROR] DXF conversion failed: {e}"); return

    print("[...] Launching EzCad2...")
    proc=subprocess.Popen(EZCAD_EXE); time.sleep(1.2)
    print(f"[OK] EzCad2 launched (PID={proc.pid})")

    print("[...] Waiting for EzCad2 main window...")
    main=find_main_by_title_sub(TITLE_SUB)
    if not main:
        print(f"[ERROR] Main window not found (title: '{TITLE_SUB}')"); return
    print(f"[OK] Main window found (handle={main})")

    print("[...] Sending Import command...")
    win32gui.PostMessage(main, win32con.WM_COMMAND, IMPORT_ID, 0)

    print("[...] Waiting for Open dialog...")
    dlg=find_open_dialog_for_pid(proc.pid)
    if not dlg:
        print("[ERROR] Open dialog not found"); return
    print(f"[OK] Open dialog found (handle={dlg})")

    win32gui.SendMessage(win32gui.GetDlgItem(dlg, PATH_EDIT_ID), win32con.WM_SETTEXT, 0, str(dxf))
    print(f"[OK] DXF path set: {dxf}")
    win32gui.SendMessage(win32gui.GetDlgItem(dlg, OPEN_BTN_ID), win32con.BM_CLICK, 0, 0)
    print("[OK] Open button clicked")
    time.sleep(0.6)

    use_def=find_in_pid_by_id(proc.pid, USE_DEFAULT_ID)
    if use_def:
        win32gui.SendMessage(use_def, win32con.BM_CLICK, 0, 0); time.sleep(0.12)
        print("[OK] 'Use Default' button clicked")
    else:
        print("[WARN] 'Use Default' button not found — skipped")

    if rot in (0,90):
        print(f"[...] Opening Transform dialog for rotation {rot}°...")
        win32gui.PostMessage(main, win32con.WM_COMMAND, TRANSFORM_CMD_ID, 0); time.sleep(0.22)
        rb=find_in_pid_by_id(proc.pid, ROTATION_BTN_ID)
        if rb:
            win32gui.SendMessage(rb, win32con.BM_CLICK, 0, 0); time.sleep(0.10)
            print("[OK] Rotation radio button selected")
        else:
            print("[WARN] Rotation radio button not found")
        ae=find_in_pid_by_id(proc.pid, ANGLE_EDIT_ID)
        if ae:
            set_edit_value(ae, str(rot))
            print(f"[OK] Angle set to {rot}°")
            parent=win32gui.GetParent(ae)
            set_edit_value(win32gui.GetDlgItem(parent, POS_X_EDIT_ID), "0")
            set_edit_value(win32gui.GetDlgItem(parent, POS_Y_EDIT_ID), "0")
            print("[OK] Position X/Y set to 0")
            ap=win32gui.GetDlgItem(parent, TRANSFORM_APPLY_ID)
            if ap:
                win32gui.SendMessage(ap, win32con.BM_CLICK, 0, 0); time.sleep(0.12)
                print("[OK] Transform Apply clicked")
            else:
                print("[WARN] Transform Apply button not found")
        else:
            print("[WARN] Angle edit not found — rotation not applied")
        close_transform_dialog(proc.pid)
        print("[OK] Transform dialog closed")

    if pwr not in (None,""):
        h_pwr=find_in_pid_by_id(proc.pid, POWER_EDIT_ID)
        if h_pwr: set_edit_value(h_pwr, str(pwr)); print(f"[OK] Power set to {pwr}")
        else: print("[WARN] Power field not found")
    if lps not in (None,""):
        h_lps=find_in_pid_by_id(proc.pid, LOOPS_EDIT_ID)
        if h_lps: set_edit_value(h_lps, str(lps)); print(f"[OK] Loops set to {lps}")
        else: print("[WARN] Loops field not found")
    if sx not in (None,""):
        h_sx=find_in_pid_by_id(proc.pid, SIZE_X_EDIT_ID)
        if h_sx: set_edit_value(h_sx, str(sx)); print(f"[OK] Size X set to {sx}")
        else: print("[WARN] Size X field not found")
    if sy not in (None,""):
        h_sy=find_in_pid_by_id(proc.pid, SIZE_Y_EDIT_ID)
        if h_sy: set_edit_value(h_sy, str(sy)); print(f"[OK] Size Y set to {sy}")
        else: print("[WARN] Size Y field not found")
    time.sleep(3)

    sx_h=find_in_pid_by_id(proc.pid, SIZE_X_EDIT_ID); sy_h=find_in_pid_by_id(proc.pid, SIZE_Y_EDIT_ID)
    try:
        if sx_h: win32gui.SetFocus(sx_h); time.sleep(0.02)
        if sy_h: win32gui.SetFocus(sy_h); time.sleep(0.02)
    except: pass

    panel = win32gui.GetParent(sx_h) if sx_h else (win32gui.GetParent(sy_h) if sy_h else 0)
    if panel:
        ap_in_panel=win32gui.GetDlgItem(panel, MAIN_APPLY_ID)
        if ap_in_panel:
            win32gui.SendMessage(ap_in_panel, win32con.BM_CLICK, 0, 0); time.sleep(0.05)
            cid=win32gui.GetDlgCtrlID(ap_in_panel)&0xFFFF
            win32gui.PostMessage(panel, win32con.WM_COMMAND, (win32con.BN_CLICKED<<16)|cid, ap_in_panel)
            time.sleep(0.06)
            print("[OK] Main Apply clicked")
        else:
            print("[WARN] Main Apply button not found in panel")
    else:
        print("[WARN] Apply panel not found — size/position may not be committed")

    print("[...] Looking for Red Light button...")
    red_btn = find_in_pid_by_id(proc.pid, RED_LIGHT_CMD_ID)
    if red_btn:
        win32gui.SendMessage(red_btn, win32con.BM_CLICK, 0, 0)
        print("[OK] Red Light button clicked")
    else:
        print("[ERROR] Red Light button not found")
    time.sleep(0.2)
    print("[DONE] Flow completed.")
class UI(QWidget):
    def __init__(self):
        super().__init__(); g=QGridLayout(self)
        L=QLabel(); lp=Path(__file__).resolve().parent/"logo.png"
        if lp.exists():
            p=QPixmap(str(lp))
            if not p.isNull(): L.setPixmap(p); L.setAlignment(Qt.AlignCenter)
        g.addWidget(L,0,0,1,2)
        self.t1=QLineEdit(); g.addWidget(QLabel("TEXT 1"),1,0); g.addWidget(self.t1,1,1)
        self.t2=QLineEdit(); g.addWidget(QLabel("TEXT 2"),2,0); g.addWidget(self.t2,2,1)
        self.x=QLineEdit("-10"); self.x.setReadOnly(True); self.x.setFocusPolicy(Qt.NoFocus)
        g.addWidget(QLabel("X (fixed)"),3,0); g.addWidget(self.x,3,1)
        self.y=QLineEdit("297"); self.y.setReadOnly(True); self.y.setFocusPolicy(Qt.NoFocus)
        g.addWidget(QLabel("Y (fixed)"),4,0); g.addWidget(self.y,4,1)
        self.h=QLineEdit("4"); self.h.setReadOnly(True); self.h.setFocusPolicy(Qt.NoFocus)
        g.addWidget(QLabel("HEIGHT (fixed)"),5,0); g.addWidget(self.h,5,1)
        self.l=QLineEdit(); g.addWidget(QLabel("LOOPS"),6,0); g.addWidget(self.l,6,1)
        self.p=QLineEdit(); g.addWidget(QLabel("POWER"),7,0); g.addWidget(self.p,7,1)
        self.sx=QLineEdit(); g.addWidget(QLabel("SIZE X"),8,0); g.addWidget(self.sx,8,1)
        self.sy=QLineEdit(); g.addWidget(QLabel("SIZE Y"),9,0); g.addWidget(self.sy,9,1)
        self.rot=QComboBox(); self.rot.addItems(["0","90"])
        g.addWidget(QLabel("ROTATION (deg)"),10,0); g.addWidget(self.rot,10,1)
        btn=QPushButton("Save & Run"); btn.clicked.connect(self.go); g.addWidget(btn,11,0,1,2)
    def go(self):
        data={"text1":self.t1.text(),"text2":self.t2.text(),"x":self.x.text(),"y":self.y.text(),
              "height":self.h.text(),"loops":self.l.text(),"power":self.p.text(),
              "size_x":self.sx.text(),"size_y":self.sy.text(),"rotation":self.rot.currentText()}
        jp=Path("config_temp.json"); jp.write_text(json.dumps(data,indent=2,ensure_ascii=False),encoding="utf-8")
        self.close(); run_flow(jp)

if __name__=="__main__":
    app=QApplication(sys.argv); w=UI(); w.show(); sys.exit(app.exec())
