/* בדיקת נירמול צד-לקוח — הרצה: node docs/test_normalize.js */
const N = require("./normalize.js");
let fail = 0;
function eq(got, want, label){
  const g=JSON.stringify(got), w=JSON.stringify(want);
  if(g!==w){ console.error(`✗ ${label}\n   got:  ${g}\n   want: ${w}`); fail++; }
  else console.log(`✓ ${label}`);
}

// שדות בודדים
eq(N.normDate("Tue, 4 Aug",26), "4.8.26", "normDate month name");
eq(N.normDate("9 Jun 2026",0), "9.6.26", "normDate with explicit year");
eq(N.normDate("04/08/2026",0), "4.8.26", "normDate numeric");
eq(N.normTimeRange("5:17–6:17"), "05:17-06:17", "normTimeRange en-dash");
eq(N.normDuration("1:00:04"), "1:00:04", "normDuration hms");
eq(N.normDuration("30:07"), "0:30:07", "normDuration ms");
eq(N.normPace("6'56''/KM"), "6:56", "normPace");
eq(N.numStr("8.65KM"), "8.65", "numStr distance");
eq(N.elevStr(null), "X", "elevStr null -> X");
eq(N.elevStr(52), "52", "elevStr number");

// שורה מלאה — בדיוק מה שהמודל יחזיר משני הצילומים של 4 באוגוסט
const FOUR_AUG = {
  date:"Tue, 4 Aug", time_range:"5:17-6:17", duration:"1:00:04",
  distance_km:8.65, avg_heart_rate:160, avg_pace:"6:56",
  elevation_gain:null, avg_power:151, avg_cadence:161, temperature:25,
};
const row = N.modelJsonToRow(FOUR_AUG, 26);
eq([row.date,row.time,row.duration,row.distance,row.hr,row.pace,row.elevation,row.power,row.cadence,row.temp],
   ["4.8.26","05:17-06:17","1:00:04","8.65","160","6:56","X","151","161","25"],
   "full 4-Aug row reproduced");

console.log(fail? `\n${fail} בדיקות נכשלו` : "\nכל הבדיקות עברו ✅");
process.exit(fail?1:0);
