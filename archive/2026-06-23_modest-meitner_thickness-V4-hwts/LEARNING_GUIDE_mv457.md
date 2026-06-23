# מדריך השתלטות — `mv457_MachineVision.py`

מדריך מפורט למי שלוקח אחריות על קוד ניתוח התמונה.
הקובץ הוא 1822 שורות, מחולק ל-10 שכבות מההפשטה הנמוכה ביותר עד לנקודת ההפעלה.

> בכל שכבה: **מה זה**, **למה זה**, **איפה זה ישבר**, **מה לעשות אז**.

---

## שכבה 1 — עזרי המרה ובדיקה (שורות 19–49, 561, 790)

| פונקציה | תפקיד |
|---|---|
| `to_pixel(mm)` | מ"מ → פיקסלים, עיגול א-סימטרי `+0.51` |
| `to_mm(px)` | פיקסלים → מ"מ |
| `to_mmXY(px, py, img)` | פיקסל בתמונה → מ"מ ביחס למרכז התמונה |
| `to_pixelXY(mmx, mmy, img)` | הפוך |
| `to_pixelXY2(...)` | **העתק מיותר** של `to_pixelXY` — מקור עתידי לבאגים |
| `cos(a)` / `sin(a)` | טריגונומטריה במעלות (עוטף `math.radians`) |
| `inspec(a, r, t)` | האם `a` בטולרנס `±t` סביב `r` |
| `should_check(i, X1, X2, inside)` | האם `i` בטווח (פנימי/חיצוני) — רק ל-`find_surface_defects` |

**המאפיין הקריטי:** `to_pixel`/`to_mm` תלויים ב-`Cfg_mv.resolution` (ברירת מחדל 0.0650 mm/px = 65 מיקרון).

### מתי תשבור לך השכבה הזו

- **שינית מרחק עצם או עדשה** → `resolution` כבר לא נכון → כל מדידה שגויה. **תיקון**: קליברציה עם `Image_calibration` (שורה 540) מול עצם ייחוס של 100 מ"מ.
- **שיניתם את `to_pixelXY` ולא את `to_pixelXY2`** (או להפך) → אזורים מסוימים יעבדו ואחרים לא.

---

## שכבה 2 — מבני נתונים (שורות 194–531)

### `Config` — פרמטרים מ-JSON + דיפולטים

נטען מ-`config/config457_mv.json`. כל שדה חסר ב-JSON → דיפולט בקוד. **סיכון לייצור:** מחיקת שדה ב-JSON לא תזעיק שגיאה.

**פרמטרים שתיגע בהם בשטח:**
- `resolution` — קליברציה
- `CropX0/X1/Y0/Y1` — חיתוך שוליים
- `Lens`, `ObjectDistance`, `Camera_Distance` — גיאומטריה אופטית
- `BCK_color`, `BCK_color_mask` — טווחי BGR לזיהוי רקע ירוק
- `lug_side` — 1 או 0, איזה צד מצולם

**הגלובל `Cfg_mv`** נוצר בסוף הקובץ (שורה 1817). כל הקובץ קורא אליו.

**באג נסתר:** `init_system` משתמש ב-`global Cfg` (שורה 1787) אבל הקובץ קורא ל-`Cfg_mv`. השדה `Cfg` הוא דד-קוד.

### `Image_` — מצב חי של המצלמה

מכונת המצבים ב-`status`:

| status | משמעות |
|---|---|
| 0 | אין עצם / לא מזוהה |
| 1 | ראש נכנס לפריים |
| 2 | עצם כולו בפריים |
| 3 | מגיע למרכז → `NeedStop=1` |
| 4 | נעצר (speed=0), מוכן לצילום |

`get_x()` — חותך רצועה של 5 פיקסלים במרכז, סף 100, מוצא רצפי "שחור".
`update_colors()` — דוגם פיקסל בצד התמונה לזיהוי תאורה ירוקה/לבנה.
`refresh()` — קורא 2× פריים כדי לנקות buffer של OpenCV.

**ישבר לך כאן אם:** תאורה לא יציבה, רעידות מסוע, או שינוי בסביבת אור. **תיקון אופייני:** Otsu אדפטיבי במקום סף קבוע.

### מחלקות גיאומטריות

| מחלקה | שדות | למה זה משונה |
|---|---|---|
| `dimension` | `Spec`, `Tolerance`, `workspec`, `worktolerance`, `actual`, `ignore` | יש הבדל בין `Spec` ל-`workspec` — האחרון מתוקן לפרספקטיבה גלילית |
| `Lug2_` | `H`, `W`, `Y`, `R` (כולם `dimension`) | רגיל |
| `Hole2_` | `L`, `W`, `X`, `Y` | `ok()` נותן יחס מקל ל-W: `actual < workspec*1.2` במקום `inspec` |
| `groove2_` | `WR`, `WL`, `TR`, `TL`, `Y` | `ok()` עובר אם **לפחות צד אחד** תקין |
| `alert_` | רק שדות, אין מתודות | דפקט שזוהה |
| `body_` | `tp`, `diam`, `W`, `T`, `H`, `rW`, `rD` | **שדות כיתה**, לא instance — יש רק `Body` אחד בעולם |

---

## שכבה 3 — `GetSpec` (שורות 51–175)

**שער הכניסה לכל job חדש.** מקבל `pandas.DataFrame` מ-CSV, מפזר לאובייקטים גלובליים.

### פורמט ה-CSV

| object | p1 | p2 | p3 | p4 | p5 | p6 | p7 | p8 | JobNbr |
|---|---|---|---|---|---|---|---|---|---|
| Body | type | diameter | width | thick | inspect_loc | — | — | — | M0557U-SI |
| Lug | type | width | height | location | tol_W | tol_H | tol_Y | — | — |
| Hole | type(1-4) | length | width | X | Y | tol_W | tol_L | tol_XY | — |
| Groove | — | W | T | Y | tol_W | tol_T | tol_Y | — | — |

`p1..p8` הם עמודות גנריות, כל סוג שורה מפרש אחרת. **גמיש אבל מסוכן**.

### `check_loc` — תיקון פרספקטיבה גלילית

הגוף הוא גליל. חור בזווית `alpha` ממרכז הגליל נראה במצלמה במיקום שונה מהשרטוט. הנוסחה משולשים דומים — `Camera_Distance` (`l`) ו-`r = diam/2`.

```python
loc_y = (r * cos(alpha) - r * offsetx * sin(alpha) / l) * l / (l + r * sin(alpha))
```

מופעלת **רק על Holes** — לא על Lugs (ייתכן שזה bug, ייתכן שזה מכוון).

### Hacks שכדאי שתדע

- **תעלת דמה כשאין Grooves:** `if not Grooves2: Grooves2.append(groove2_())` — מקומנט `##temp`. תעלה כל-אפסים תמיד `ok()=True`.
- **`Err` הוא קוד מת:** מחושב, מוחזר, אבל אף אחד לא בודק אותו.
- **`return Body, Grooves, Err`:** מחזיר `Grooves` (ריק!) במקום `Grooves2`. bug ישן, לא משפיע (הכל עובד דרך הגלובלים).

### תרחיש החלפת job

1. אופרטור טוען CSV חדש.
2. `init_system` → `GetSpec(spec)`.
3. `Body`, `Lugs2`, `Holes2`, `Grooves2` נמחקים (שורה 80) ומתמלאים מחדש.
4. `Cfg_mv.JobNbr` מתעדכן.

---

## שכבה 4 — מחזור חיי המצלמה (שורות 286–386)

### `Image_.refresh()`

```python
ret, img0 = cam.read()
ret, img0 = cam.read()   # ניקוי buffer
if not ret:
    # מצלמה מנותקת — שמור no_pic, סטטוס 0
self.last_img = img0
self.update_colors(img0)
if self.GreenOn == 1:
    self.get_x(img0)
```

זה הלב של ה-loop. לולאת `loop1` קוראת לזה כל ~20ms.

### `get_x` — זיהוי מיקום העצם

1. רצועה אנכית של 5 פיקסלים במרכז (`x1..x2`).
2. ממוצע ערוץ G → מערך 1D של עוצמת ירוק.
3. סף 100 → מערך בינארי (0=ירוק רקע, 1=עצם).
4. `np.diff` → התחלות וסופים של רצפים.
5. מיזוג רצפים סמוכים אם הפער `<= to_pixel(7)`.
6. עדכון `status` לפי כמות הרצפים ומיקומם.

**הסכנות:**
- סף קבוע 100 → רגיש לתאורה.
- הפער 7mm — אם יש עצם עם חתוכים יותר רחבים מ-7mm, יזוהה כשני עצמים.

### `update_colors`

דוגם פיקסל ב-`(img.shape[0]/2, img.shape[1]/10)` ובודק יחסי R/G/B.
- ירוק דולק: `G/(R+B+1) > 1.5` AND `G > 150`.
- לבן דולק: כל שלושת הערוצים מאוזנים סביב 35+.

**הסכנה:** מנורת אזעקה אדומה דולקת ליד המצלמה → יזוהה כתאורה ירוקה כבויה והניתוח לא יתחיל.

---

## שכבה 5 — עיבוד מקדים (שורות 565–787)

### `cut_image(white, green)` — חיתוך והסרת פרספקטיבה דו-מימדית

1. **חיתוך לפי `Crop*`** מהגולמית.
2. **מסכה בינארית של ירוק** → דרך `cv2.threshold` הופך לבן⇒שחור⇒לבן.
3. **`cv2.fitLine`** על נקודות הקצה → קו ישר.
4. **חישוב זווית** הקו ביחס לאופק (`arctan2(vy, vx)`).
5. **`warpAffine`** עם `rot_mat` → התמונה מסתובבת כדי שהגוף יהיה אופקי.
6. **חיתוך נוסף** של 40 פיקסלים שוליים שנוצרו מהסיבוב.
7. **חיפוש גבולות `l, r, t, b`** של הגוף (סף 90% מגובה התמונה).
8. **`Body.rW`, `Body.rD`** — שמירת רוחב ועומק נמדדים.
9. **חיתוך סופי** עם `marginX/Y` סביב הגבולות.

**מחזיר** את התמונה הירוקה והלבנה אחרי חיתוך וסיבוב.

**איפה זה ישבר לך:**
- אם הגוף מאוד מוטה (>5°), `fitLine` יחזיר זווית שגויה.
- אם רקע לא הומוגני (צל, סימני שמן), המסכה תיתפס לסימנים האלה.
- כתיבה ל-`dev_img\\...` ב-Windows paths — לא יעבוד על Linux/Mac.

### `Image_alignment(white, green)` — השטחת פרספקטיבה גלילית

הגוף הוא גליל, אז מה שרואים בתמונה זה הקרנה של גליל על מישור. החלק האמצעי מתוח, הקצוות "דחוסים". הפונקציה הופכת את זה ל**איזומטרי** (כאילו הסתכלת מלמעלה).

האלגוריתם:
1. מוצאים `l, r` של הגוף.
2. `center_bearing = (l+r)/2`; ממקמים שיהיה במרכז אופקי של התמונה.
3. `ri = (r-l)/2 - 40` (רדיוס בפיקסלים מינוס שוליים).
4. **לולאה לאורך X** מ-`center_x - ri` עד `center_x + ri`:
   - `CosTeta = sqrt(ri² - (center_x - x)²) / ri` (קוסינוס מקומי).
   - `ky = (1 - CosTeta * ri / ObjectDistance_px) * y3/2` — כמה למתוח בציר Y באותו מיקום.
   - `delta_x` — רוחב הפס הנוכחי (מותאם לפי CosTeta³).
   - חותכים פס `imgWhite[center_y-ky:center_y+ky, x:x+delta_x]`.
   - מתחים אותו ל-`(delta_x, y3)` עם `cv2.resize`.
   - מחברים `np.concatenate` משמאל לימין.
5. בסוף — הצמדה לחלק הימני שלא בלולאה.

**מבוצע פעמיים, פעם לתמונה לבנה ופעם ירוקה.**

**איפה זה ישבר לך:**
- `ObjectDistance` בקונפיג לא תואם — הקצוות יהיו מתוחים פחות/יותר מדי.
- הגוף לא בדיוק במרכז → `ri` שגוי → כל הניתוח מוסט.
- שינוי גוף לעצם לא-גלילי (חרוט, פוליגון) → האלגוריתם פשוט שגוי לחלוטין.

---

## שכבה 6 — אלגוריתמי זיהוי (שורות 797–1241)

### `find_surface_defects(white, toshow)` — דפקטים על פני שטח

1. המרה ל-gray.
2. **Down-sample 3×3** (מטריצה של בלוקים 3×3, ממוצע כל בלוק → פיקסל אחד).
3. עוטף את אזורי תעלות שמן (`Grooves2`) — לא בודק שם.
4. עוטף שוליים — `hide_margin_x/y`.
5. **לולאה כפולה j,i**: בודקים אם `gray[j,i]` שונה ב->20% מ-`gray[j±k,i]` (k=1..4).
6. אם כן → `imgp2[j,i] = 255` (דפקט).
7. עוטף חורים (`Holes2`) — מצייר עיגול שחור על המסכה.
8. **`cv2.connectedComponentsWithStats`** למציאת blobs.
9. סינון: `area>20, w>5, h>8` → נכנס ל-`Alerts`.

**איפה זה ישבר לך:**
- הלולאה הכפולה ב-Python איטית — צוואר בקבוק לזמן מחזור.
- סף `20%` יחסי לבהירות הפיקסל. בעצם כהה → סף קטן → דפקטים שווא.
- `Grooves2` עם ערכי 0 (`##temp` hack) → `y1, y2` יוצאים שווים, פתרון לא הגיוני.

### `Detect_Lugs(green, toshow)` — שיניים בקצה

1. מסכת ירוק `cv2.inRange((0,150,0), (255,255,255))`.
2. הפיכת 0↔1 (1=עצם, 0=רקע).
3. **`sumofg = sum על axis=0`** → סכום אנכי לכל עמודה.
4. סריקה מימין לשמאל למציאת `lug_edge` (התחלת השן) ו-`bear_edge` (התחלת המיסב).
5. סריקה אנכית בצד הימני למציאת `top_edge` ו-`bottom_edge` של המיסב.
6. **`connectedComponentsWithStats`** על העמודה במרכז השן.
7. כל component → שן אחת; ממלא `lg.H.actual`, `lg.W.actual`, `lg.Y.actual`.
8. אם יש יותר components מ-`Lugs2` → שן מיותרת, מסומן באדום.

**איפה זה ישבר לך:**
- ההנחה היא ששיניים נמצאות **רק בצד ימין** של התמונה. אם המצלמה הפוכה (`lug_side`) — שגוי.
- הסף `average_sum/2 > sumofg[i] > 5` לזיהוי שן. רעש בקצוות → false positives.

### `Detect_Holes(green, toshow)` — חורים

1. מסכת ירוק → `connectedComponentsWithStats`.
2. לכל `h ∈ Holes2`:
   - מחזרים מועמדים לפי `distance < 3mm` ממרכז ה-spec.
   - בודקים שטח: `0.15 < area/(W*L) < 1.5`.
3. נמצא: ממלא `h.L.actual, h.W.actual, h.X.actual, h.Y.actual`.
4. סימטריזציה — אם `len_r != len_l` או `len_b != len_t`, מסמן את הגדול יותר (כדי לא לחתוך אם המסכה לא תפסה הכל).
5. ציור עיגול (תוצאה), עיגול ספסיפיקציה ירוק, ציר X+Y בכחול.

**איפה זה ישבר לך:**
- אם spec לא מדויק (`h.X.workspec` שגוי), המועמד הנכון מסוון בגלל `distance < 3mm`.
- הקובץ קורא ל-`h.X.actual` בשורה 1064 לפני שהוא חושב את `h.X.actual` בשורה 1102 — נראה כמו bug. בעצם `h.X.actual` הוא 0 בהתחלה ושימוש בו ב-`y_distance` בקריאה הראשונה תמיד ייכשל.

### `Detect_Grooves(white, toshow)` — תעלות שמן

1. חותך שני פסים בצדי התמונה (`xg1..xg2` שמאל, `xg3..xg4` ימין).
2. `groove_meas(side, 0/1)`:
   - המרה ל-gray, סף `cv2.inRange(20, 200)`.
   - `connectedComponentsWithStats` — תופס את הצללית של התעלה.
   - `T_groove` = רוחב צל (=עומק התעלה במ"מ).
   - `W_groove` = גובה צל בקואורדינטה הקריטית.
3. ממלא `g.WL/WR/TL/TR.actual`.

**איפה זה ישבר לך:**
- ההנחה היא שצל התעלה נראה ב-Range 20–200. שינוי תאורה לבנה → סטיה.
- כל תעלה צריכה להופיע **בשני הצדדים** (שמאל וימין). אם התעלה לא סימטרית — אחד הצדדים יזרוק שגיאה.

---

## שכבה 7 — `Analize_Process` (שורות 1252–1307)

הצינור הראשי. מקבל את `img.img_G` ו-`img.img_W` (תמונות שמורות), מחזיר `imgtoshow` עם כל הציורים.

```
img.img_G, img.img_W
    ↓
cut_image          → imgtw, imgtg (חתוך, מסובב)
    ↓
Image_alignment    → imgtWhite, imgtGreen (השטחה גלילית)
    ↓
bitwise_not + morphology + inRange → mask של רקע
    ↓
imgtoshow = imgtWhite.copy(); imgtoshow[mask] = (0, 255, 0)  # רקע חוזר ירוק
    ↓
Detect_Holes(imgtGreen, imgtoshow)
Detect_Lugs(imgtGreen, imgtoshow)
Detect_Grooves(imgtWhite, imgtoshow)
find_surface_defects(imgtWhite, imgtoshow)
    ↓
return imgtoshow
```

**הסדר חשוב:** `Detect_Holes` קודם כי `find_surface_defects` משתמש ב-`h.X.actual` כדי "למחוק" את אזורי החורים מהבדיקה.

**הסיכון הגדול:** כל `Detect_*` רץ ברצף, על אותם גלובלים. אם תרצה לבדל ב-thread נפרד — תצטרך לעטוף ב-lock.

---

## שכבה 8 — `Data_processing` (שורות 1310–1495)

לוקח את `imgtoshow` המוכנה, מוסיף:
- **קווי גבול** של אזור הבדיקה (קווים ירוקים בקצוות).
- **בלוק טקסט תחתון** (200 פיקסלים גובה) עם מידות Body/Lugs/Grooves/Holes — לבן אם OK, אדום אם לא.
- **OK/X גדול** בצבע מתאים.
- **חותמת**: `workstationID | JobNbr | No. counter | timestamp`.
- **תוויות `lug.Ok` / `lug.X`** ליד כל שן.
- **תיבות אדומות** סביב כל `Alert`.

**החזרה:** `(Ok, imgc)` — `Ok` הוא 0/1, `imgc` היא התמונה הסופית.

**איפה זה ישבר לך:**
- `cv2.putText` עם פונט HERSHEY לא תומך עברית/יוניקוד.
- מיקומי טקסט hard-coded יחסית לגובה תמונה. אם משנים `marginY` או `extra_height`, מטקסט נחתך/חופף.

---

## שכבה 9 — ריצה ייצורית (שורות 1498–1595)

### `run_prog()` — מחזור בודד

```python
Cfg_mv.counter += 1
Cfg_mv.sttprog = 1            # רץ
imgtoshow = Analize_Process()
result, img_toShow = Data_processing(imgtoshow)
img.img_toShow = img_toShow
cv2.imwrite(f"results\\img_toShow {counter} .jpg", ...)
Cfg_mv.sttprog = 2 if result == 1 else 3   # 2=OK, 3=NOK
```

נקרא ב-thread נפרד (`threading.Thread(target=run_prog, daemon=True)`) מ-`loop1`.

### `plc_connect()` — Modbus TCP

```python
plc = ModbusClient(host="192.168.3.76", port=502, unit_id=1, timeout=1.0)
plc.open()
```

קריאת `read_holding_registers(0, 9)` כדי לוודא חיבור.

**אם תתקלה כאן:** בדוק כתובת IP, פיירוול, האם ה-PLC דולק.

### `loop1()` — מכונת המצבים הראשית

```python
state = 0
while not stop_event.is_set():
    img.refresh()

    if state == 0 and img.status == 3:   # התחלת מחזור
        state = 1
    if state == 1 and img.status == 4:   # נעצר
        plc.write([1,0,0,1,0])           # Green Off, White On
        state = 2
    if state == 2 and img.WhiteOn == 1:  # לבן דולק
        plc.write([1,1,1,0,1])           # Green On, Conveyor On
        t2 = Thread(target=run_prog)
        t2.start()
        state = 3
    if state == 3 and not t2.is_alive(): # ניתוח הסתיים
        state = 0

    time.sleep(0.02 - elapsed)           # 50Hz
```

**מקבילות עם ה-PLC:**
- כותב ל-registers 0–4: `[Live, Green, White, Conveyor, ...]`
- המסוע נעצר כש-`img.status == 3` (לא מהקוד הזה — מסומן ב-`NeedStop`).

**איפה זה ישבר לך:**
- אם `t2.is_alive()` נשאר True כי `run_prog` נתקע (חריגה לא נתפסה) — המסוע יישאר עומד.
- `time.sleep(0.02 - elapsed)` יכול להיות שלילי → ScientificError; כעיקרון `max(0, ...)` כבר מגן.

---

## שכבה 10 — אתחול והפעלה (שורות 1784–1819)

### `init_system(spec_path, enable_plc=False)`

יוצר את הגלובלים:
- `img = Image_()`
- `Cfg = Config()` ← **דד-קוד**, הקובץ משתמש ב-`Cfg_mv` הגלובלי הנפרד
- `cam = cv2.VideoCapture(Cfg.CameraID)`
- מגדיר `CAP_PROP_FRAME_WIDTH/HEIGHT`
- `pprint = 1` (debug print כללי)
- `spec = pd.read_csv(spec_path)` → `GetSpec(spec)`
- אם `enable_plc=True` — `plc_connect()` (כרגע **תמיד מושבת**, `if 0==1:` בשורה 1807)

### קוד ברמת המודול (שורות 1817–1819)

```python
Cfg_mv = Config()
Filename = 'M0557U-SI.csv'
init_system('W:\\MachineVisionTemplates\\' + Filename)
```

**זה רץ בעת `import`!** כלומר ברגע ש-`457Main_prog.py` או כל קוד אחר עושה `import mv457_MachineVision as mv`, ה-CSV מנסה להיטען מ-`W:\` — שזה נתיב Windows ספציפי לאתר. **על מחשב הפיתוח שלך זה ייכשל.**

**תיקון מומלץ:** להעביר את שתי השורות האחרונות לתוך `if __name__ == "__main__":` או להשתמש ב-CSV מקומי בפיתוח.

### ה-UI היתום (שורות 1599–1781)

זו **בעיית פרסר חמורה**: יש `def __init__(self, ...)` בשורה 1599 ללא `class` עוטף. הקוד למעשה הופך לפונקציה פנימית של `loop1` (כי `def loop1():` נמשך עד שורה 1595, ואחריה `__init__` בלי הזחה).

**Python מסכים לזה syntactically** רק כי הוא לא מבחין בקונטקסט (זה ייפול אם תקרא ל-`__init__` כפונקציה). פשוט "מת" ולא רץ.

**מה זה היה אמור להיות:** מחלקה `class MvUI:` שמציגה 3 תמונות (חי, גולמי, מעובד) ב-`customtkinter`. החסר: שורת `class MvUI:` בשורה 1598.

**הפעולה הנדרשת:** או להוסיף את שורת ה-`class`, או למחוק את כל ה-UI הזה (כנראה הוחלף בקוד אחר ב-`457Main_prog.py`).

---

## תרחישי תקלה נפוצים — תיקון מהיר

| תסמין | חשד ראשון | איפה לבדוק |
|---|---|---|
| כל מדידה שגויה ב-X% | `resolution` לא תואם הגדרה אמיתית | `config457_mv.json`, או `Image_calibration` |
| חורים לא מזוהים | `h.X.workspec` שגוי לאחר תיקון פרספקטיבה | `check_loc` בתוך `GetSpec`, `Camera_Distance` |
| תאורה מזוהה שגוי | סף ב-`update_colors` | פיקסל הדגימה ב-`(shape[0]/2, shape[1]/10)` |
| מסוע לא נעצר | thread של `run_prog` תקוע | log את `t2.is_alive()` |
| תמונה הפוכה | `lug_side` הפוך | קונפיג, או הפעלת `Image_Flip` בלי טעינה מחדש |
| Crash בפתיחה | `init_system` נכשל לקרוא CSV | נתיב `W:\` לא קיים |
| איטיות בעיבוד | `find_surface_defects` הלולאה הכפולה ב-Python | שקול ל-vectorize עם NumPy |

---

## קבצים נלווים שתחפש

- `config/config457_mv.json` — פרמטרים
- `config/config457_thk.json` — לעובי, לא ל-MV
- `W:\MachineVisionTemplates\*.csv` — ספסיפיקציות
- `dev_img\\*.jpg` — תמונות debug שנוצרות בכל ריצה (`1.x`, `2.x`, ..., `8.x`)
- `results\\img_toShow N.jpg` — תוצאות לארכוב
- `native_img\\native_green/white N.jpg` — תמונות גולמיות לבדיקות manual
- `icons\\parallel.png`, `cone.png`, `no_pic2.png` — אייקונים ל-UI

---

## רשימת תיקונים מומלצים (ב-priority order)

1. **תיקון ה-UI היתום**: או הוספת `class MvUI:` או מחיקה.
2. **העברת `init_system(...)` ל-`if __name__ == "__main__":`** — להפסיק לרוץ ב-import.
3. **הפיכת paths ל-`os.path.join` במקום `\\`** — לעבודה בין פלטפורמות.
4. **הוספת lock סביב הגלובלים** אם מתכננים threading אמיתי.
5. **מחיקת `to_pixelXY2`** (כפילות של `to_pixelXY`).
6. **מחיקת `Cfg`** מ-`init_system` (דד-קוד).
7. **הוספת validation ב-`Config`** — חובה במקום דיפולטים.
8. **vectorization של `find_surface_defects`** — שיפור פי 50–100 במהירות.
