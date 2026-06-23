import cv2 as cv2
import numpy as np
import math
import traceback
import pandas as pd
import os
import json
import subprocess
import time
import threading
from pyModbusTCP.client import ModbusClient
import queue
import customtkinter as ctk
from tkinter import messagebox, filedialog
from PIL import Image, ImageTk
import matplotlib.pyplot as plt


# המרת אורך מפיקסלים ל מילמטר
def to_pixel(millimeter):
    try:
        return int(millimeter / Cfg_mv.resolution + 0.51)
    except:
        return 0

# המרת אורך מ מ"מ ל פיקסלים
def to_mm(pixels):
    return round(pixels * Cfg_mv.resolution, 2)

def to_mmXY(pxlX, pxlY, imgb):
    x = to_mm(pxlX * 1.0 - 1.0 * imgb.shape[1] / 2)
    y = to_mm(pxlY * 1.0 - 1.0 * imgb.shape[0] / 2)
    return x, y

def to_pixelXY(mmx, mmy, imgb):
    x = min(to_pixel(mmx) + int(imgb.shape[1] / 2), imgb.shape[1])
    y = min(to_pixel(mmy) + int(imgb.shape[0] / 2), imgb.shape[0])
    return x, y

def to_pixelXY2(mmx, mmy, imgp2):
    x = min(to_pixel(mmx) + int(imgp2.shape[1] / 2), imgp2.shape[1])
    y = min(to_pixel(mmy) + int(imgp2.shape[0] / 2), imgp2.shape[0])
    return x, y

def cos(a):
    return math.cos(math.radians(a))

def sin(a):
    return math.sin(math.radians(a))

def GetSpec(spec):
    global Lugs2, Grooves2, Body, JobNbr, Alerts, Holes2, Lug2

    def check_loc(alpha, H_Diameter):
        if alpha > 0:
            rec_check = 1

        elif alpha < 0:
            rec_check = -1
            alpha *= -1
        else:
            return Cfg_mv.offsetx
        r = H_Diameter / 2 - 2
        l = Cfg_mv.Camera_Distance

        if rec_check == 1:  # angle from bottom PL
            loc_y = (r * cos(alpha) - r * Cfg_mv.offsetx * sin(alpha) / l) * l / (l + r * sin(alpha))
            print('loc_y', loc_y)
            return loc_y

        elif rec_check == -1:  # angle from top PL (lug side)
            loc_y = -(r * cos(alpha) + r * Cfg_mv.offsetx * sin(alpha) / l) * l / (l + r * sin(alpha))
            print('loc_y', loc_y)
            return loc_y
        else:

            check_loc(alpha, H_Diameter)

    Body = body_()
    Lugs2 = []
    Holes2 = []
    Grooves = []
    Grooves2 = []
    Alerts = []
    Err = 110

    for i, r in spec.iterrows():
        if r.object.strip() == 'Body':
            Body.tp = r.p1
            Body.diam = r.p2
            Body.W = r.p3
            Body.T = r.p4
            Body.H = to_pixel(r.p5)  ##inspection location
            if pd.notna(r.JobNbr):
                Cfg_mv.JobNbr = str(r.JobNbr)
            else:
                Cfg_mv.JobNbr = 'NoJob'

            Err -= 10
            if Body.tp < 1 or Body.diam < 25 or Body.W < 12 or Body.T < 10 or Body.H > Body.T - 2:
                Err = Err + 10
        elif r.object.strip() == 'Lug':
            # Lug2=Lug2_()
            Lugs2.append(Lug2_())
            j = len(Lugs2) - 1
            Lugs2[j].tp = r.p1
            Lugs2[j].W.workspec = r.p2  # width
            Lugs2[j].W.Spec = r.p2
            Lugs2[j].H.workspec = r.p3  # height
            Lugs2[j].H.Spec = r.p3
            Lugs2[j].Y.workspec = r.p4 + r.p2 / 2  # location
            Lugs2[j].Y.Spec = r.p4 + r.p2 / 2
            Lugs2[j].W.worktolerance = r.p5  # +Lug_tol[0]
            Lugs2[j].W.Tolerance = r.p5  # +Lug_tol[0]
            Lugs2[j].H.worktolerance = r.p6  # +Lug_tol[1]
            Lugs2[j].H.Tolerance = r.p6  # +Lug_tol[1]
            Lugs2[j].Y.worktolerance = r.p7  # +Lug_tol[2]
            Lugs2[j].Y.Tolerance = r.p7  # +Lug_tol[2]
            # Lugs2.append(Lug2_())

        elif r.object.strip() == 'Hole':
            Holes2.append(Hole2_())
            i = len(Holes2) - 1
            Holes2[i].tp = r.p1  # 'p1 Type 1 round/2 Xslot  /3 Yslot /4 round+Xslot'
            Holes2[i].W.workspec = r.p3  # 'p1 W  B-T  mm (Vertical lendth)'
            # Holes2[i].L.workspec = round(r.p2 * (Body.diam / 2 - abs(r.p4) * 0.7) / (Body.diam / 2),1)  # 'p2L  L-R mm (horizontal Length)'
            if r.p4 == 0:
                Holes2[i].L.workspec = r.p2
                Holes2[i].L.workspecorg = r.p2
            else:
                Holes2[i].L.workspec = round(r.p2 * sin(abs(r.p4)))
                Holes2[i].L.workspecorg = r.p2

            Holes2[i].X.workspec = r.p4  # 'p4X mm (center)'
            Holes2[i].Y.workspec = r.p5  # 'p5Y mm (center'
            Holes2[i].W.worktolerance = r.p6  # +Holes_tol[0]  # 'p6W
            Holes2[i].L.worktolerance = r.p7  # +Holes_tol[1]  # 'p7L '
            Holes2[i].Y.worktolerance = r.p8  # +Holes_tol[2] # 'p8XY '
            Holes2[i].X.worktolerance = r.p8  # +Holes_tol[2]  # 'p8XY '
        elif r.object.strip() == 'Groove':
            Grooves2.append(groove2_())
            i = len(Grooves2) - 1
            Grooves2[i].WR.workspec = r.p2
            Grooves2[i].WL.workspec = r.p2
            Grooves2[i].TL.workspec = r.p3
            Grooves2[i].TR.workspec = r.p3
            Grooves2[i].Y.workspec = r.p4
            Grooves2[i].WR.worktolerance = r.p5  # +Groove_tol[0]
            Grooves2[i].WL.worktolerance = r.p5  # +Groove_tol[0]
            Grooves2[i].TR.worktolerance = r.p6  # +Groove_tol[1]
            Grooves2[i].TL.worktolerance = r.p6  # +Groove_tol[1]
            Grooves2[i].Y.worktolerance = r.p7  # +Groove_tol[2]
        else:
            Err = Err + 1

    if not Grooves2:  ##temp
        Grooves2.append(groove2_())
        i = len(Grooves2) - 1
        Grooves2[i].WR.workspec = 0
        Grooves2[i].WL.workspec = 0
        Grooves2[i].TL.workspec = 0
        Grooves2[i].TR.workspec = 0
        Grooves2[i].Y.workspec = 0
        Grooves2[i].WR.worktolerance = 0
        Grooves2[i].WL.worktolerance = 0
        Grooves2[i].TR.worktolerance = 0
        Grooves2[i].TL.worktolerance = 0
        Grooves2[i].Y.worktolerance = 0

    for h in Holes2:
        if Cfg_mv.lug_side == 1:
            h.X.workspec = h.X.workspec * -1
        h.X.workspec = check_loc(h.X.workspec, Body.diam)

    return Body, Grooves, Err

def analyze_img():
    def a():
        pass
    def b():
        pass

    img_g=img.img_G.copy()
    # img_w=
    # Crop G,W images
    # Alline Prspective vew to Izometric view
    #detect Lug on G img
    #detect hold
    #detect Groves
    #detect Deffiect
    #Build img_to_show
    #clac Okk

class Config():
    """Config with fixed members from defaults; access as cfg.resolution, cfg.delta, etc."""

    def __init__(self):
        # Fixed list of members (defaults); all accessible as self.<member>
        defaults = {
            "resolution": 0.0650,
            "Y0": 250,
            "delta": 3.0,
            "CameraID": 0,
            "workstationID": 'MV-Endline-02',
            "CameraXpixel": 1920,
            "CameraYpixel": 1080,
            "CameraRotation": 0,
            "CropX0": 100,
            "CropX1": 100,
            "CropY0": 50,
            "CropY1": 50,
            "marginX": 100,
            "marginY": 100,
            "ObjectDistance": 550,
            "Lens": 25,
            "offsetx": 1.0,
            "Camera_Distance": 440,
            "BCK_color": [(0, 220, 0), (75, 255, 75)],
            "BCK_color_mask": [(0, 160, 0), (180, 255, 100)],
            "StopDistanceFromCenter": 1.0,
            "lug_side": 1,
            "sttprog": 0,
        }
        data = {}
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "config457_mv.json")
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        # Build values from defaults + JSON, then set fixed members for cfg.resolution, cfg.delta, etc.
        def val(key):
            v = data.get(key, defaults[key])
            if key in ("BCK_color", "BCK_color_mask") and isinstance(v, list):
                v = [tuple(x) for x in v]
            return v
        self.resolution = val("resolution")
        self.Y0 = val("Y0")
        self.delta = val("delta")
        self.CameraID = val("CameraID")
        self.CameraXpixel = val("CameraXpixel")
        self.CameraYpixel = val("CameraYpixel")
        self.CameraRotation = val("CameraRotation")
        self.CropX0 = val("CropX0")
        self.CropX1 = val("CropX1")
        self.CropY0 = val("CropY0")
        self.CropY1 = val("CropY1")
        self.marginX = val("marginX")
        self.marginY = val("marginY")
        self.ObjectDistance = val("ObjectDistance")
        self.Lens = val("Lens")
        self.offsetx = val("offsetx")
        self.Camera_Distance = val("Camera_Distance")
        self.BCK_color = val("BCK_color")
        self.BCK_color_mask = val("BCK_color_mask")
        self.StopDistanceFromCenter = val("StopDistanceFromCenter")
        self.lug_side = val("lug_side")
        self.sttprog = val("sttprog")
        self.workstationID = val("workstationID")
        self.JobNbr=0
        self.counter = 0




class Image_():
    def __init__(self):

        defult_img=cv2.imread('icons/no_pic2.png')
        self.status=-1
        self.last_img = defult_img
        self.img_G = defult_img
        self.img_W = defult_img
        self.img_toShow=defult_img
        self.Err = -100
        self.distance = []
        self.speed=-1
        self.GreenOn=0
        self.WhiteOn = 0
        self.NeedStop=False
        self.connected=False
        self.nbr=0


    def get_x(self,img):
        x1 = int(img.shape[1]/2) - 5
        x2 = x1 + 5
        arr_ = np.mean(img[:, x1:x2, 1], axis=1)
        a= (arr_ < 100).astype(int)
        d = np.diff(a.astype(int), prepend=0, append=0)
        starts = np.where(d == 1)[0]
        ends = np.where(d == -1)[0] - 1
        ranges = np.column_stack((starts, ends)).tolist()
        # Merge ranges whose gap (zeros between them) <= max_gap
        merged = []
        max_gap=to_pixel(7)
        for s, e in ranges:
            if not merged:
                merged.append([s, e])
            else:
                prev_s, prev_e = merged[-1]
                gap = s - prev_e - 1  # number of zeros between runs
                if gap <= max_gap:
                    merged[-1][1] = e  # extend previous interval
                else:
                    merged.append([s, e])

        if len(merged)>0:
            self.distance.append(to_mm((merged[0][0] + merged[0][1]) / 2))  #

        # updating values of disctnace and speed
        if len(merged)==0 or len(merged)>2: #case 0: nothing was detected
            self.distance=[]
            self.speed =-1
            self.status=0
            self.NeedStop = 0
        elif  self.status==0 and ( merged[0][1]< len(a)*0.4 or merged[0][0]<len(a)*0.05 ): # case 1: head indisde, tail not yet
            self.distance = []
            self.speed =100
            self.status =1
            self.NeedStop = 0

        elif self.status == 1 and to_mm(merged[0][0])>8: # head & tail inside
            self.speed = 100
            self.status = 2
            self.NeedStop = 0


        elif  self.status == 2  and   (merged[-1][0]+ merged[-1][1])/2 > len(a)/2- to_pixel(Cfg_mv.StopDistanceFromCenter) : #need stop
            self.speed = 100
            self.NeedStop=1
        ####    plc.write_multiple_registers(0, [1, 0, 0, 0, 1])  # Conveyor stop
            self.status=3

        elif self.status == 3 and len( self.distance)>1 and  to_mm(self.distance[-1]-self.distance[-2])<0.2:#stop is detected
            self.speed=0
            self.status = 4


        return

    def update_colors(self, img0):
        y1, y2 = int(img0.shape[0] / 2) - 5, int(img0.shape[0] / 2) + 5
        x1, x2 = int(img0.shape[1] / 10) - 5, int(img0.shape[1] / 10) + 5
        color = np.mean(img0[y1:y2, x1:x2], axis=(0, 1))
        self.GreenOn = 1 if (color[1] / (1 + color[0] + color[2]) > 1.5) and color[1] > 150 else 0
        #print('green',color[1] / (1 + color[0] + color[2]), color[1])
        self.WhiteOn = 1 if np.max(color) / max(1, np.mean(color)) < 1.5 and np.min(color) / max(1, np.mean(
            color)) > 0.66 and np.mean(color) > 35 else 0

    def refresh(self):

        self.connected = True
        ret, img0 = cam.read()
        ret, img0 = cam.read()
        #cv2.imwrite(f"capture\\{cnt2}_G.BMP", img0)
        if not ret:

            img0 = cv2.imread('images457\\icons\\no_pic2.png')
            self.connected = False
            self.GreenOn = 0
            self.WhiteOn = 0
            self.distance = []
            self.speed = -1
            self.status=0
            return

        self.last_img = img0
        self.update_colors(img0)
        if self.GreenOn == 1:
            self.get_x(img0)
            if self.speed==0 :
                pass
                #self.img_G = img0

        else:
            self.distance = []
            self.speed = -1
            self.status=0
        if self.WhiteOn == 1 :
            #self.img_W = img0
            pass


        #print('img refresh-  GreenOn:',self.GreenOn,'WhiteOn',self.WhiteOn,'distance',self.distance,'status',self.status)


class dimension():
    def __init__(self):
        self.name = 'NoName'
        self.Spec = 0.0
        self.Tolerance = 0.0
        self.workspec = 0.0
        self.worktolerance = 0.0
        self.actual = 0.0
        self.ignore = False
        self.index = -1

    def ok(self):
        if inspec(self.actual, self.workspec, self.worktolerance):
            return True
        else:
            return False

    def reject(self):
        if (not inspec(self.actual, self.workspec, 2 * self.worktolerance)) and not self.ignore:
            return True
        else:
            return False


class Lug2_():
    def __init__(self):
        self.ignor = False
        self.tp = 1
        self.text = 'Lug'
        self.H = dimension()
        self.W = dimension()
        self.Y = dimension()
        self.R = dimension()
        self.R.tolerance = 0.25
        self.R.worktolerance = 0.25

    def ok(self):
        if self.H.ok() and self.W.ok() and self.Y.ok() and self.R.ok():  #
            return True
        else:
            if not self.H.ok():
                if pprint: print("lug H not ok=", self.H.actual, self.H.workspec)
            if not self.W.ok():
                if pprint: print("lug W not ok=", self.W.actual, self.W.workspec)
            if not self.Y.ok():
                if pprint: print("lug Y not ok=", self.Y.actual, self.Y.workspec)
            if not self.R.ok():
                if pprint: print("lug R not ok=", self.R.actual, self.R.workspec)
            return False

    def reject(self):
        if (self.H.reject() or self.W.reject() or self.Y.reject() or self.R.reject()) and not self.ignor:
            return True
        else:
            return False


class Hole2_():

    def __init__(self):
        self.ignor = False
        self.text = 'Hole-1'
        self.tp = 1
        self.L = dimension()  # 'p2 Length  L-R mm (horizontal Length)'
        self.W = dimension()  # 'p1 Width  B-T  mm (Vertical length)'
        self.X = dimension()
        self.Y = dimension()

    def ok(self):
        if self.L.ok() and self.W.actual < self.W.workspec * 1.2 and self.Y.ok() and self.X.ok():
            # print("Hole X  ok=", self.X.actual, self.X.workspec)
            return True
        else:
            if not self.L.ok():
                if pprint: print("Hole L not ok=", self.L.actual, self.L.workspec)
            if not self.W.actual < self.W.workspec * 1.2:
                if pprint: print("Hole W not ok=", self.W.actual, self.W.workspec)
            if not self.Y.ok():
                if pprint: print("Hole Y not ok=", self.Y.actual, self.Y.workspec)
            if not self.X.ok():
                if pprint: print("Hole X not ok=", self.X.actual, self.X.workspec)
            return False

    def reject(self):
        if (self.L.reject() or self.W.reject() or self.X.reject() or self.Y.reject()) and not self.ignor:
            return True
        else:
            return False


class groove2_(object):
    def __init__(self):
        self.text = 'Groove-1'
        self.ignor = False
        self.WR = dimension()
        self.WL = dimension()
        self.TL = dimension()
        self.TR = dimension()
        self.Y = dimension()

    def ok(self):
        if (self.TL.ok() and self.WL.ok()) or (self.TR.ok() and self.WR.ok()):  # and self.W.ok() and self.Y.ok():
            return True
        else:

            if not self.TL.ok():
                if pprint: print("groove TL not ok", self.TL.actual, self.TL.workspec)
            if not self.TR.ok():
                if pprint: print("groove TR not ok", self.TR.actual, self.TR.workspec)
            if not self.WR.ok():
                if pprint: print("groove WR not ok", self.WR.actual, self.WR.workspec)
            if not self.WL.ok():
                if pprint: print("groove WL not ok", self.WL.actual, self.WL.workspec)

            return False

    def reject(self):
        if (self.WL.reject() or self.TL.reject() or self.TR.reject() or self.Y.reject()) and not self.ignor:
            return True
        else:
            return False


class alert_(object):
    tp = 0
    x1 = 0
    y1 = 0
    x2 = 0
    y2 = 0
    cx = 0
    cy = 0
    size = 0


class body_(object):
    tp = 1
    diam = 1.0  # diameter
    W = 1.0  # width
    T = 1.0  # thick
    H = 1.0  # analysis location
    rW = 0.0
    rD = 0.0



def Image_Flip():
    global lug_side
    Cfg_mv.lug_side = 1 - lug_side
    GetSpec(spec)


def Image_calibration():
    ret, img0 = cam.read()
    img1 = img0[Cfg_mv.CropY0:- Cfg_mv.CropY1, Cfg_mv.CropX0:-Cfg_mv.CropX1, :]
    mask = cv2.inRange(img1, (0, 50, 0), (255, 255, 255))
    sumofpixy = np.sum(mask, axis=0)

    l, r = 0, 0
    for i in range(mask.shape[1]):
        if sumofpixy[i] / 255 < mask.shape[0] * 0.9:
            l = i
            break
    for i in range(mask.shape[1] - 1, 1, -1):
        if sumofpixy[i] / 255 < mask.shape[0] * 0.9:
            r = i + 4
            break
    obj_size = 100
    length = r - l
    resolution =   obj_size/length
    print('resolution-', resolution)

# בודק שערך מצוי נמצא בסביבה של ערך רצוי מחזיר תשובה כן-לא
def inspec(a, r, t):
    return True if r - t <= a <= r + t else False


def cut_image(imgnativeWhite, imgnativeGreen):
    try:
        print('start cutting_image')
        sliceWhite = imgnativeWhite[Cfg_mv.CropY0:- Cfg_mv.CropY1, Cfg_mv.CropX0:-Cfg_mv.CropX1, :]
        sliceGreen = imgnativeGreen[Cfg_mv.CropY0:- Cfg_mv.CropY1,Cfg_mv.CropX0:-Cfg_mv.CropX1,:]
        mask = cv2.inRange(sliceGreen, (0, 50, 0), (255, 255, 255))
        green = sliceGreen[:, :, 1]
        _, thresh = cv2.threshold(green,80, 255,  cv2.THRESH_BINARY_INV  )
        cv2.imwrite("dev_img\\2.13sliceGreen.jpg", sliceGreen)
        cv2.imwrite("dev_img\\2.14green.jpg", green)

        # ניקוי רעשים
        kernel = np.ones((5, 5), np.uint8)
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)

        # =====================================================
        # כמה פיקסלים לבנים יש בכל שורה
        # =====================================================
        row_sum = np.sum(thresh > 0, axis=1)

        # =====================================================
        # לשמור רק שורות רחבות
        # =====================================================
        mask_rows = row_sum > 200

        # =====================================================
        # למצוא נקודות רק מהשורות האלו
        # =====================================================
        ys, xs = np.where(thresh > 0)

        valid = mask_rows[ys]

        xs = xs[valid]
        ys = ys[valid]

        points = np.column_stack((xs, ys)).astype(np.float32)



        #points = np.column_stack(np.where(thresh > 0))
        cv2.imwrite("dev_img\\2.12thresh.jpg", thresh)

        # fitLine

        points = points.astype(np.float32)
        vx, vy, x0, y0 = cv2.fitLine(points, cv2.DIST_L2, 0, 0.01, 0.01)
        print('vx',vx, 'vy',vy, 'x0',x0, 'y0',y0)
        angle = np.degrees(np.arctan2(vy, vx))[0]

        print(f"Detected angle = {angle:.2f}")

        # סיבוב תמונה כדי ליישר את האובייקט
        h, w = mask.shape[:2]
        center = (w // 2, h // 2)
        rot_mat = cv2.getRotationMatrix2D(center, angle, 1.0)

        rotatedWhite = cv2.warpAffine(sliceWhite, rot_mat, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 255, 0))
        rotatedGreen = cv2.warpAffine(sliceGreen, rot_mat, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 255, 0))
        cv2.imwrite("dev_img\\2.11rotated.jpg", rotatedGreen)
        sf=40
        rotatedWhite = rotatedWhite[sf:- sf, sf:-sf, :]
        rotatedGreen = rotatedGreen[sf:- sf,sf:-sf,:]
        cv2.imwrite("dev_img\\2.12rotated.jpg", rotatedGreen)

        matrix = cv2.inRange(rotatedGreen, (0, 50, 0), (255, 255, 255))
        cv2.imwrite("dev_img\\2.2rotatedmatrix.jpg", matrix)
        sumofpix = np.sum(matrix, axis=1)
        sumofpixy = np.sum(matrix, axis=0)
        print('sumofpixy',sumofpixy[1150:1250]/255,'matrix.shape[0]',matrix.shape[0])

        l, r, t, b = 0, 0, 0, 0
        for i in range(matrix.shape[1]):
            if sumofpixy[i] / 255 < matrix.shape[0] * 0.9:
                l = i
                break
        for i in range(matrix.shape[1]-1,1,-1):
            if sumofpixy[i] / 255 < matrix.shape[0] * 0.9:
                r =  i+4
                break
        window_size = 5
        limit_frame=window_size*1
        for i in range(limit_frame, matrix.shape[0] - limit_frame, 1):
            prev_mean = np.mean(sumofpix[i - window_size:i]) / 255
            curr_mean = np.mean(sumofpix[i:i + window_size]) / 255
            if prev_mean - curr_mean > 30:
                t = i
                break

        for i in range(matrix.shape[0] - limit_frame, limit_frame, -1):
            prev_mean = np.mean(sumofpix[i:i + window_size]) / 255
            curr_mean = np.mean(sumofpix[i - window_size:i]) / 255
            if prev_mean - curr_mean > 30:
                b = i
                break


        Body.rW = to_mm(b - t)
        Body.rD = to_mm(r - l)

        print('Cropborder  l-',l, 'r', r, 't', t,'b',b)

        imgtw = rotatedWhite[t - Cfg_mv.marginY:b + Cfg_mv.marginY, l - Cfg_mv.marginX:r + Cfg_mv.marginX, :]

        imgtg = rotatedGreen[t - Cfg_mv.marginY:b + Cfg_mv.marginY, l - Cfg_mv.marginX:r + Cfg_mv.marginX, :]
        cv2.imwrite("dev_img\\2.3imgtw.jpg", imgtw)
        cv2.imwrite("dev_img\\2.4imgtg.jpg", imgtg)

        return imgtw, imgtg


    except Exception as e:
        tb = traceback.format_exc()
        print(f'cutting_image Not responding  {tb}')
        err_txt = f' cutting_image Not responding   {e}'
        show_error(err_txt)


def Image_alignment(imgWhite, imgGreen):
    try:
        print('start Image_alignment')
        mask = cv2.inRange(imgGreen, (0, 50, 0), (255, 255, 255))
        #cv2.imwrite("dev_img\\mask_align.png", mask)
        mask[mask == 0] = 0
        # mask[mask == 255] = 1
        Y0 = int(mask.shape[0] / 2)
        scanner = to_pixel(2)
        detector = scanner * 0.7

        # left side body detection
        for x in range(mask.shape[1]):
            if mask[Y0:Y0 + scanner, x].sum() < detector:
                l = x
                break

        # right side body detection
        for x in range(mask.shape[1]):
            xi = mask.shape[1] - 1 - x
            if mask[Y0:Y0 + scanner, xi].sum() < detector:
                r = xi
                break

        ###WHITE

        # imgnativeWhite=imgnativeWhite[:,l-100:r+100]
        #print('r-', r, "l-", l)
        center_x = int(imgWhite.shape[1] / 2)
        center_bearing = int(l + (r - l) / 2)
        if center_bearing > center_x:
            imgWhite = imgWhite[:, center_bearing - center_x:, :]

        elif center_bearing < center_x:
            imgWhite = imgWhite[:, :-(center_x - center_bearing), :]

        ri = int((r - l) / 2 - 40)
        #print("ri", ri, "center_x", center_x)
        center_y = int(imgWhite.shape[0] / 2)
        y2 = int(imgWhite.shape[0])
        #print('center_x - ri', center_x - ri)
        lm = imgWhite[:, :center_x - ri, :]
        if pprint:print('--------------------x', l - (Cfg_mv.CropX0), 'y', center_x - ri)
        y3 = lm.shape[0]
        x = center_x - ri

        while x < center_x + ri:
            CosTeta = math.sqrt(ri ** 2 - (center_x - x) ** 2) / ri
            ky = 1 - CosTeta * ri / to_pixel(Cfg_mv.ObjectDistance)
            ky = int(ky * y3 / 2)
            delta_x = max(int(ri / 5 * CosTeta ** 3 / 2), 2)
            yyy = imgWhite[center_y - ky:center_y + ky, x:x + delta_x, :]
            yyy = cv2.resize(yyy, (delta_x, y3), interpolation=cv2.INTER_AREA)
            lm = np.concatenate((lm, yyy), axis=1)
            y_last = y2
            x = x + delta_x

        rm = imgWhite[y_last - y3:y_last, center_x + ri:r + Cfg_mv.CropX1, :]
        imgtWhite = np.concatenate((lm, rm), axis=1)

        ###GREEN
        tttstart = time.time()
        if center_bearing > center_x:
            imgGreen = imgGreen[:, center_bearing - center_x:, :]

        elif center_bearing < center_x:
            imgGreen = imgGreen[:, :-(center_x - center_bearing), :]

        ri = int((r - l) / 2 - 40)
        center_y = int(imgGreen.shape[0] / 2)
        y2 = int(imgGreen.shape[0])
        lm = imgGreen[:, :center_x - ri, :]
        y3 = lm.shape[0]
        x = center_x - ri

        while x < center_x + ri:
            CosTeta = math.sqrt(ri ** 2 - (center_x - x) ** 2) / ri
            ky = 1 - CosTeta * ri / to_pixel(Cfg_mv.ObjectDistance)
            ky = int(ky * y3 / 2)
            delta_x = max(int(ri / 5 * CosTeta ** 3 / 2), 2)
            yyy = imgGreen[center_y - ky:center_y + ky, x:x + delta_x, :]
            yyy = cv2.resize(yyy, (delta_x, y3), interpolation=cv2.INTER_AREA)
            lm = np.concatenate((lm, yyy), axis=1)
            y_last = y2
            x = x + delta_x

        rm = imgGreen[y_last - y3:y_last, center_x + ri:r + Cfg_mv.CropX1, :]
        imgtGreen = np.concatenate((lm, rm), axis=1)

        cv2.imwrite("dev_img\\3.1align_img_white.jpg", imgtWhite)
        cv2.imwrite("dev_img\\3.2align_img_green.jpg", imgtGreen)
        # x = 1 / 0  # חלוקה באפס
        # plt.imshow(imgt)
        # plt.show()

        return imgtWhite, imgtGreen




    except Exception as e:
        tb = traceback.format_exc()
        print(f'Image_alignment Not responding  {tb}')
        err_txt = ' Image_alignment Not responding '

        show_error(err_txt)


def should_check(i, X1, X2, inside=True):
    if inside:
        return X1 <= i <= X2
    else:
        return i < X1 or i > X2


def find_surface_defects(imgtw, imgtoshow):
    print("find_surface_defects")
    global Alerts

    try:

        cv2.imwrite(f"dev_img\\7.1imgtwwww.jpg", imgtw)
        gray_image = cv2.cvtColor(imgtw, cv2.COLOR_BGR2GRAY)

        ##sampling picture 3X3 pixel to 1 pixel
        h, w = gray_image.shape
        h_new = h - h % 3
        w_new = w - w % 3
        img_cropped = gray_image[:h_new, :w_new]

        # משנה את הצורה למטריצה של בלוקים של 3×3
        img_blocks = img_cropped.reshape(h_new // 3, 3, w_new // 3, 3)

        # מחשב ממוצע לכל בלוק
        gray_image = img_blocks.mean(axis=(1, 3)).astype(np.uint8)

        imgp2 = gray_image.copy()
        imgp2[:, :] = 0
        imgp3 = imgp2.copy()
        gray_image = gray_image.astype(float)

        '''
        # Apply Gaussian blur to reduce noise (optional but recommended)
        blurred = cv2.GaussianBlur(gray_image, (3, 3), 0.5)
        # Apply Laplacian
        laplacian = cv2.Laplacian(blurred, cv2.CV_64F)
        # Convert back to uint8 for display
        gray_image = cv2.convertScaleAbs(laplacian)
        cv2.imwrite(f"dev_img\\laplacian.jpg", gray_image)
        '''

        hide_margin_x = int(Cfg_mv.CropX0 / 3 + gray_image.shape[1] * 0.03)
        hide_margin_y = int(Cfg_mv.CropY0 / 3 + gray_image.shape[0] * 0.07)

        if pprint:print('gray_image.shape[0]', gray_image.shape[0], 'gray_image.shape[1]', gray_image.shape[1])
        # cv2.line(gray_image,(hide_margin_x,0),(hide_margin_x,gray_image.shape[0]),(0,0,0),3)
        # cv2.line(gray_image, (gray_image.shape[1]-hide_margin_x, 0), (gray_image.shape[1]-hide_margin_x, gray_image.shape[0]), (0, 0, 0), 3)
        # cv2.line(gray_image, (0, hide_margin_y), ( gray_image.shape[1],hide_margin_y), (0, 0, 0), 3)
        # cv2.line(gray_image, (0,gray_image.shape[0] - hide_margin_y ), (gray_image.shape[1],gray_image.shape[0] - hide_margin_y ), (0, 0, 0), 3)

        if pprint:print('Grooves2', Grooves2[0].WL.workspec)

        for g in Grooves2:  # מוחק ערכים בתוך המקומות שיש תעלות שמם

            try:
                y1 = int(gray_image.shape[0] / 2 - to_pixel(g.WL.workspec * 1.1 / 2) / 3 - 12)
                y2 = int(gray_image.shape[0] / 2 + to_pixel(g.WL.workspec * 1.1 / 2) / 3 + 12)


            except:
                y1 = int(gray_image.shape[0] / 2 - to_pixel(g.WR.workspec * 1.1 / 2) / 3 - 25)
                y2 = int(gray_image.shape[0] / 2 + to_pixel(g.WR.workspec * 1.1 / 2) / 3 + 25)

            if g.WL.workspec == 0:
                y1 = int(imgtw.shape[0] / 2)
                y2 = int(imgtw.shape[0] / 2)
            else:
                pass

        if pprint:print('y1', y1, 'y2', y2)

        ######  change to read from csv table
        X1 = hide_margin_x
        X2 = gray_image.shape[1] - hide_margin_x
        inside = False
        de = 20

        for i in range(hide_margin_x, gray_image.shape[1] - hide_margin_x):

            for j in range(hide_margin_y, gray_image.shape[0] - hide_margin_y):

                if (j < y1) or (j >= y2) or (y1 <= j < y2 and should_check(i, X1, X2, inside)):
                    de = gray_image[j, i] * 0.2
                    if abs(gray_image[j, i] - gray_image[j + 4, i]) > de or abs(
                            gray_image[j, i] - gray_image[j + 3, i]) > de or abs(
                        gray_image[j, i] - gray_image[j + 2, i]) > de or abs(
                        gray_image[j, i] - gray_image[j + 1, i]) > de:
                        imgp2[j, i] = 255

                    else:
                        imgp2[j, i] = 0

        # cv2.line(gray_image,(0,y1),(gray_image.shape[1],y1),(255,255,255),1)
        # cv2.line(gray_image, (0, y2), (gray_image.shape[1], y2), (255,255,255), 1)
        cv2.imwrite(f"dev_img\\7.2gray_image.jpg", gray_image)

        for h in Holes2:  # מוחק ערכים בתוך המקומות שיש חורים
            if h.W.workspec == h.L.workspecorg:  #### change1- was  if h.tp ==1:
                xy2 = to_pixelXY2(h.X.actual, h.Y.actual, imgp2)
                imgp2 = cv2.circle(imgp2, xy2, to_pixel((h.W.actual + Cfg_mv.delta * 1.2) / 2 * 1), 0, -1)

            else:
                cx = to_pixelXY2(h.X.workspec, h.Y.workspec, imgp2)[0]
                cy = to_pixelXY2(h.X.workspec, h.Y.workspec, imgp2)[1]
                x1, x2 = int(cx - to_pixel(h.L.workspec / 2 + 0.3)), int(cx + to_pixel(h.L.workspec / 2 + 0.5))
                y1, y2 = int(cy - to_pixel(h.W.workspec / 2 + 0.3)), int(cy + to_pixel(h.W.workspec / 2 + 0.5))
                imgp2 = cv2.rectangle(imgp2, (x1, y1), (x2, y2), 0, 1)

        imgp2 = np.repeat(np.repeat(imgp2, 3, axis=0), 3, axis=1)  # unsampling- resize to original size picture

        Alerts = []
        scale = 1
        n_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(imgp2, connectivity=8)
        for i in range(1, n_labels):
            area = stats[i, cv2.CC_STAT_AREA]
            h = stats[i, cv2.CC_STAT_HEIGHT]
            w = stats[i, cv2.CC_STAT_WIDTH]
            jx = int(centroids[i][0])
            jy = int(centroids[i][1])
            # print('area=', area, 'h=', h, 'w=', w, "jx=", jx, "jy=", jy)
            if area > 20 and w > 5 and h > 8:
                obj = alert_()
                obj.tp = 1
                obj.x1 = int(stats[i, cv2.CC_STAT_LEFT] / scale)
                obj.y1 = int(stats[i, cv2.CC_STAT_TOP] / scale)
                obj.x2 = int((stats[i, cv2.CC_STAT_LEFT] + stats[i, cv2.CC_STAT_WIDTH]) / scale)
                obj.y2 = int((stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT]) / scale)
                obj.cx = int(centroids[i][0])
                obj.cy = int(centroids[i][1])
                obj.size = area / to_pixel(1.0) ** 2
                Alerts.append(obj)

        nbr = 0
        for i in Alerts:
            imgAlert = cv2.rectangle(imgtoshow, (i.x1, i.y1), (i.x2, i.y2), (0, 0, 255), 2)
            nbr += 1
            imgAlert = cv2.putText(imgtoshow, str(nbr), (min(i.x1, i.x2), min(i.y1, i.y2) - 10),
                                   cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2, cv2.LINE_AA)

        if Alerts: cv2.imwrite(f"dev_img\\7.3surface_detect.jpg", imgAlert)

        cv2.imwrite(f"dev_img\\7.4imgp2.jpg", imgp2)

        return imgtoshow

    except Exception as e:
        tb = traceback.format_exc()
        err_txt = f' find_surface_defects Not responding {tb}'
        show_error(err_txt)


def Detect_Lugs(imgtg1, imgtoshow):
    global lug_edge, bear_edge

    try:
        maskg = cv2.inRange(imgtg1, (0, 150, 0), (255, 255, 255))
        cv2.imwrite("dev_img\\5.1maskg.jpg", maskg)

        maskg[maskg == 0] = 1
        maskg[maskg == 255] = 0
        sumofg = np.sum(maskg, axis=0)
        filtered = [num for num in sumofg if num != 0]
        average_sum = sum(filtered) / len(filtered) if filtered else 0
        #print("filtered", filtered)
        #print("sumofg",sumofg[600:])
        #plt.plot(range(len(sumofg)), sumofg)
        #plt.show()
        st = 0
        for i in range(len(sumofg) - 1, int(len(sumofg) / 2), -1):
            if average_sum / 2 > sumofg[i] > 5 and st == 0:
                lug_edge = i + 3
                st = 1
            elif average_sum / 2 > sumofg[i] > 5 and st == 1:
                bear_edge = i
            elif sumofg[i] > average_sum / 2 and st == 1:
                break
            else:
                pass
        if pprint:print("lug_edge", lug_edge, "bear_edge", bear_edge)

        side_check = maskg[:, -(Cfg_mv.CropX1+5):]
        sum_side = np.sum(side_check, axis=1)
        top_edge = int(len(sum_side) / 2) - np.argmin(list(reversed(sum_side[:int(len(sum_side) / 2)]))) - 0
        bottom_edge = np.argmin(sum_side[int(len(sum_side) / 2):]) + int(len(sum_side) / 2) - 5
        if pprint:print("top_edge", top_edge, "bottom_edge", bottom_edge)

        # plt.plot(range(len(sum_side)), sum_side)
        # plt.show()
        side_check2 = side_check.copy()
        side_check2[side_check2 == 1] = 255
        cv2.imwrite("dev_img\\5.2side_check.jpg", side_check)
        cv2.imwrite("dev_img\\5.3side_check2.jpg", side_check2)
        check_lug = maskg[:, int((bear_edge + lug_edge) / 2) - 2]
        cv2.imwrite("dev_img\\5.11check_lug.jpg", check_lug)
        # print("check_lug",check_lug)

        connected = []
        for lg in Lugs2:
            n_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(check_lug, connectivity=8)
            for i in range(1, n_labels):
                if i not in connected:
                    w_lug = stats[i, cv2.CC_STAT_HEIGHT]
                    top_lug = stats[i, cv2.CC_STAT_TOP]
                    if pprint:print("w_lug", w_lug, "top_lug", top_lug)
                    Y_lug = (top_lug + w_lug / 2) - top_edge
                    if pprint: print("Y_lug", Y_lug)

                    connected.append(i)

                    ###רישום מידות השן
                    lg.H.actual = round((lug_edge - bear_edge) * Cfg_mv.resolution, 2)
                    lg.W.actual = round(w_lug * Cfg_mv.resolution, 2)
                    lg.Y.actual = round(Y_lug * Cfg_mv.resolution, 2)
                    if pprint:print("Lug2_H_actual", lg.H.actual)
                    if pprint:print("Lug2_W_actual", lg.W.actual)
                    if pprint:print("Lug2_Y_actual", lg.Y.actual)

                    ### display
                    gap=3
                    cv2.line(imgtoshow, (bear_edge, top_lug-gap), (lug_edge, top_lug-gap), (255, 255, 255), 2)  # top lug line
                    cv2.line(imgtoshow, (bear_edge, w_lug + top_lug+gap), (lug_edge, w_lug + top_lug+gap), (255, 255, 255),2)  # bottom lug line
                    #cv2.line(imgtoshow, (lug_edge+gap, top_lug), (lug_edge+gap, w_lug + top_lug), (255, 255, 255),                             1)  # right side lug line
                    #cv2.line(imgtoshow, (bear_edge-gap, top_lug), (bear_edge-gap, w_lug + top_lug), (255, 255, 255),                             1)  # left side lug line
                    #cv2.line(imgtoshow, (lug_edge, int(w_lug / 2) + top_lug),(imgtg1.shape[1], int((w_lug / 2) + top_lug)), (255, 255, 255), 2)  # senter lug line
                    cv2.arrowedLine(
                        imgtoshow,
                        (imgtg1.shape[1], int(w_lug / 2) + top_lug),
                        (lug_edge, int(w_lug / 2) + top_lug),
                        (255, 255, 255),
                        2,
                        tipLength=0.15
                    )
                    break

            if len(stats) - 1 != len(Lugs2):  ###לבדיקת שן מיותרת
                for i in range(1, n_labels):
                    w_lug_err = stats[i, cv2.CC_STAT_HEIGHT]
                    if w_lug_err > 10 and i not in connected:
                        w_lug_err = stats[i, cv2.CC_STAT_HEIGHT]
                        top_lug_err = stats[i, cv2.CC_STAT_TOP]
                        Y_lug_err = (top_lug + w_lug / 2) - top_edge
                        cv2.rectangle(imgtoshow, (bear_edge, top_lug_err), (lug_edge, top_lug_err + w_lug_err),
                                      (0, 0, 255), 2)

                break

        cv2.imwrite(f"dev_img\\5.4lug_detection.jpg", imgtoshow)

        return imgtoshow

    except Exception as e:
        err_txt = ' Detect_Lugs Not responding '
        show_error(err_txt)


# בודק קיום חורים ע"פ ספסיפיקציות
def Detect_Holes(imgtg, imgtoshow):
    print("Detect_Holes")
    try:
        imgb = cv2.inRange(imgtg, (0, 50, 0), (255, 255, 255))
        # cv2.imwrite("dev_img\\imgb.bmp", imgb)
        n_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(imgb, connectivity=8)

        connected = []
        hcount = 0
        for h in Holes2:
            hcount += 1

            r, l, t, b, px, py, L_hole, W_hole = 0, 0, 0, 0, 0, 0, 0, 0
            for i in range(1, n_labels):
                mmxy = to_mmXY(centroids[i][0], centroids[i][1], imgb)
                distance = math.sqrt((h.X.workspec - mmxy[0]) ** 2 + (h.Y.workspec - mmxy[1]) ** 2)
                y_distance = abs(h.Y.actual - mmxy[1])  ### change4- axial distance
                area = round(stats[i, cv2.CC_STAT_AREA] * Cfg_mv.resolution ** 2, 2)
                if distance < 3 and y_distance < 0.6 and h.W.workspec * h.L.workspec * 1.5 > area > h.W.workspec * h.L.workspec * 0.15 and i not in connected:
                    connected.append(i)
                    px, py = int(centroids[i][0]), int(centroids[i][1])
                    s_l, s_t, s_gap = to_pixel(0.75), to_pixel(0.2), 40
                    L_hole = to_mm(stats[i, cv2.CC_STAT_WIDTH])
                    W_hole = to_mm(stats[i, cv2.CC_STAT_HEIGHT])

            r = px + to_pixel(L_hole / 2)
            l = px - to_pixel(L_hole / 2)
            t = py - to_pixel(W_hole / 2)
            b = py + to_pixel(W_hole / 2)
            len_r = r - px
            len_l = px - l
            len_b = b - py
            len_t = py - t

            if abs(len_r - len_l) > 2:
                if len_r > len_l:
                    l = px - len_r
                elif len_l > len_r:
                    r = px + len_l
            else:
                pass

            if abs(len_b - len_t) > 2:
                if len_b > len_t:
                    t = py - len_b
                elif len_t > len_b:
                    b = py + len_t
            else:
                pass

            # רישום התוצאה באויבביקט

            px = int(r / 2 + l / 2)
            py = int(b / 2 + t / 2)
            h.X.actual = to_mmXY(px, py, imgb)[0]
            h.Y.actual = to_mmXY(px, py, imgb)[1]
            h.L.actual = L_hole
            h.W.actual = W_hole
            if pprint:print('h.X.actual', h.X.actual, 'h.Y.actual', h.Y.actual, 'h.L.actual', h.L.actual, 'h.W.actual',
                  h.W.actual)

            # טיפול בתצוגה
            okk = 'H_Ok' if h.ok() else 'H_X'
            clr = (255, 0, 0) if h.ok() else (0, 0, 255)

            imgtoshow = cv2.putText(imgtoshow, okk, (px, t - 10), cv2.FONT_HERSHEY_SIMPLEX, 1, clr, 2, cv2.LINE_AA)
            # קווי ציר
            imgtoshow[t:b, px, :] = (255, 0, 0)
            imgtoshow[py, l:r, :] = (255, 0, 0)

            # עיגול למטרה
            imgtoshow = cv2.circle(imgtoshow, (px, py), 4, (255, 0, 0), 2)
            # עיגול על ספציפיקציה מרכז החור צ"ל
            imgtoshow = cv2.circle(imgtoshow, to_pixelXY(h.X.workspec, h.Y.workspec, imgb),
                                   to_pixel(h.W.workspec / 2 + 2), (100, 255, 100), 2)
        # print('n_labels-',n_labels)
        # print('connected-', connected)

        # מסמן תקלות= אובייקטים שלא שמוייכים לחור והגודל שלהם גדול מ 1 ממ"ר
        for i in range(1, n_labels):
            area = round(stats[i, cv2.CC_STAT_AREA] * Cfg_mv.resolution ** 2, 1)
            (gX, gY) = centroids[i]
            X1s = stats[i, cv2.CC_STAT_LEFT]
            Y1s = stats[i, cv2.CC_STAT_TOP]

            '''if Body.tp==2:
                Body_top1= round(Body_top+(Body_bottom-Body_top)*0.2)  ##ignore labels of klinch (type 2)
            else:
                Body_top1=Body_top
            'and area < Cfg.delta * Body.diam'

            if area > 1.5  and X1s>1 and Y1s>1 and (Body_left) < gX < (Body_right) and (Body_top1) < gY < (Body_bottom ) and i not in connected:

                obj = alert_()
                obj.tp = 1
                obj.x1 = stats[i, cv2.CC_STAT_LEFT]
                obj.y1 = stats[i, cv2.CC_STAT_TOP]
                obj.x2 = stats[i, cv2.CC_STAT_LEFT] + stats[i, cv2.CC_STAT_WIDTH]
                obj.y2 = stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT]
                obj.cx = int(centroids[i][0])
                obj.cy = int(centroids[i][1])
                obj.size = area / to_pixel(1.0) ** 2
                Alerts.append(obj)'''
        Ok = True
        for x in Holes2:
            if x.ok():
                Ok = Ok

            else:
                Ok = False
                imgtoshow = cv2.putText(imgtoshow, "Holes not ok", (400, imgtg.shape[0] - to_pixel(3)),
                                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2,
                                        cv2.LINE_AA)
        cv2.imwrite(f"dev_img\\4.1detect_hole.jpg", imgtoshow)

        return imgtoshow

    except Exception as e:
        err_txt = ' Detect_Holes Not responding '
        show_error(err_txt)


def Detect_Grooves(imgtWhite, imgtoshow):
    print("Detect_Grooves")

    def groove_meas(groove_check, side):
        gray_groove_check = cv2.cvtColor(groove_check, cv2.COLOR_BGR2GRAY)
        gray_groove_check = cv2.inRange(gray_groove_check, 20, 200)
        gray_groove_check = cv2.bitwise_not(gray_groove_check)
        cv2.imwrite(f"dev_img\\6.1gray_groove_check{side}.jpg", gray_groove_check)

        n_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(gray_groove_check, connectivity=8)

        T_groove = to_mm(stats[1, cv2.CC_STAT_WIDTH])
        W_groove = to_mm(stats[1, cv2.CC_STAT_HEIGHT])
        if side == 0:
            bottom_groove = (stats[1, cv2.CC_STAT_LEFT]) + 1

        elif side == 1:
            bottom_groove = (stats[1, cv2.CC_STAT_LEFT] + stats[1, cv2.CC_STAT_WIDTH]) - 1

        W_groove = to_mm(gray_groove_check[:, bottom_groove].sum() / 255)
        T_groove = to_mm(gray_groove_check.shape[1] - stats[1, cv2.CC_STAT_WIDTH])

        return T_groove, W_groove

    try:
        for g in Grooves2:
            ## slice to images in the groove sides for detction
            try:
                yg1 = int(imgtWhite.shape[0] / 2 - to_pixel(g.WL.workspec * 1.1 / 2) - 50)
                yg2 = int(imgtWhite.shape[0] / 2 + to_pixel(g.WL.workspec * 1.1 / 2) + 50)
                xg1 = int(Cfg_mv.CropX0)
                xg2 = int(Cfg_mv.CropX0 + to_pixel(Body.T))
                xg3 = int(imgtWhite.shape[1] - Cfg_mv.CropX0 - to_pixel(Body.T))
                xg4 = int(imgtWhite.shape[1] - Cfg_mv.CropX0)

            except:
                yg1 = int(imgtWhite.shape[0] / 2 - to_pixel(g.WR.workspec * 1.1 / 2) / 3 - 25)
                yg2 = int(imgtWhite.shape[0] / 2 + to_pixel(g.WR.workspec * 1.1 / 2) / 3 + 25)

            if g.WL.workspec == 0:
                yg1 = int(imgtWhite.shape[0] / 2)
                yg2 = int(imgtWhite.shape[0] / 2)
            else:
                pass

            if pprint:print('yg1', yg1, 'yg2', yg2)

            # left_side = cv2.rectangle(imgtWhite, (xg1, yg1), (xg2, yg2), (0, 0, 255), 2)
            # right_side = cv2.rectangle(imgtWhite, (xg3, yg1), (xg4, yg2), (0, 0, 255), 2)

            left_side = imgtWhite.copy()[yg1:yg2, xg1:xg2]
            right_side = imgtWhite.copy()[yg1:yg2, xg3:xg4]

            cv2.imwrite(f"dev_img\\6.2left_side.jpg", left_side)
            cv2.imwrite(f"dev_img\\6.3right_side.jpg", right_side)

            left_T_groove, left_W_groove = groove_meas(left_side, 0)
            right_T_groove, right_W_groove = groove_meas(right_side, 1)

            g.WL.actual = left_W_groove
            g.TL.actual = left_T_groove
            g.WR.actual = right_W_groove
            g.TR.actual = right_T_groove

            if pprint:print('left_T_groove', left_T_groove, '  left_W_groove', left_W_groove, '  right_T_groove', right_T_groove,
                  '  right_W_groove', right_W_groove)



    except Exception as e:
        err_txt = ' Detect_Grooves Not responding '
        show_error(err_txt)


def show_error(err_txt):
    global error_label
    """פונקציה כללית להצגת שגיאות"""
    current_text = ''
    new_text = current_text + f"\nError: {err_txt}"
    # error_label.configure(text=new_text, text_color="red")


def Analize_Process():
    global ser_num,num

    try:
        manual=0
        num=2
        if manual==1:
            imgnativeGreen = cv2.imread(f"native_img\\native_green{num}.jpg")
            imgnativeWhite = cv2.imread(f"native_img\\native_white{num}.jpg")
        else:
            imgnativeGreen = img.img_G
            imgnativeWhite = img.img_W

        tstart = time.time()
        #cv2.imwrite(f"native_img\\imgnativeGreen{Cfg_mv.counter}.jpg", imgnativeGreen)
        #cv2.imwrite(f"native_img\\imgnativeWhite{Cfg_mv.counter}.jpg", imgnativeWhite)
        #מנקה שוליים ומחזיר תמונה גזורה לפי מידות האובייקט
        imgtw, imgtg = cut_image(imgnativeWhite, imgnativeGreen)

        # מיישר את התמונה מפרספקטיבה לאיזומטריה
        imgtWhite, imgtGreen = Image_alignment(imgtw, imgtg)

        if pprint:print('------------image aliggnment time:', time.time() - tstart)

        img0c = cv2.bitwise_not(imgtGreen)

        kernel = np.ones((10, 10), np.uint8)
        opening = cv2.morphologyEx(img0c, cv2.MORPH_OPEN, kernel, iterations=2)

        im_back = cv2.bitwise_not(opening)
        mask = cv2.inRange(im_back, Cfg_mv.BCK_color_mask[0], Cfg_mv.BCK_color_mask[1])

        imgtoshow = imgtWhite.copy()
        imgtoshow[mask != 0] = [0, 255, 0]

        if pprint:print('-----------imgtoshow make time:', time.time() - tstart)

        Detect_Holes(imgtGreen, imgtoshow)

        if pprint:print('-----------detect holes time:',time.time() - tstart)
        Detect_Lugs(imgtGreen, imgtoshow)

        if pprint:print('-----------detct lugs time:', time.time() - tstart)
        Detect_Grooves(imgtWhite, imgtoshow)

        if pprint:print('-----------detect groove time:',  time.time() - tstart)
        find_surface_defects(imgtWhite, imgtoshow)

        if pprint:print('-----------find surface defects time:', time.time() - tstart)

        return imgtoshow

    except Exception as e:
        print(' Analize_Process Not responding ')
        err_txt = ' Analize_Process Not responding '
        show_error(err_txt)


def Data_processing(imgtoshow):
    global Alerts

    try:

        ##סימון גבולות המיסב
        imgtoshow[Cfg_mv.marginY , :, 1] = 200
        imgtoshow[-Cfg_mv.marginY, :, 1] = 200
        imgtoshow[:, Cfg_mv.marginX, 1] = 200
        imgtoshow[:, -Cfg_mv.marginX, 1] = 200

        ## הוספת אזור רישום נתונים ותוצאות בתמונה
        height, width, channels = imgtoshow.shape
        extra_height = 200
        new_rows = np.full((extra_height, width, channels), (110, 110, 110), dtype=np.uint8)  # White pixels
        newimg = np.vstack((imgtoshow, new_rows))  # Stack vertically
        font = cv2.FONT_HERSHEY_SIMPLEX
        white = (255, 255, 255)
        red = (0, 0, 255)
        darkblue = (100, 0, 0)
        color = white
        gapp = 28
        line1 = 170
        line2 = line1 - gapp
        line3 = line2 - gapp
        line4 = line3 - gapp
        line5 = line4 - gapp
        line6 = line5 - gapp
        fontscale = 0.5

        # יש כאן טעות צריך לתקן ולבדוק שהייתה טעות והתיקון סודר
        imgc = cv2.putText(newimg, 'Body:', (10, newimg.shape[0] - line1), cv2.FONT_HERSHEY_SIMPLEX, fontscale,
                           darkblue, 1, cv2.LINE_AA, False)
        txt = 'W [' + str(Body.W) + ']=' + str(Body.rW)
        imgc = cv2.putText(newimg, txt, (10, newimg.shape[0] - line2), font, fontscale, color, 1, cv2.LINE_AA, False)
        txt = 'D [' + str(Body.diam) + ']=' + str(Body.rD)
        imgc = cv2.putText(newimg, txt, (10, newimg.shape[0] - line3), font, fontscale, color, 1, cv2.LINE_AA, False)

        Ok = True
        pos = 4


        #מוסיף טקסטים וגם  משקלל את כל הבדיקות לתוצאה סופית טוב כו לא
        for x in Lugs2:
            if x.ok():
                Ok = Ok
            else:
                Ok = False
                imgc = cv2.putText(imgc, "Lug not ok", (400, imgtoshow.shape[0] - to_pixel(5)), font, 1,
                                   (0, 0, 255), 2, cv2.LINE_AA)

            imgc = cv2.putText(imgc, f'Lug{pos - 3}:', (int(imgc.shape[1] * (pos / 8)), imgc.shape[0] - line1),
                               cv2.FONT_HERSHEY_SIMPLEX, fontscale,
                               darkblue, 1, cv2.LINE_AA, False)
            color = white if x.W.ok() else red
            txt = "W [{}]={}".format(round(x.W.workspec, 2), round(x.W.actual, 3))
            imgc = cv2.putText(imgc, txt, (int(imgc.shape[1] * (pos / 8)), imgc.shape[0] - line2),
                               cv2.FONT_HERSHEY_SIMPLEX, fontscale,
                               color, 1, cv2.LINE_AA, False)
            color = white if x.Y.ok() else red
            txt = "Y [{}]={}".format(round(x.Y.workspec, 2), round(x.Y.actual, 3))
            imgc = cv2.putText(imgc, txt, (int(imgc.shape[1] * (pos / 8)), imgc.shape[0] - line3),
                               cv2.FONT_HERSHEY_SIMPLEX, fontscale,
                               color, 1, cv2.LINE_AA, False)
            color = white if x.H.ok() else red
            txt = "H [{}]={}".format(round(x.H.workspec, 2), round(x.H.actual, 3))
            imgc = cv2.putText(imgc, txt, (int(imgc.shape[1] * (pos / 8)), imgc.shape[0] - line4),
                               cv2.FONT_HERSHEY_SIMPLEX, fontscale,
                               color, 1, cv2.LINE_AA, False)
            pos += 1

        # ממשיך עם חורים
        for x in Grooves2:
            if x.ok():
                Ok = Ok
            else:
                Ok = False
                imgc = cv2.putText(imgc, "Groove not ok", (400, imgtoshow.shape[0] - to_pixel(1)), font, 1,
                                   (0, 0, 255), 2, cv2.LINE_AA)

            imgc = cv2.putText(imgc, f'Groove{pos - 5}:', (int(imgc.shape[1] * (pos / 8)), imgc.shape[0] - line1),
                               cv2.FONT_HERSHEY_SIMPLEX, fontscale, darkblue, 1, cv2.LINE_AA, False)
            color = white if x.WL.ok() else red
            txt = "WL [{}]={}".format(round(x.WL.workspec, 2), round(x.WL.actual, 3))
            imgc = cv2.putText(imgc, txt, (int(imgc.shape[1] * (pos / 8)), imgc.shape[0] - line2),
                               cv2.FONT_HERSHEY_SIMPLEX, fontscale,
                               color, 1, cv2.LINE_AA, False)
            color = white if x.TL.ok() else red
            txt = "TL [{}]={}".format(round(x.TL.workspec, 2), round(x.TL.actual, 3))
            imgc = cv2.putText(imgc, txt, (int(imgc.shape[1] * (pos / 8)), imgc.shape[0] - line3),
                               cv2.FONT_HERSHEY_SIMPLEX, fontscale,
                               color, 1, cv2.LINE_AA, False)
            color = white if x.WR.ok() else red
            txt = "WR [{}]={}".format(round(x.WR.workspec, 2), round(x.WR.actual, 3))
            imgc = cv2.putText(imgc, txt, (int(imgc.shape[1] * (pos / 8)), imgc.shape[0] - line4),
                               cv2.FONT_HERSHEY_SIMPLEX, fontscale,
                               color, 1, cv2.LINE_AA, False)
            color = white if x.TR.ok() else red
            txt = "TR [{}]={}".format(round(x.TR.workspec, 2), round(x.TR.actual, 3))
            imgc = cv2.putText(imgc, txt, (int(imgc.shape[1] * (pos / 8)), imgc.shape[0] - line5),
                               cv2.FONT_HERSHEY_SIMPLEX, fontscale,
                               color, 1, cv2.LINE_AA, False)

            pos += 1

        pos = 1
        #ממשיך עם  תעלות שמן
        for x in Holes2:
            if x.ok():
                Ok = Ok
            else:
                Ok = False
                imgc = cv2.putText(imgc, "Holes not ok", (400, imgtoshow.shape[0] - to_pixel(3)), font, 1, (0, 0, 255),2,cv2.LINE_AA)

            imgc = cv2.putText(imgc, f'Hole{pos}:', (int(imgc.shape[1] * (pos / 8)), imgc.shape[0] - line1),
                               cv2.FONT_HERSHEY_SIMPLEX, fontscale, darkblue, 1, cv2.LINE_AA, False)
            color = white if x.L.ok() else red
            txt = "L [{}]={}".format(round(x.L.workspec, 2), round(x.L.actual, 3))
            imgc = cv2.putText(imgc, txt, (int(imgc.shape[1] * (pos / 8)), imgc.shape[0] - line2),
                               cv2.FONT_HERSHEY_SIMPLEX, fontscale,
                               color, 1, cv2.LINE_AA, False)
            color = white if x.W.ok() else red
            txt = "W [{}]={}".format(round(x.W.workspec, 2), round(x.W.actual, 3))
            imgc = cv2.putText(imgc, txt, (int(imgc.shape[1] * (pos / 8)), imgc.shape[0] - line3),
                               cv2.FONT_HERSHEY_SIMPLEX, fontscale,
                               color, 1, cv2.LINE_AA, False)
            color = white if x.X.ok() else red
            txt = "X [{}]={}".format(round(x.X.workspec, 2), round(x.X.actual, 3))
            imgc = cv2.putText(imgc, txt, (int(imgc.shape[1] * (pos / 8)), imgc.shape[0] - line4),
                               cv2.FONT_HERSHEY_SIMPLEX, fontscale,
                               color, 1, cv2.LINE_AA, False)

            color = white if x.Y.ok() else red
            txt = "Y [{}]={}".format(round(x.Y.workspec, 2), round(x.Y.actual, 3))
            imgc = cv2.putText(imgc, txt, (int(imgc.shape[1] * (pos / 8)), imgc.shape[0] - line5),
                               cv2.FONT_HERSHEY_SIMPLEX, fontscale,
                               color, 1, cv2.LINE_AA, False)

            pos += 1

        #משקלל קיום התראות לתוצאה הסופית
        if len(Alerts) == 0:
            Ok = Ok
        else:
            Ok = False


        okk = 'Ok' if Ok else 'X'
        clr = (255, 0, 0) if Ok else (0, 0, 255)
        imgc = cv2.putText(imgc, okk, (300, imgtoshow.shape[0] - to_pixel(1)), font, 4, clr, 4, cv2.LINE_AA)
        now = time.localtime()
        imgc = cv2.putText(imgc,
                           f'{Cfg_mv.workstationID}  |  {Cfg_mv.JobNbr}  |  No. {Cfg_mv.counter}    |  {time.strftime("%d.%m.%y %H:%M", now)}',
                           (newimg.shape[1] - 480, newimg.shape[0] - to_pixel(0.5)), font, 0.45, white, 1, cv2.LINE_AA)

        for l in Lugs2:
            okk = 'lug.Ok' if l.ok() else 'lug.X'
            clr = (255, 0, 0) if l.ok() else (0, 0, 255)

            px = newimg.shape[1] - Cfg_mv.CropX0 + 20
            py = Cfg_mv.CropY0 + to_pixel(l.Y.actual) + to_pixel(l.W.actual)*0
            imgc = cv2.putText(imgc, okk, (px, py), font, 0.75, clr, 2, cv2.LINE_AA)

        nbr = 0
        for i in Alerts:
            imgc = cv2.rectangle(imgc, (i.x1, i.y1), (i.x2, i.y2), (0, 0, 255), 2)
            nbr += 1
            imgc = cv2.putText(imgc, str(nbr), (min(i.x1, i.x2), min(i.y1, i.y2) - 10), font, 1, (0, 0, 255), 2,
                               cv2.LINE_AA)

        if Ok:
            Ok = 1
        else:
            Ok = 0

        cv2.imwrite(f"dev_img\\8.1imgc.jpg", imgc)
        return Ok,imgc.copy()





    except Exception as e:
        print(' Data_processing Not responding ')
        err_txt = ' Data_processing Not responding '
        show_error(err_txt)


def run_prog():
    global num
    print('---------start image processing---------')
    try:
        Cfg_mv.counter+=1
        Cfg_mv.sttprog = 1
        t0 = time.time()

        # מנתח את התמונה ומקבל החלטות טוב כן או לא
        imgtoshow = Analize_Process()
        if imgtoshow is None:
            print("!!! Analize_Process returned None")
            Cfg_mv.sttprog = 0
            return

        if pprint:print('Analize time:', time.time() - t0)
        # מבצע אנימציות משלימות  לתמונה
        result, img_toShow = Data_processing(imgtoshow)
        if result is None:
            print("!!! Data_processing returned None")
            Cfg_mv.sttprog = 0
            return
        img.img_toShow=img_toShow
        #cv2.imwrite(f"dev_img\\9.1img_toShow {num} .jpg", img.img_toShow )
        cv2.imwrite(f"results\\img_toShow {Cfg_mv.counter} .jpg", img.img_toShow)
        okk = result
        if pprint:print('data process time:', time.time() - t0)

        Cfg_mv.sttprog = 2 if okk == 1 else 3



    except Exception:
        print("!!! run_prog crashed")
        traceback.print_exc()
        Cfg_mv.sttprog = 0


def plc_connect():
    global plc

    plc = ModbusClient()
    plc.host="192.168.3.76"
    plc.port=502
    plc.unit_id=1
    plc.timeout=1.0
    # x= ModbusClient(host=1,port=2,unit_id=3,timeout=4,debug=False,auto_open=True,auto_close=True)
    # managing TCP sessions with call to c.open()/c.close()
    plc.open()
    regs = plc.read_holding_registers(0, 9)
    if pprint:print('regs',regs)

    if regs:
        return True
    else:
        return False


def loop1():
    global cnt2
    state = 0     # 0: not started, 1: running, 2: finished
    t2 = None

    regs2 = plc.read_holding_registers(0, 9)

    while not stop_event.is_set():
        t0 = time.perf_counter()

        img.refresh()


        if  state == 0 and img.status == 3:
            t20 = time.perf_counter()
            if pprint:print('t20',t20)
            state = 1
        if state == 1  and img.status == 4 :

            plc.write_multiple_registers(0, [1, 0, 0, 1, 0]) # Green Off, White On
            state = 2
        if state == 2 and img.WhiteOn == 1 :
            plc.write_multiple_registers(0, [1, 1, 1, 0, 1]) # Green on,Conveyor On
            t2 = threading.Thread(target=run_prog, daemon=True)
            t2.start()
            state = 3
            img.status = 0


        if state == 3 and t2 is not None and not t2.is_alive(): #analyze is completed

            #plc.write_multiple_registers(1, [1, 2, 3])  # PUt Good Not Good on last product to PLC
            t30 = time.perf_counter()-t20
            if pprint:print('//////analize results send to plc//////    cycle time:',t30)
            if pprint:print('Cfg.sttprog', Cfg_mv.sttprog)

            state = 0


        time.sleep(max(0, 0.02- (time.perf_counter() - t0 ) ) )



    def __init__(self, job_number, queue, stop_event, root=None):
        self.queue = queue
        self.stop_event = stop_event

        # אתחול חלון ראשי
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")

        # אם נותנים root חיצוני (לשילוב בתוך UI אחר) – לא יוצרים חלון חדש.
        self.root = root if root is not None else ctk.CTk()
        if root is None:
            self.root.title(f"999-Machine Vision End Line {job_number}")
            try:
                self.root.state("zoomed")  # חלון מקסימלי למסך
            except Exception:
                pass

        # --- טופ-בר עם לוגו, טקסט ושעון ---
        top = ctk.CTkFrame(self.root, fg_color="#444444", height=50)
        top.pack(fill="x", side="top")

        self.logo = ctk.CTkImage(Image.open("img/k2.png"), size=(100, 40))
        ctk.CTkLabel(top, image=self.logo, text="").pack(side="left", padx=10)

        self.clock_label = ctk.CTkLabel(top, text="", font=("Helvetica", 22))
        self.clock_label.pack(side="right", padx=30)

        self.status_icon = [
            ImageTk.PhotoImage(Image.open("img/PlcStt0.png")),
            ImageTk.PhotoImage(Image.open("img/PlcStt1.png")),
            ImageTk.PhotoImage(Image.open("img/PlcStt2.png"))
        ]
        self.lbl_status = ctk.CTkLabel(top, image=self.status_icon[0], text="")
        self.lbl_status.pack(side="right", padx=20)

        ctk.CTkLabel(top, text="Machine Vision End Line", font=("Helvetica", 18, "bold")).pack()

        # --- מסגרת כפתורים בצד שמאל ---
        side = ctk.CTkFrame(self.root, width=250, corner_radius=8)
        side.pack(side="left", fill="y", padx=5, pady=5)

        ctk.CTkButton(side, text="F1 Change Job ✎", command=self.new_job).pack(pady=10)
        ctk.CTkButton(side, text="Settings ⌭", command=self.check_password_settings).pack(pady=10)
        ctk.CTkButton(side, text="Open Images 📂", command=self.open_img_folder).pack(pady=10)
        ctk.CTkButton(side, text="Exit ❌", command=self.exit_app).pack(pady=10)

        self.error_label = ctk.CTkLabel(side, text="", text_color="red", font=("Tahoma", 10), justify="left")
        self.error_label.pack(side="bottom", pady=10)

        # --- אזור תמונות ---
        # חשוב: ה-UI משתמש ב-pack (טופ-בר) + pack (סרגל צד שמאל)
        # לכן ב-place חייבים להתחיל *אחרי* הטופ-בר והסרגל, אחרת התצוגות יעלו על הפקדים.
        images_x = 270   # 250 (side) + מרווח
        images_y0 = 70   # 50 (top) + מרווח
        gap_y = 260

        self.img_live = ctk.CTkLabel(self.root, text="No Live Image")
        self.img_live.place(x=images_x, y=images_y0)

        self.img_native = ctk.CTkLabel(self.root, text="No native Image")
        self.img_native.place(x=images_x, y=images_y0 + gap_y)

        self.img_processed = ctk.CTkLabel(self.root, text="processed Image")
        self.img_processed.place(x=images_x, y=images_y0 + 2 * gap_y)

        # התחלת עדכון UI
        self.last_time = time.time()
        self.update_ui()

    # -------- פונקציות עזר ----------
    def update_ui(self):
        """בודקת תור ומעדכנת מסך – מרוקנת את התור אבל מציירת כל סוג תמונה רק פעם אחת"""

        # נאסוף את התמונה האחרונה מכל סוג
        last_live = None
        last_native = None
        last_processed = None

        while True:
            try:
                msg = self.queue.get_nowait()
            except queue.Empty:
                break

            if not msg:
                continue

            kind = msg[0]
            if kind == "live":  # ( "live", cv2frame )
                last_live = msg[1]
            elif kind == "native":  # ( "native", cv2frame )
                last_native = msg[1]
            elif kind == "processed":  # ( "processed", cv2frame )
                last_processed = msg[1]

        # עכשיו – לכל סוג שיש לו תמונה אחרונה, מציירים פעם אחת
        # גדלים מותאמים לפריסה אנכית, כדי לא לחנוק רוחב במסך משולב.
        if last_live is not None:
            self.set_image(self.img_live, last_live, (520, 240))
        if last_native is not None:
            self.set_image(self.img_native, last_native, (520, 240))
        if last_processed is not None:
            self.set_image(self.img_processed, last_processed, (520, 240))

        # עדכון שעון
        current_time = time.strftime("%H:%M")
        self.clock_label.configure(text=f"🕔 {current_time}")

        # זמן מחזור (רק לניטור, כמו שהיה)
        now = time.time()
        cycle = now - self.last_time
        self.last_time = now
        # print("cycle:", cycle)

        if not self.stop_event.is_set():
            self.root.after(50, self.update_ui)

    def set_image(self, label, cv2_img, size):
        """המרת תמונה מ־OpenCV ל־Tkinter"""
        img_pil = Image.fromarray(cv2.cvtColor(cv2_img, cv2.COLOR_BGR2RGB))
        img_pil = img_pil.resize(size)
        imgtk = ImageTk.PhotoImage(img_pil)
        label.configure(image=imgtk, text="")
        label.image = imgtk

    def show_error(self, txt):
        old = self.error_label.cget("text")
        self.error_label.configure(text=old + "\n" + txt)

    def open_img_folder(self):
        folder = r"W:\prodsoft01\improper_image"
        if os.path.isdir(folder):
            if os.name == "nt":
                subprocess.run(["explorer", os.path.normpath(folder)])
            else:
                subprocess.run(["xdg-open", folder])
        else:
            self.show_error("Folder not found")

    def exit_app(self):
        self.stop_event.set()
        self.root.destroy()
        try:
            plc.write_multiple_registers(0, [1, 0, 0, 0, 0])  # start conveyor 1,2 and turn on green light
        except Exception as e:
            print('[WARN] PLC shutdown write failed:', e)

    # -------- דוגמאות לפופאפ ----------
    def check_password_settings(self):
        box = ctk.CTkToplevel(self.root.winfo_toplevel())
        box.title("Password")
        box.geometry("250x150")
        ctk.CTkLabel(box, text="Enter password:").pack(pady=10)
        entry = ctk.CTkEntry(box, show="*")
        entry.pack(pady=5)

        def submit():
            if entry.get() == "9114":
                messagebox.showinfo("OK", "Access Granted")
                box.destroy()
            else:
                messagebox.showerror("Error", "Wrong password")
                entry.delete(0, "end")

        ctk.CTkButton(box, text="Confirm", command=submit).pack(pady=10)

    def new_job(self):
        box = ctk.CTkToplevel(self.root.winfo_toplevel())
        box.title("New Job")
        box.geometry("250x120")
        ctk.CTkLabel(box, text="Enter Job #:").pack(pady=10)
        entry = ctk.CTkEntry(box)
        entry.pack(pady=5)

        def submit():
            job = entry.get()
            messagebox.showinfo("New Job", f"Job {job} loaded")
            box.destroy()

        ctk.CTkButton(box, text="Confirm", command=submit).pack(pady=10)

    def run(self):
        self.root.mainloop()


def init_system(spec_path=r'config\\last_mv_spec.csv', enable_plc=False):
    """Initialize globals (camera, config, spec, plc).
    This used to run at import time; now it runs only when called."""
    global img, Cfg, cam, spec, pprint
    img = Image_()
    Cfg = Config()
    cam = cv2.VideoCapture(Cfg.CameraID)
    cam.set(cv2.CAP_PROP_FRAME_WIDTH, Cfg.CameraXpixel)
    cam.set(cv2.CAP_PROP_FRAME_HEIGHT, Cfg.CameraYpixel)

    pprint = 1
    if spec_path:
        try:
            spec = pd.read_csv(spec_path)
            print('spec', spec)
            GetSpec(spec)
        except Exception as e:
            print('[WARN] Failed to load spec CSV:', spec_path, e)
            spec = None

    else:

        spec = None
    if 0==1:

        try:
            plc_connect()
            plc.write_multiple_registers(0, [1,1,1,0,1])  # start conveyor 1,2 and turn on green light
        except Exception as e:
            print('[WARN] PLC init failed:', e)
    return {'img': img, 'Cfg': Cfg, 'cam': cam, 'spec': spec}


Cfg_mv=Config()
Filename='M0557U-SI.csv'
init_system('W:\\MachineVisionTemplates\\'+Filename)
#run_prog()

