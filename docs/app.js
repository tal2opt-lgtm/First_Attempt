/* מעקב ריצות — לוגיקת צד-לקוח בלבד. קורא ל-Anthropic ישירות מהדפדפן. */
"use strict";

// ---- שדות הטבלה (בסדר העמודות של הקובץ שלך) --------------------------------
const FIELDS = [
  { key: "date",      label: 'תאריך',        ph: "4.8.26" },
  { key: "time",      label: 'שעה',          ph: "05:17-06:17" },
  { key: "duration",  label: 'משך',          ph: "1:00:04" },
  { key: "distance",  label: 'מרחק (ק״מ)',   ph: "8.65" },
  { key: "hr",        label: 'דופק',         ph: "160" },
  { key: "pace",      label: 'קצב',          ph: "6:56" },
  { key: "elevation", label: 'טיפוס (מ׳)',   ph: "X" },
  { key: "power",     label: 'הספק (W)',     ph: "151" },
  { key: "cadence",   label: 'צעדים (SPM)',  ph: "161" },
  { key: "temp",      label: 'טמפ׳ (°C)',    ph: "25" },
];
// כותרות ה-CSV/Excel — זהות לכותרות בגיליון "ריצות" שלך.
const XLSX_HEADERS = ["Date ","Time","Duration[HH:MM:SS]","Distance[Km]",
  "Avg. Heart Rate","Avg. Pace[Km/h]","Elevation Gain[m]","Avg. Power[W]",
  "Avg. Cadence[SPM]","Temeture[C]"];

// ---- 14 הריצות הקיימות שלך (זרע התחלתי) ------------------------------------
const SEED = [
  ["25.5.26","20:36-21:14","0:30:07","5.82","168","6:32","X","159","147","21"],
  ["2.6.26","21:03-21:33","0:30:19","5.22","180","5:58","X","178","160","22"],
  ["9.6.26","20:53-21:24","0:30:51","5.42","182","5:42","31","181","159","24"],
  ["16.6.26","20:50-21:20","0:30:07","5.46","180","5:31","X","184","160","25"],
  ["23.6.26","20:47-21:17","0:30:04","5.56","187","5:24","X","190","156","24"],
  ["30.6.26","21:00-21:46","0:45:26","6.3","160","7:13","X","144","159","26"],
  ["7.7.26","20:56-21:48","0:52:32","7.32","162","7:10","52","142","158","26"],
  ["9.7.26","05:16-06:01","0:45:26","6.46","163","7:02","X","148","162","23"],
  ["14.7.26","20:53-21:49","0:56:15","7.64","160","7:22","51","140","156","27"],
  ["16.7.26","21:03-22:01","0:57:39","7.7","159","7:29","53","139","160","26"],
  ["21.7.26","20:54-21:54","1:00:03","8.1","158","7:25","53","140","159","27"],
  ["28.7.26","21:00-22:01","1:01:02","8.19","158","7:27","51","139","158","27"],
  ["30.7.26","20:54-21:58","1:03:36","8.33","159","7:38","50","136","159","28"],
  ["4.8.26","05:17-06:17","1:00:04","8.65","160","6:56","52","151","161","25"],
];

// פונקציות הנירמול (normDate, modelJsonToRow וכו') מוגדרות ב-normalize.js
// שנטען לפני הקובץ הזה — ולכן זמינות כאן כגלובליות.

// ---- הפרומפט למודל ---------------------------------------------------------
const PROMPT = `You extract data from Apple Fitness / Apple Watch running-workout screenshots.
You may get the "Summary" view and the scrolled "Map/Weather" view of the SAME run. Read all images and combine.
Return ONE JSON object and nothing else (no markdown). Use null for anything not visible — do not guess:
{"date":"e.g. Tue, 4 Aug","time_range":"e.g. 5:17-6:17","duration":"e.g. 1:00:04","distance_km":8.65,"avg_heart_rate":160,"avg_pace":"e.g. 6:56 from 6'56\\"/KM","elevation_gain":null_or_meters_int,"avg_power":151,"avg_cadence":161,"temperature":25}
Rules: distance number only; pace minutes:seconds per km; heart rate = the "Avg. Heart Rate" value; temperature = TEMP value in Celsius (ignore humidity); elevation is usually NOT shown -> null. Report the date text as shown; do not invent a year.`;

// ---- מצב ואחסון ------------------------------------------------------------
const LS_ROWS="runs_v1", LS_KEY="anthropic_key", LS_MODEL="model", LS_YEAR="year";
const $=id=>document.getElementById(id);
let rows = load();
let pending = null; // השורה הממתינה לאישור

function load(){
  try{const r=JSON.parse(localStorage.getItem(LS_ROWS));
    if(Array.isArray(r)&&r.length) return r;}catch(e){}
  return SEED.map(r=>r.slice());
}
function save(){ localStorage.setItem(LS_ROWS, JSON.stringify(rows)); }
function rowToArr(o){ return FIELDS.map(f=>o[f.key]??""); }

// ---- קריאה ל-Anthropic -----------------------------------------------------
async function fileToImage(file){
  const dataUrl = await new Promise((res,rej)=>{
    const fr=new FileReader(); fr.onload=()=>res(fr.result); fr.onerror=rej; fr.readAsDataURL(file);
  });
  const m = /^data:(.+?);base64,(.*)$/.exec(dataUrl);
  const media = (m&&m[1])||file.type||"image/png";
  const data = m?m[2]:dataUrl.split(",")[1];
  return {type:"image", source:{type:"base64", media_type:media, data}};
}
function extractJson(text){
  const m = String(text).match(/\{[\s\S]*\}/);
  if(!m) throw new Error("המודל לא החזיר JSON תקין:\n"+text);
  return JSON.parse(m[0]);
}
async function callModel(images, key, model){
  const content = images.slice();
  content.push({type:"text", text: PROMPT});
  const resp = await fetch("https://api.anthropic.com/v1/messages",{
    method:"POST",
    headers:{
      "x-api-key":key,
      "anthropic-version":"2023-06-01",
      "content-type":"application/json",
      "anthropic-dangerous-direct-browser-access":"true",
    },
    body: JSON.stringify({model, max_tokens:500,
      messages:[{role:"user", content}]}),
  });
  const j = await resp.json().catch(()=>({}));
  if(!resp.ok){
    const msg=(j&&j.error&&j.error.message)||resp.status+" "+resp.statusText;
    throw new Error("שגיאת API: "+msg);
  }
  const txt = (j.content&&j.content[0]&&j.content[0].text)||"";
  return extractJson(txt);
}

// ---- UI: העלאה -------------------------------------------------------------
let files=[];
$("file").addEventListener("change", e=>{
  files=[...(e.target.files||[])].slice(0,4);
  const t=$("thumbs"); t.innerHTML="";
  files.forEach(f=>{const img=document.createElement("img"); img.src=URL.createObjectURL(f); t.appendChild(img);});
  $("extractBtn").disabled = files.length===0;
  if(files.length){
    status("uploadStatus", `נבחרו ${files.length} תמונות ✓ — עכשיו לחץ "חלץ נתונים".`, "ok");
    $("extractBtn").scrollIntoView({behavior:"smooth", block:"center"});
  } else {
    status("uploadStatus","");
  }
});

$("extractBtn").addEventListener("click", async ()=>{
  const key=($("apiKey").value||"").trim();
  if(!key){ status("uploadStatus","קודם הזן מפתח API בהגדרות (⚙️ בתחתית).","err"); $("settings").open=true; return; }
  const model=$("model").value;
  const yy = clampYear($("year").value);
  const btn=$("extractBtn"); btn.disabled=true; btn.innerHTML='<span class="spin"></span>מחלץ...';
  status("uploadStatus","שולח ל-Claude ומחלץ נתונים...","info");
  try{
    const images = await Promise.all(files.map(fileToImage));
    const j = await callModel(images, key, model);
    pending = modelJsonToRow(j, yy);
    showConfirm(pending);
    status("uploadStatus","");
  }catch(err){
    status("uploadStatus", err.message||String(err), "err");
  }finally{
    btn.disabled=false; btn.textContent="חלץ נתונים";
  }
});

// ---- UI: אישור -------------------------------------------------------------
function showConfirm(o){
  const box=$("fields"); box.innerHTML="";
  FIELDS.forEach(f=>{
    const w=document.createElement("div"); w.className="fld";
    const l=document.createElement("label"); l.textContent=f.label;
    const i=document.createElement("input"); i.type="text"; i.value=o[f.key]??""; i.placeholder=f.ph; i.dataset.k=f.key;
    i.addEventListener("input",()=>{pending[f.key]=i.value;});
    w.appendChild(l); w.appendChild(i); box.appendChild(w);
  });
  const dup = rows.some(r=>r[0]===o.date && r[1]===o.time);
  const missing = FIELDS.filter(f=>f.key!=="elevation" && !(o[f.key]||"").trim()).map(f=>f.label);
  let note="בדוק שהמספרים נכונים לפני שמירה.";
  if(o.elevation==="X") note+=' הטיפוס לא זוהה (X) — אם צריך, הקלד ידנית.';
  if(missing.length) note+=` ⚠️ שדות חסרים: ${missing.join(", ")}.`;
  if(dup) note+=" ⚠️ כבר קיימת שורה עם אותו תאריך+שעה.";
  status("confirmNote", note, "warn");
  $("confirmCard").style.display="block";
  $("confirmCard").scrollIntoView({behavior:"smooth"});
}
$("cancelBtn").addEventListener("click",()=>{ pending=null; $("confirmCard").style.display="none"; });
$("addBtn").addEventListener("click",()=>{
  if(!pending) return;
  rows.push(rowToArr(pending));
  save(); render();
  pending=null; $("confirmCard").style.display="none";
  files=[]; $("file").value=""; $("thumbs").innerHTML=""; $("extractBtn").disabled=true;
  status("uploadStatus","✓ הריצה נוספה לטבלה.","ok");
  $("tbl").scrollIntoView({behavior:"smooth"});
});

// ---- UI: טבלה --------------------------------------------------------------
function render(){
  const th=$("thead"); th.innerHTML="";
  FIELDS.forEach(f=>{const c=document.createElement("th"); c.textContent=f.label; th.appendChild(c);});
  th.appendChild(document.createElement("th")); // עמודת מחיקה
  const tb=$("tbody"); tb.innerHTML="";
  rows.forEach((r,idx)=>{
    const tr=document.createElement("tr");
    r.forEach(v=>{const td=document.createElement("td"); td.textContent=v; tr.appendChild(td);});
    const del=document.createElement("td"); del.textContent="✕"; del.className="del"; del.title="מחק שורה";
    del.addEventListener("click",()=>{ if(confirm("למחוק את השורה הזו?")){rows.splice(idx,1); save(); render();} });
    tr.appendChild(del); tb.appendChild(tr);
  });
  $("rowCount").textContent = `(${rows.length} ריצות)`;
}

// ---- ייצוא -----------------------------------------------------------------
function toCsv(withHeader){
  const esc=v=>/[",\n]/.test(v)?'"'+String(v).replace(/"/g,'""')+'"':v;
  const lines = rows.map(r=>r.map(esc).join(","));
  if(withHeader) lines.unshift(XLSX_HEADERS.map(esc).join(","));
  return lines.join("\n");
}
function download(name, text, type){
  const blob=new Blob(["﻿"+text],{type}); const url=URL.createObjectURL(blob);
  const a=document.createElement("a"); a.href=url; a.download=name; a.click();
  setTimeout(()=>URL.revokeObjectURL(url),1500);
}
$("csvBtn").addEventListener("click",()=>download("Runs.csv", toCsv(false), "text/csv"));
$("copyBtn").addEventListener("click",async()=>{
  try{await navigator.clipboard.writeText(toCsv(true)); alert("הועתק ללוח.");}
  catch(e){alert("לא ניתן להעתיק אוטומטית — השתמש בייצוא CSV.");}
});
$("xlsxBtn").addEventListener("click",()=>exportXlsx());

function exportXlsx(){
  const build=()=>{
    const data=[XLSX_HEADERS, ...rows];
    const ws=XLSX.utils.aoa_to_sheet(data);
    const wb=XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, "ריצות");
    XLSX.writeFile(wb, "runs.xlsx");
  };
  if(window.XLSX) return build();
  const s=document.createElement("script");
  s.src="https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js";
  s.onload=build;
  s.onerror=()=>alert("טעינת רכיב ה-Excel נכשלה (אין רשת?). השתמש בייצוא CSV — הוא תואם לתבנית שלך.");
  document.head.appendChild(s);
}

// ---- כללי ------------------------------------------------------------------
function status(id,msg,kind){
  const el=$(id); el.textContent=msg||"";
  el.className="status"+(msg?` show ${kind||"info"}`:"");
}
function clampYear(v){ let y=parseInt(v,10); if(isNaN(y)) y=new Date().getFullYear()%100; return ((y%100)+100)%100; }

// טעינת הגדרות שמורות
(function init(){
  $("apiKey").value = localStorage.getItem(LS_KEY)||"";
  $("model").value  = localStorage.getItem(LS_MODEL)||"claude-sonnet-5";
  $("year").value   = localStorage.getItem(LS_YEAR)|| (new Date().getFullYear()%100);
  $("apiKey").addEventListener("change",e=>localStorage.setItem(LS_KEY,e.target.value.trim()));
  $("apiKey").addEventListener("blur",e=>localStorage.setItem(LS_KEY,e.target.value.trim()));
  $("model").addEventListener("change",e=>localStorage.setItem(LS_MODEL,e.target.value));
  $("year").addEventListener("change",e=>localStorage.setItem(LS_YEAR,e.target.value));
  $("resetBtn").addEventListener("click",()=>{ if(confirm("לאפס את הטבלה ל-14 הריצות ההתחלתיות? כל מה שהוספת יימחק.")){rows=SEED.map(r=>r.slice()); save(); render();} });
  render();
})();
