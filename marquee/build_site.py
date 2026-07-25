"""Static-site generator for Marquee.

Writes two files into a site directory:
  - data.json   {"generated_at": ..., "venues": [...], "screenings": [...]}
  - index.html  self-contained viewer (inline CSS/JS, no external assets)
                that fetches data.json from the same directory.

Public API:
    build(site_dir, venues, screenings, generated_at) -> None
"""
from __future__ import annotations

import json
from pathlib import Path

__all__ = ["build"]


def build(site_dir, venues: list[dict], screenings: list[dict], generated_at: str) -> None:
    """Write data.json and index.html into site_dir (created if needed).

    Malformed records are tolerated: non-dict entries are dropped here, and the
    page's JS skips any screening without a valid date/title at render time.
    """
    out = Path(site_dir)
    out.mkdir(parents=True, exist_ok=True)

    data = {
        "generated_at": generated_at,
        "venues": [v for v in (venues or []) if isinstance(v, dict)],
        "screenings": [s for s in (screenings or []) if isinstance(s, dict)],
    }
    (out / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str),
        encoding="utf-8",
    )
    (out / "index.html").write_text(_INDEX_HTML, encoding="utf-8")


# ---------------------------------------------------------------------------
# The page. One constant string: no substitution needed (generated_at and all
# content come from data.json at runtime).
# ---------------------------------------------------------------------------

_INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>Marquee · NYC</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  color-scheme:dark;
  --bg:#14161A; --ink:#EAE7DE; --muted:#9BA0AB; --accent:#E2B24C;
  --line:#272B33; --chipbg:#1D2128; --rowline:#20242B; --onaccent:#14161A;
  --disp:"Avenir Next Condensed","Arial Narrow",system-ui,sans-serif;
}
@media (prefers-color-scheme:light){
  :root{
    color-scheme:light;
    --bg:#FAF8F3; --ink:#1C1D22; --muted:#6D7280; --accent:#96690A;
    --line:#E3DFD3; --chipbg:#F0EDE3; --rowline:#ECE8DD; --onaccent:#FAF8F3;
  }
}
html{-webkit-text-size-adjust:100%}
body{background:var(--bg);color:var(--ink);
  font:16px/1.45 -apple-system,BlinkMacSystemFont,system-ui,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:inherit}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
[hidden]{display:none!important}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{transition:none!important;animation:none!important}}

.top{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;
  padding:14px 16px 8px;padding-top:calc(14px + env(safe-area-inset-top))}
h1{font-family:var(--disp);text-transform:uppercase;letter-spacing:.16em;
  font-size:28px;line-height:1;color:var(--accent);font-weight:700}
.tag{color:var(--muted);font-size:11px;letter-spacing:.08em;text-transform:uppercase;margin-top:5px}
.topright{display:flex;align-items:center;gap:10px;flex:0 0 auto}
.count{color:var(--muted);font-size:12px;text-align:right;line-height:1.3;font-variant-numeric:tabular-nums}

.chiprow{display:flex;gap:8px;overflow-x:auto;padding:8px 16px;align-items:center;
  scrollbar-width:none;-webkit-overflow-scrolling:touch}
.chiprow::-webkit-scrollbar{display:none}
.chiprow.bare{flex-wrap:wrap;overflow:visible;padding:0}
.chip{flex:0 0 auto;border:1px solid var(--line);background:var(--chipbg);border-radius:999px;
  padding:9px 14px;font-size:14px;min-height:38px;white-space:nowrap;
  transition:background .15s,color .15s,border-color .15s}
.chip.on{background:var(--accent);border-color:var(--accent);color:var(--onaccent);font-weight:600}
.chip[aria-pressed]{-webkit-tap-highlight-color:transparent}
.preset{border-color:var(--accent);color:var(--accent);font-weight:600}
.preset.ghost{border-color:var(--line);color:var(--muted);font-weight:400}
.sep{flex:0 0 1px;height:22px;background:var(--line);margin:0 2px}
.grouplab{color:var(--muted);font-size:11px;letter-spacing:.1em;text-transform:uppercase;
  font-family:var(--disp);margin:2px 0}

#panel{padding:6px 16px 16px;border-bottom:1px solid var(--line);display:grid;gap:12px}
.frow{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
input[type=search]{flex:1;min-width:0;background:var(--chipbg);border:1px solid var(--line);
  border-radius:10px;padding:10px 12px;color:var(--ink);font-size:16px;-webkit-appearance:none;appearance:none}
input[type=date],select{background:var(--chipbg);border:1px solid var(--line);border-radius:10px;
  padding:8px 10px;color:var(--ink);font-size:16px;min-height:42px;max-width:100%}
.lab{color:var(--muted);font-size:13px}

.dayhead{position:sticky;top:0;z-index:10;background:var(--bg);border-bottom:1px solid var(--line);
  padding:10px 16px 7px;font-family:var(--disp);text-transform:uppercase;letter-spacing:.1em;
  font-size:17px;font-weight:600}
.dn{color:var(--accent)}
.todaytag{margin-left:8px;font-size:10px;letter-spacing:.12em;color:var(--onaccent);
  background:var(--accent);border-radius:4px;padding:2px 6px;vertical-align:2px}
.venuehead{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;padding:14px 16px 4px;
  font-family:var(--disp);text-transform:uppercase;letter-spacing:.08em;font-size:15px;font-weight:600}
.vname a{text-decoration:none;border-bottom:1px dotted var(--muted)}
.vboro{color:var(--muted);font-size:12px;letter-spacing:.08em;font-weight:400}
.catchip{font-size:10px;letter-spacing:.1em;border:1px solid var(--line);color:var(--muted);
  border-radius:4px;padding:1px 6px}

.row{display:flex;gap:12px;padding:10px 16px;border-bottom:1px solid var(--rowline);
  text-decoration:none;align-items:baseline;min-height:44px}
a.row:active{background:var(--chipbg)}
.time{font-variant-numeric:tabular-nums;font-weight:700;flex:0 0 4.6em;font-size:15px}
.time .ap{font-size:10px;font-weight:600;color:var(--muted);margin-left:2px}
.time.late{color:var(--accent)}
.time.late .ap{color:var(--accent)}
.tbd{color:var(--muted);font-weight:400}
.main{flex:1;min-width:0}
.title{display:block;font-weight:600;font-size:16px}
.meta{display:block;color:var(--muted);font-size:13px;margin-top:1px}
.fchip{display:inline-block;border:1px solid var(--line);border-radius:4px;font-size:11px;
  padding:0 5px;color:var(--muted);letter-spacing:.04em}
.fchip.film{border-color:var(--accent);color:var(--accent);font-weight:600}
.series{display:block;color:var(--muted);font-size:12px;font-style:italic;margin-top:1px;opacity:.85}
.go{color:var(--muted);align-self:center;flex:0 0 auto}
.note{padding:40px 16px;color:var(--muted);text-align:center}
footer{padding:24px 16px calc(28px + env(safe-area-inset-bottom));color:var(--muted);font-size:12px;
  text-align:center;border-top:1px solid var(--line);margin-top:24px}
footer a{color:var(--accent);text-decoration:none}

@media (min-width:760px){
  body{max-width:880px;margin:0 auto}
  h1{font-size:34px}
  .chiprow{flex-wrap:wrap;overflow:visible}
}
</style>
</head>
<body>

<header class="top">
  <div class="brand">
    <h1>Marquee</h1>
    <p class="tag">what&#8217;s playing &middot; NYC</p>
  </div>
  <div class="topright">
    <div id="count" class="count" aria-live="polite"></div>
    <button id="filtersBtn" class="chip" data-act="toggle-panel" aria-expanded="false" aria-controls="panel">Filters</button>
  </div>
</header>

<nav class="chiprow" aria-label="Presets and date range">
  <button class="chip preset" data-act="preset-tonight">Tonight late</button>
  <button class="chip preset" data-act="preset-weekend">Weekend indie</button>
  <button class="chip preset ghost" data-act="preset-reset">Everything</button>
  <span class="sep" aria-hidden="true"></span>
  <button class="chip" data-act="range-today" aria-pressed="false">Today</button>
  <button class="chip" data-act="range-week" aria-pressed="false">This week</button>
  <button class="chip" data-act="range-weekend" aria-pressed="false">Weekend</button>
  <button class="chip" data-act="range-all" aria-pressed="false">All</button>
</nav>

<section id="panel" hidden>
  <div class="frow">
    <input id="q" type="search" placeholder="Search titles&#8230;" enterkeyhint="search" aria-label="Search titles" autocomplete="off">
  </div>
  <div class="frow">
    <label class="lab" for="from">From</label><input type="date" id="from">
    <label class="lab" for="to">to</label><input type="date" id="to">
  </div>
  <div class="frow">
    <label class="lab" for="after">Starts</label>
    <select id="after">
      <option value="">Any time</option>
      <option value="17:00">After 5 PM</option>
      <option value="20:00">After 8 PM</option>
      <option value="22:00">After 10 PM</option>
    </select>
    <label class="lab" for="venue">Venue</label>
    <select id="venue"><option value="">All venues</option></select>
  </div>
  <div>
    <p class="grouplab">Day of week</p>
    <div class="chiprow bare" role="group" aria-label="Day of week">
      <button class="chip" data-act="toggle-dow" data-val="1" aria-pressed="false">Mon</button>
      <button class="chip" data-act="toggle-dow" data-val="2" aria-pressed="false">Tue</button>
      <button class="chip" data-act="toggle-dow" data-val="3" aria-pressed="false">Wed</button>
      <button class="chip" data-act="toggle-dow" data-val="4" aria-pressed="false">Thu</button>
      <button class="chip" data-act="toggle-dow" data-val="5" aria-pressed="false">Fri</button>
      <button class="chip" data-act="toggle-dow" data-val="6" aria-pressed="false">Sat</button>
      <button class="chip" data-act="toggle-dow" data-val="7" aria-pressed="false">Sun</button>
    </div>
  </div>
  <div>
    <p class="grouplab">Category</p>
    <div class="chiprow bare" role="group" aria-label="Venue category">
      <button class="chip" data-act="toggle-cat" data-val="doc" aria-pressed="false">Doc</button>
      <button class="chip" data-act="toggle-cat" data-val="rep" aria-pressed="false">Rep</button>
      <button class="chip" data-act="toggle-cat" data-val="indie" aria-pressed="false">Indie</button>
      <button class="chip" data-act="toggle-cat" data-val="micro" aria-pressed="false">Micro</button>
      <button class="chip" data-act="toggle-cat" data-val="museum" aria-pressed="false">Museum</button>
      <button class="chip" data-act="toggle-cat" data-val="dine-in" aria-pressed="false">Dine-in</button>
      <button class="chip" data-act="toggle-cat" data-val="chain" aria-pressed="false">Chain</button>
    </div>
  </div>
  <div>
    <p class="grouplab">Borough</p>
    <div class="chiprow bare" id="boroRow" role="group" aria-label="Borough"></div>
  </div>
  <div>
    <p class="grouplab">Format</p>
    <div class="chiprow bare" role="group" aria-label="Format">
      <button class="chip" data-act="toggle-fmt" data-val="35mm" aria-pressed="false">35mm</button>
      <button class="chip" data-act="toggle-fmt" data-val="70mm" aria-pressed="false">70mm</button>
      <button class="chip" data-act="toggle-fmt" data-val="16mm" aria-pressed="false">16mm</button>
    </div>
  </div>
</section>

<main id="list"><p class="note">Loading&#8230;</p></main>
<footer id="foot"></footer>

<script>
'use strict';
var DATA=null, VEN={}, ROWS=[];
var DAYN=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
var MONN=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
var CATL={doc:'Doc',rep:'Rep',indie:'Indie',micro:'Micro',museum:'Museum','dine-in':'Dine-in',chain:'Chain'};

function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function pad(n){return (n<10?'0':'')+n;}
function dstr(d){return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate());}
function today(){return dstr(new Date());}
function addDays(s,n){var p=s.split('-');return dstr(new Date(+p[0],+p[1]-1,+p[2]+n));}
function isoDow(s){var p=s.split('-');var g=new Date(+p[0],+p[1]-1,+p[2]).getDay();return String(g===0?7:g);}
function endOfWeek(){var g=new Date().getDay();return addDays(today(),(7-g)%7);}
function weekendRange(){var g=new Date().getDay();
  if(g===0)return [today(),today()];
  var sat=addDays(today(),6-g);return [sat,addDays(sat,1)];}
function defaults(){return {q:'',from:today(),to:addDays(today(),14),after:'',dow:[],cat:[],boro:[],fmt:[],venue:''};}
var S=defaults();

/* ---------- URL hash <-> state ---------- */
function stateToHash(){
  var p=new URLSearchParams();
  if(S.q)p.set('q',S.q);
  if(!S.from&&!S.to)p.set('range','all');
  if(S.from)p.set('from',S.from);
  if(S.to)p.set('to',S.to);
  if(S.after)p.set('after',S.after);
  if(S.dow.length)p.set('dow',S.dow.join('.'));
  if(S.cat.length)p.set('cat',S.cat.join('.'));
  if(S.boro.length)p.set('boro',S.boro.join('.'));
  if(S.fmt.length)p.set('fmt',S.fmt.join('.'));
  if(S.venue)p.set('venue',S.venue);
  var h=p.toString();
  history.replaceState(null,'', h?('#'+h):(location.pathname+location.search));
}
function hashToState(){
  var raw=location.hash.replace(/^#/,'');
  if(!raw){S=defaults();return;}
  var p=new URLSearchParams(raw);
  function list(k){var v=p.get(k);return v?v.split('.').filter(Boolean):[];}
  S={q:p.get('q')||'', from:p.get('from')||'', to:p.get('to')||'', after:p.get('after')||'',
     dow:list('dow'), cat:list('cat'), boro:list('boro'), fmt:list('fmt'), venue:p.get('venue')||''};
}

/* ---------- data load ---------- */
fetch('data.json',{cache:'no-store'})
  .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
  .then(init)
  .catch(function(e){
    document.getElementById('list').innerHTML=
      '<p class="note">Could not load data.json ('+esc(e.message)+'). Serve this folder over HTTP.</p>';
  });

function init(data){
  DATA=data;
  (data.venues||[]).forEach(function(v){if(v&&v.slug)VEN[v.slug]=v;});
  ROWS=[];
  (data.screenings||[]).forEach(function(r){
    try{
      if(!r||typeof r!=='object')return;
      if(!/^\d{4}-\d{2}-\d{2}$/.test(r.date||''))return;
      if(!r.title)return;
      if(r.time&&!/^([01]\d|2[0-3]):[0-5]\d$/.test(r.time))r.time=null;
      var v=r.venue?VEN[r.venue]:null;
      r._vkey=r.venue||('~'+(r.venue_raw||'?'));
      r._vname=v?v.name:(r.venue_raw||'Unknown venue');
      r._vsite=(v&&v.website)?v.website:null;
      r._boro=v?(v.borough||null):null;
      r._cat=v?(v.category||null):null;
      r._tl=String(r.title).toLowerCase();
      r._dow=isoDow(r.date);
      ROWS.push(r);
    }catch(e){/* skip malformed record */}
  });
  ROWS.sort(function(a,b){
    if(a.date!==b.date)return a.date<b.date?-1:1;
    var an=a._vname.toLowerCase(), bn=b._vname.toLowerCase();
    if(an!==bn)return an<bn?-1:1;
    var at=a.time||'99:99', bt=b.time||'99:99';
    if(at!==bt)return at<bt?-1:1;
    return a._tl<b._tl?-1:(a._tl>b._tl?1:0);
  });
  buildBoroChips();
  buildVenueSelect();
  hashToState(); syncUI(); render();
  var g=fmtGen(data.generated_at);
  document.getElementById('foot').innerHTML=
    'Updated '+esc(g)+' &middot; Data: <a href="https://repertory.nyc" rel="noopener">repertory.nyc</a> &middot; '+
    'Screen Slate &mdash; support <a href="https://www.screenslate.com" rel="noopener">screenslate.com</a>';
}

function fmtGen(g){
  if(!g)return 'unknown';
  var d=new Date(g);
  if(isNaN(d.getTime()))return String(g);
  var h=d.getHours(), ap=h>=12?'PM':'AM'; h=h%12||12;
  return MONN[d.getMonth()]+' '+d.getDate()+', '+d.getFullYear()+' '+h+':'+pad(d.getMinutes())+' '+ap;
}

function buildBoroChips(){
  var order=['Manhattan','Brooklyn','Queens','Bronx','Staten Island'];
  var seen={};
  (DATA.venues||[]).forEach(function(v){if(v&&v.borough)seen[v.borough]=1;});
  var bs=order.filter(function(b){return seen[b];});
  Object.keys(seen).sort().forEach(function(b){if(order.indexOf(b)<0)bs.push(b);});
  document.getElementById('boroRow').innerHTML=bs.map(function(b){
    return '<button class="chip" data-act="toggle-boro" data-val="'+esc(b)+'" aria-pressed="false">'+esc(b)+'</button>';
  }).join('');
}

function buildVenueSelect(){
  var m={};
  ROWS.forEach(function(r){m[r._vkey]=r._vname;});
  var keys=Object.keys(m).sort(function(a,b){
    var x=m[a].toLowerCase(), y=m[b].toLowerCase();
    return x<y?-1:(x>y?1:0);});
  document.getElementById('venue').innerHTML=
    '<option value="">All venues</option>'+keys.map(function(k){
      return '<option value="'+esc(k)+'">'+esc(m[k])+'</option>';}).join('');
}

/* ---------- filtering ----------
   Screenings whose venue is not in the registry have unknown category/borough;
   they PASS category & borough filters (filters only exclude known mismatches). */
function pass(s){
  if(S.q&&s._tl.indexOf(S.q.toLowerCase())<0)return false;
  if(S.from&&s.date<S.from)return false;
  if(S.to&&s.date>S.to)return false;
  if(S.after&&(!s.time||s.time<S.after))return false;
  if(S.dow.length&&S.dow.indexOf(s._dow)<0)return false;
  if(S.cat.length&&s._cat&&S.cat.indexOf(s._cat)<0)return false;
  if(S.boro.length&&s._boro&&S.boro.indexOf(s._boro)<0)return false;
  if(S.fmt.length){
    var f=(s.format||'').toLowerCase();
    var hit=false;
    for(var i=0;i<S.fmt.length;i++){if(f.indexOf(S.fmt[i].toLowerCase())>=0){hit=true;break;}}
    if(!hit)return false;
  }
  if(S.venue&&s._vkey!==S.venue)return false;
  return true;
}

/* ---------- render (one HTML string, one innerHTML swap) ---------- */
function timeHTML(t){
  if(!t)return '<span class="tbd">&mdash;</span>';
  var h=+t.slice(0,2), m=t.slice(3,5), ap=h>=12?'PM':'AM'; h=h%12||12;
  return h+':'+m+'<span class="ap">'+ap+'</span>';
}
function rowHTML(s){
  var late=!!(s.time&&s.time>='22:00');
  var meta=[];
  if(s.year)meta.push(esc(s.year));
  if(s.director)meta.push(esc(s.director));
  if(s.runtime_min)meta.push(esc(s.runtime_min)+' min');
  var mhtml=meta.join(' <span class="dot">&middot;</span> ');
  if(s.format){
    var film=/\b(8|16|35|70)\s?mm\b/i.test(s.format);
    mhtml+=(mhtml?' ':'')+'<span class="fchip'+(film?' film':'')+'">'+esc(s.format)+'</span>';
  }
  var inner='<span class="time'+(late?' late':'')+'">'+timeHTML(s.time)+'</span>'+
    '<span class="main"><span class="title">'+esc(s.title)+'</span>'+
    (mhtml?'<span class="meta">'+mhtml+'</span>':'')+
    (s.series?'<span class="series">'+esc(s.series)+'</span>':'')+
    '</span>';
  var u=s.ticket_url;
  if(u&&/^https?:\/\//i.test(u)){
    return '<a class="row" href="'+esc(u)+'" rel="noopener">'+inner+
      '<span class="go" aria-hidden="true">&rsaquo;</span></a>';
  }
  return '<div class="row">'+inner+'</div>';
}
function dayLabel(ds){
  var p=ds.split('-');var d=new Date(+p[0],+p[1]-1,+p[2]);
  return '<span class="dn">'+DAYN[d.getDay()]+'</span> &middot; '+MONN[d.getMonth()]+' '+d.getDate();
}
function render(){
  var rows=ROWS.filter(pass);
  var films={};
  rows.forEach(function(r){films[r._tl+'|'+(r.year||'')]=1;});
  var nf=Object.keys(films).length;
  document.getElementById('count').textContent=
    rows.length+' screening'+(rows.length===1?'':'s')+' · '+nf+' film'+(nf===1?'':'s');
  var listEl=document.getElementById('list');
  if(!rows.length){
    listEl.innerHTML='<p class="note">Nothing matches. Loosen a filter, or tap Everything.</p>';
    return;
  }
  var out=[], curD=null, curV=null, tod=today();
  for(var i=0;i<rows.length;i++){
    var s=rows[i];
    if(s.date!==curD){
      if(curV!==null)out.push('</div>');
      if(curD!==null)out.push('</section>');
      curD=s.date; curV=null;
      out.push('<section class="day"><h2 class="dayhead">'+dayLabel(s.date)+
        (s.date===tod?'<span class="todaytag">Today</span>':'')+'</h2>');
    }
    if(s._vkey!==curV){
      if(curV!==null)out.push('</div>');
      curV=s._vkey;
      var vh='<span class="vname">'+
        (s._vsite&&/^https?:\/\//i.test(s._vsite)
          ?'<a href="'+esc(s._vsite)+'" rel="noopener">'+esc(s._vname)+'</a>'
          :esc(s._vname))+'</span>';
      if(s._boro)vh+='<span class="vboro">'+esc(s._boro)+'</span>';
      if(s._cat)vh+='<span class="catchip">'+esc(CATL[s._cat]||s._cat)+'</span>';
      out.push('<div class="venue"><h3 class="venuehead">'+vh+'</h3>');
    }
    out.push(rowHTML(s));
  }
  if(curV!==null)out.push('</div>');
  if(curD!==null)out.push('</section>');
  listEl.innerHTML=out.join('');
}

/* ---------- UI sync ---------- */
function rangeMode(){
  if(!S.from&&!S.to)return 'all';
  if(S.from===today()&&S.to===today())return 'today';
  if(S.from===today()&&S.to===endOfWeek())return 'week';
  var w=weekendRange();
  if(S.from===w[0]&&S.to===w[1])return 'weekend';
  return null;
}
function syncUI(){
  var q=document.getElementById('q');
  if(q.value!==S.q)q.value=S.q;
  ['from','to','after','venue'].forEach(function(id){
    var el=document.getElementById(id);
    if(el.value!==S[id])el.value=S[id];
  });
  var chips=document.querySelectorAll('button[data-act][data-val]');
  for(var i=0;i<chips.length;i++){
    var b=chips[i], act=b.getAttribute('data-act'), v=b.getAttribute('data-val'), on=false;
    if(act==='toggle-dow')on=S.dow.indexOf(v)>=0;
    else if(act==='toggle-cat')on=S.cat.indexOf(v)>=0;
    else if(act==='toggle-boro')on=S.boro.indexOf(v)>=0;
    else if(act==='toggle-fmt')on=S.fmt.indexOf(v)>=0;
    b.classList.toggle('on',on);
    b.setAttribute('aria-pressed',on?'true':'false');
  }
  var mode=rangeMode();
  [['range-today','today'],['range-week','week'],['range-weekend','weekend'],['range-all','all']]
    .forEach(function(pr){
      var b=document.querySelector('button[data-act="'+pr[0]+'"]');
      if(b){var on=mode===pr[1];b.classList.toggle('on',on);b.setAttribute('aria-pressed',on?'true':'false');}
    });
}
function update(){stateToHash();syncUI();render();}
function tog(arr,v){var i=arr.indexOf(v);if(i<0)arr.push(v);else arr.splice(i,1);}

/* ---------- events ---------- */
document.addEventListener('click',function(e){
  var b=e.target.closest('button[data-act]');
  if(!b)return;
  var act=b.getAttribute('data-act'), val=b.getAttribute('data-val'), w;
  if(act==='toggle-panel'){
    var p=document.getElementById('panel'), open=p.hasAttribute('hidden');
    if(open)p.removeAttribute('hidden');else p.setAttribute('hidden','');
    b.setAttribute('aria-expanded',open?'true':'false');
    b.classList.toggle('on',open);
    return;
  }
  if(act==='preset-tonight'){S=defaults();S.from=today();S.to=endOfWeek();S.after='22:00';}
  else if(act==='preset-weekend'){S=defaults();w=weekendRange();S.from=w[0];S.to=w[1];S.after='20:00';
    S.cat=['doc','rep','indie','micro','museum','dine-in'];}
  else if(act==='preset-reset'){S=defaults();}
  else if(act==='range-today'){S.from=today();S.to=today();}
  else if(act==='range-week'){S.from=today();S.to=endOfWeek();}
  else if(act==='range-weekend'){w=weekendRange();S.from=w[0];S.to=w[1];}
  else if(act==='range-all'){S.from='';S.to='';}
  else if(act==='toggle-dow'){tog(S.dow,val);}
  else if(act==='toggle-cat'){tog(S.cat,val);}
  else if(act==='toggle-boro'){tog(S.boro,val);}
  else if(act==='toggle-fmt'){tog(S.fmt,val);}
  else return;
  update();
});

var qTimer=null;
document.getElementById('q').addEventListener('input',function(){
  var el=this;
  clearTimeout(qTimer);
  qTimer=setTimeout(function(){S.q=el.value.trim();update();},150);
});
['from','to','after','venue'].forEach(function(id){
  document.getElementById(id).addEventListener('change',function(){S[id]=this.value;update();});
});
window.addEventListener('hashchange',function(){hashToState();syncUI();render();});
</script>
</body>
</html>
"""
