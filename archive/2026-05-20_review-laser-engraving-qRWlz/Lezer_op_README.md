# Lezer_op.py — מדריך שימוש ותחזוקה

מערכת אוטומציה מקצה לקצה לחריטת טקסט במכשיר לייזר באמצעות תוכנת **EzCad2**.
המשתמש מזין טקסט ופרמטרים בממשק גרפי, והקוד מבצע אוטומטית את כל שלבי ההכנה וההעלאה לתוכנת הלייזר.

---

## תוכן עניינים

1. [סקירה כללית](#סקירה-כללית)
2. [דרישות מקדימות](#דרישות-מקדימות)
3. [קבצים נדרשים](#קבצים-נדרשים)
4. [הפעלה](#הפעלה)
5. [שדות הממשק](#שדות-הממשק)
6. [שלבי התהליך](#שלבי-התהליך)
7. [מערכת הלוגים](#מערכת-הלוגים)
8. [טבלת ID של פקדים ב-EzCad2](#טבלת-id-של-פקדים-ב-ezcad2)
9. [פתרון תקלות](#פתרון-תקלות)
10. [מבנה הקוד](#מבנה-הקוד)
11. [הרחבה ותחזוקה](#הרחבה-ותחזוקה)

---

## סקירה כללית

הקוד מבצע את הזרימה הבאה:

```
משתמש מזין טקסט בממשק Qt
        ↓
שמירה ל-config_temp.json
        ↓
בניית קובץ SVG עם הטקסט
        ↓
המרה ל-DXF דרך Inkscape (CLI)
        ↓
הפעלת EzCad2
        ↓
שליטה ב-GUI של EzCad2 דרך Win32 API:
  - ייבוא ה-DXF
  - הגדרת סיבוב (אופציונלי)
  - הגדרת Power, Loops, Size
  - הפעלת Red Light לתצוגה מקדימה
```

**למה Win32 API?** ל-EzCad2 אין API ציבורי לאוטומציה, אז הקוד "מחקה" לחיצות עכבר והקלדות דרך הודעות Windows ישירות לפקדים.

---

## דרישות מקדימות

### תוכנות

| תוכנה | גרסה | תפקיד |
|-------|------|--------|
| Python | 3.9+ | הרצת הקוד |
| Inkscape | 1.0+ | המרת SVG ל-DXF |
| EzCad2 (Lite) | - | תוכנת הלייזר |
| Windows | 10/11 | חובה — הקוד משתמש ב-Win32 API |

### חבילות Python

```bash
pip install pywin32 PySide6
```

### גופן

חייב להיות מותקן במערכת: **`CNC Vector`**
(הגופן הסטנדרטי לחריטה וקטורית; ללא זה הטקסט לא ייווצר נכון).

---

## קבצים נדרשים

הקוד מצפה לנתיבים הקבועים הבאים (ניתן לשנות בראש הקובץ):

```python
INKSCAPE     = r"C:\Program Files\Inkscape\bin\inkscape.com"
DEFAULT_SVG  = r"C:\Users\lz1\AppData\Roaming\inkscape\templates\default.svg"
EZCAD_EXE    = r"C:\Users\lz1\Desktop\LAZER\EzCad_program\Program\EzCad2.exe"
```

קבצים אופציונליים:
- `logo.png` — באותה תיקייה כמו הקוד; יוצג בראש הממשק אם קיים.

קבצים שנוצרים אוטומטית בזמן הריצה (באותה תיקייה):
- `config_temp.json` — הפרמטרים מהממשק
- `config_temp.svg` — הטקסט בפורמט וקטורי
- `config_temp.dxf` — הקובץ שנטען ל-EzCad2

---

## הפעלה

מומלץ להריץ מ-Command Prompt או PowerShell כדי לראות את לוגי התהליך:

```bash
python Lezer_op.py
```

> אם מריצים בלחיצה כפולה (`pythonw.exe`), הלוגים לא יוצגו.

---

## שדות הממשק

| שדה | סוג | הסבר |
|-----|-----|------|
| TEXT 1 | חופשי | שורת טקסט ראשונה |
| TEXT 2 | חופשי | שורת טקסט שנייה (אופציונלי) |
| X (fixed) | קבוע: -10 | מיקום X על לוח החריטה |
| Y (fixed) | קבוע: 297 | מיקום Y על לוח החריטה |
| HEIGHT (fixed) | קבוע: 4 | גובה הגופן ביחידות SVG |
| LOOPS | חופשי | כמה פעמים הלייזר חורט (יותר = עמוק יותר) |
| POWER | חופשי | עוצמת הלייזר באחוזים |
| SIZE X | חופשי | רוחב סופי של הטקסט ב-EzCad2 |
| SIZE Y | חופשי | גובה סופי של הטקסט ב-EzCad2 |
| ROTATION | 0 / 90 | זווית סיבוב |

> השדות `X`, `Y`, `HEIGHT` נעולים כיוון שהם מותאמים למיקום הפיזי של המוצר על מכשיר הלייזר. שינוי שלהם יוביל לחריטה בלא נכון.

---

## שלבי התהליך

לחיצה על **Save & Run** מפעילה את `run_flow()`, שמבצעת בסדר הבא:

| # | שלב | פונקציה |
|---|-----|---------|
| 1 | קריאת `config_temp.json` | `json.loads` |
| 2 | בניית `config_temp.svg` | `build_svg()` |
| 3 | המרה ל-`config_temp.dxf` | `svg_to_dxf()` (קורא ל-Inkscape) |
| 4 | הפעלת EzCad2 | `subprocess.Popen` |
| 5 | מציאת החלון הראשי | `find_main_by_title_sub()` |
| 6 | שליחת פקודת ייבוא | `WM_COMMAND` + `IMPORT_ID` |
| 7 | המתנה לדיאלוג Open | `find_open_dialog_for_pid()` |
| 8 | הזנת נתיב ה-DXF + Open | `WM_SETTEXT` + `BM_CLICK` |
| 9 | לחיצה על "Use Default" אם מופיע | `BM_CLICK` |
| 10 | סיבוב (אם נבחר 90°) | פתיחת Transform dialog → בחירת Rotation → הזנת זווית → Apply → סגירה |
| 11 | הזנת Power | `set_edit_value()` |
| 12 | הזנת Loops | `set_edit_value()` |
| 13 | הזנת Size X/Y | `set_edit_value()` |
| 14 | המתנה 3 שניות (קריטי — EzCad מעדכן ויזואלית) | `time.sleep(3)` |
| 15 | לחיצה על Apply בפאנל הצדדי | `BM_CLICK` + הודעת `BN_CLICKED` |
| 16 | לחיצה על Red Light (תצוגה מקדימה על החומר) | `BM_CLICK` על הפקד |
| 17 | סיום | `[DONE]` |

---

## מערכת הלוגים

הקוד מדפיס בקונסול אחרי כל שלב סטטוס ברור:

| תווית | משמעות | התנהגות |
|-------|--------|---------|
| `[START]` | תחילת התהליך | - |
| `[...]` | פעולה בתהליך | - |
| `[OK]` | פעולה הושלמה בהצלחה | ממשיך |
| `[WARN]` | פקד אופציונלי לא נמצא | **ממשיך** — לא קריטי |
| `[ERROR]` | פעולה קריטית נכשלה | **עוצר מיד** (return) |
| `[DONE]` | התהליך הסתיים בהצלחה | - |

### דוגמת פלט תקין

```
[START] Reading config: config_temp.json
[OK] Config loaded — text1='Hello' text2='World' x=-10.0 y=297.0 h=4.0 rot=0 ...
[...] Building SVG...
[OK] SVG created: config_temp.svg
[...] Converting SVG to DXF via Inkscape...
[OK] DXF created: config_temp.dxf
[...] Launching EzCad2...
[OK] EzCad2 launched (PID=12345)
[OK] Main window found (handle=987654)
[OK] Open dialog found (handle=...)
[OK] DXF path set: config_temp.dxf
[OK] Open button clicked
[OK] 'Use Default' button clicked
[OK] Power set to 50
[OK] Loops set to 3
[OK] Size X set to 30
[OK] Size Y set to 5
[OK] Main Apply clicked
[OK] Red Light button clicked
[DONE] Flow completed.
```

---

## טבלת ID של פקדים ב-EzCad2

ה-ID-ים נחקרו עם **Spy++** (Microsoft) מול הגרסה הספציפית של EzCad-Lite שמותקנת במכונה. אם תוחלף גרסת EzCad — חלקם עשויים להשתנות.

| קבוע | ערך | מה זה |
|------|-----|-------|
| `IMPORT_ID` | 32807 | פריט תפריט "Import" בחלון הראשי |
| `PATH_EDIT_ID` | 1152 | שדה הנתיב בדיאלוג Open של Windows |
| `OPEN_BTN_ID` | 1 | כפתור Open בדיאלוג (סטנדרטי) |
| `USE_DEFAULT_ID` | 3032 | "Use Default" שמופיע אחרי ייבוא |
| `POWER_EDIT_ID` | 1432 | שדה Power בפאנל הימני |
| `LOOPS_EDIT_ID` | 1009 | שדה Loops |
| `SIZE_X_EDIT_ID` | 1119 | שדה Size X |
| `SIZE_Y_EDIT_ID` | 1120 | שדה Size Y |
| `MAIN_APPLY_ID` | 1105 | כפתור Apply בפאנל הראשי |
| `TRANSFORM_CMD_ID` | 32818 | פריט תפריט "Transform" |
| `ROTATION_BTN_ID` | 1261 | רדיו "Rotation" בדיאלוג Transform |
| `ANGLE_EDIT_ID` | 1390 | שדה זווית בדיאלוג Transform |
| `TRANSFORM_APPLY_ID` | 1105 | Apply בדיאלוג Transform |
| `POS_X_EDIT_ID` | 1066 | שדה X בדיאלוג Transform |
| `POS_Y_EDIT_ID` | 1081 | שדה Y בדיאלוג Transform |
| `RED_LIGHT_CMD_ID` | 17023 | כפתור Red Light (תצוגה מקדימה) |

---

## פתרון תקלות

### `[ERROR] Main window not found`
- EzCad2 לא הצליח להיפתח, או הכותרת השתנתה.
- **בדיקה:** ודא ש-`EZCAD_EXE` נכון, וש-`TITLE_SUB="EzCad-Lite  - No title"` עדיין תואם (יש שני רווחים בין "Lite" ל-"-"!).

### `[ERROR] Open dialog not found`
- EzCad לא הגיב לפקודת הייבוא תוך 8 שניות.
- **בדיקה:** ייתכן שהמערכת איטית — נסה להגדיל את ה-`time.sleep(1.2)` שאחרי `Popen` ל-2 או יותר.

### `[ERROR] Red Light button not found`
- הכפתור הוא פקד ויזואלי בפאנל הצד; הוא **לא** פריט תפריט.
- **בדיקה:** ודא שהפקד עם ID 17023 גלוי על המסך לפני שהקוד מגיע לשלב הזה. אם הפאנל מוסתר — הקוד לא ימצא אותו.

### `[WARN] Power/Loops/Size field not found`
- ייתכן ש-EzCad לא טען את הקובץ במלואו לפני שהקוד התחיל לחפש.
- **פתרון:** הגדל את ה-`time.sleep(0.6)` שאחרי לחיצת Open.

### הטקסט לא מופיע ב-EzCad
- ודא שהגופן `CNC Vector` מותקן במערכת.
- בדוק ש-`config_temp.dxf` נוצר תקין (נסה לפתוח אותו ידנית).

### `subprocess.CalledProcessError` ב-Inkscape
- **בדיקה:** ודא שהנתיב `INKSCAPE` נכון. גרסאות חדשות של Inkscape יכולות לדרוש `.exe` במקום `.com`.

---

## מבנה הקוד

```
Lezer_op.py
├── קבועים          # נתיבים, IDs של פקדי EzCad
├── פונקציות עזר Win32:
│   ├── _enum_windows_of_pid()        # רשימת חלונות לפי PID
│   ├── find_in_pid_by_id()           # מציאת פקד לפי ID
│   ├── find_main_by_title_sub()      # מציאת חלון EzCad הראשי
│   ├── find_open_dialog_for_pid()    # מציאת דיאלוג Open
│   ├── set_edit_value()              # הזנת ערך לשדה טקסט + עדכון
│   ├── find_transform_dialog()       # מציאת דיאלוג Transform
│   └── close_transform_dialog()      # סגירת Transform
├── פונקציות SVG/DXF:
│   ├── build_svg()                   # יצירת SVG מטקסט
│   └── svg_to_dxf()                  # המרה דרך Inkscape
├── run_flow()                        # התהליך המלא (כל השלבים)
└── UI (PySide6)                      # ממשק המשתמש
    ├── __init__()                    # בניית החלון
    └── go()                          # שמירה והפעלה
```

---

## הרחבה ותחזוקה

### הוספת שדה חדש לממשק
1. הוסף `QLineEdit` ב-`UI.__init__()`.
2. הוסף את הערך ל-`data` בתוך `UI.go()`.
3. שלוף אותו ב-`run_flow()` עם `d.get("שם_השדה")`.

### הוספת לחיצה על פקד חדש ב-EzCad2
1. מצא את ה-ID של הפקד עם **Spy++** (חינמי מ-Microsoft, חלק מ-Visual Studio).
2. הוסף קבוע בראש הקובץ.
3. השתמש בתבנית הבאה:

```python
btn = find_in_pid_by_id(proc.pid, NEW_BUTTON_ID)
if btn:
    win32gui.SendMessage(btn, win32con.BM_CLICK, 0, 0)
    print("[OK] New button clicked")
else:
    print("[WARN] New button not found")
```

> **שים לב:** פקדים ויזואליים (כפתורים, שדות) דורשים `find_in_pid_by_id` + `BM_CLICK`.
> פריטי תפריט דורשים `PostMessage(main, WM_COMMAND, MENU_ID, 0)`.

### שינוי X/Y/HEIGHT הקבועים
- ערכי ברירת המחדל נמצאים ב-`UI.__init__()`: שורות `QLineEdit("-10")`, `QLineEdit("297")`, `QLineEdit("4")`.

---

## הערות חשובות

- **תאימות:** הקוד עובד רק על Windows ועם הגרסה הספציפית של EzCad-Lite שעבורה ה-IDs נחקרו. עדכון EzCad יכול לשבור את האוטומציה.
- **חוסן:** הקוד לא טיפול בכל הקצוות (למשל: אם EzCad כבר פתוח, או אם דיאלוג שגיאה קופץ). הלוגים יעזרו לאתר בעיות כאלה.
- **ביצוע מקבילי:** אסור להפעיל את הקוד פעמיים במקביל — שניהם ינסו לתפעל את אותה חלונית EzCad.
