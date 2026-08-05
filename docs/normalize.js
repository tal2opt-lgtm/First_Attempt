/* נירמול ערכים — זהה ללוגיקה ב-engine/schema.py.
   נטען גם בדפדפן (גלובלי) וגם ב-Node (module.exports) לצורך בדיקות. */
(function(root){
"use strict";

const MONTHS={jan:1,feb:2,mar:3,apr:4,may:5,jun:6,jul:7,aug:8,sep:9,oct:10,nov:11,dec:12};

function normDate(raw, yy){
  if(raw==null) return "";
  const s=String(raw).trim();
  let m=s.match(/(\d{1,2})\s+([A-Za-z]{3,})\.?\s*(\d{4})?/);
  if(m){
    const day=+m[1], mon=MONTHS[m[2].slice(0,3).toLowerCase()];
    if(!mon) return s;
    const year=m[3]?(+m[3])%100:yy;
    return `${day}.${mon}.${year}`;
  }
  m=s.match(/(\d{1,2})[./](\d{1,2})[./](\d{2,4})/);
  if(m) return `${+m[1]}.${+m[2]}.${(+m[3])%100}`;
  return s;
}
function normTimeRange(raw){
  if(raw==null) return "";
  const p=[...String(raw).matchAll(/(\d{1,2}):(\d{2})/g)];
  if(p.length<2) return String(raw).trim();
  const pad=n=>String(n).padStart(2,"0");
  return `${pad(+p[0][1])}:${p[0][2]}-${pad(+p[1][1])}:${p[1][2]}`;
}
function normDuration(raw){
  if(raw==null) return "";
  const n=(String(raw).match(/\d+/g)||[]).map(Number);
  let h,mm,ss;
  if(n.length>=3){[h,mm,ss]=n;} else if(n.length===2){h=0;mm=n[0];ss=n[1];} else return String(raw).trim();
  const pad=x=>String(x).padStart(2,"0");
  return `${h}:${pad(mm)}:${pad(ss)}`;
}
function normPace(raw){
  if(raw==null) return "";
  const n=(String(raw).match(/\d+/g)||[]).map(Number);
  if(n.length<2) return String(raw).trim();
  return `${n[0]}:${String(n[1]).padStart(2,"0")}`;
}
function numStr(raw){
  if(raw==null) return "";
  const m=String(raw).match(/-?\d+(?:\.\d+)?/);
  return m?m[0]:"";
}
function elevStr(raw){
  if(raw==null||raw==="") return "X";
  const m=String(raw).match(/-?\d+/);
  return m?m[0]:"X";
}
function modelJsonToRow(j, yy){
  return {
    date:normDate(j.date,yy),
    time:normTimeRange(j.time_range),
    duration:normDuration(j.duration),
    distance:numStr(j.distance_km),
    hr:numStr(j.avg_heart_rate),
    pace:normPace(j.avg_pace),
    elevation:elevStr(j.elevation_gain),
    power:numStr(j.avg_power),
    cadence:numStr(j.avg_cadence),
    temp:numStr(j.temperature),
  };
}

const api={normDate,normTimeRange,normDuration,normPace,numStr,elevStr,modelJsonToRow};
if(typeof module!=="undefined"&&module.exports){ module.exports=api; }
Object.assign(root, api);
})(typeof window!=="undefined"?window:globalThis);
