"""Generate self-contained HTML reports from search/comparison result data.

compare.html — side-by-side row comparison. Row data is embedded as a
gzip+base64 JSON blob and rendered in-browser by a virtual scroller, so the
DOM stays light regardless of row count (single file, opens from file://).
report.html — search-results summary. Rows are rendered directly (few,
variable-height) with the same light visual language.
"""

import base64
import gzip
import html
import json
from datetime import datetime
from pathlib import Path


def _escape(s) -> str:
    return html.escape("" if s is None else str(s))


def _highlight(text, spans, css_class) -> str:
    """HTML-escape `text`, wrapping the given char `spans` in a <mark>.

    Kept for tests / reference; the compare report now highlights in JS.
    """
    if text is None:
        return '<span class="null">—</span>'
    if not spans:
        return _escape(text)

    clean = []
    for s, e in sorted(spans):
        s = max(0, s)
        e = min(len(text), e)
        if e > s:
            clean.append((s, e))
    if not clean:
        return _escape(text)

    out = []
    pos = 0
    for s, e in clean:
        if s < pos:
            s = pos
        if s > pos:
            out.append(_escape(text[pos:s]))
        if e > s:
            out.append(f'<mark class="{css_class}">{_escape(text[s:e])}</mark>')
        pos = max(pos, e)
    if pos < len(text):
        out.append(_escape(text[pos:]))
    return "".join(out)


def _color_for_score(val: float) -> tuple:
    if val >= 0.7:
        return "#2E7D32", "#E8F5E9"
    elif val >= 0.4:
        return "#E65100", "#FFF3E0"
    return "#C62828", "#FFEBEE"


_KIND_RU = {"exact": "точное", "fuzzy": "нечёткое"}


def _pack(payload) -> str:
    """gzip-compress + base64-encode a JSON payload for inlining in HTML."""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, 9)).decode("ascii")


# --------------------------------------------------------------------------- #
# Search-results report (report.html) — direct render, light UI
# --------------------------------------------------------------------------- #

_REPORT_CSS = """
:root{
  --bg:#f5f7fa; --surface:#fff; --text:#1a2233; --muted:#6b7280; --faint:#9aa3b2;
  --rule:#e6e9ef; --rule2:#d8dde6; --accent:#2563eb;
  --exact-bg:#dcfce7; --exact-fg:#166534; --fuzzy-bg:#ffedd5; --fuzzy-fg:#9a3412;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:13px/1.5 system-ui,-apple-system,'Segoe UI',Arial,sans-serif}
header{display:flex;align-items:baseline;gap:14px;padding:12px 20px;background:var(--surface);border-bottom:1px solid var(--rule)}
header h1{font-size:15px;font-weight:650}
header .sub{font-size:11px;color:var(--faint)}
main{max-width:100%;padding:10px 20px 20px;display:flex;flex-direction:column;gap:8px}
.row-wrap{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.pill{display:inline-flex;gap:6px;align-items:center;font-size:11px;color:var(--muted);background:var(--surface);border:1px solid var(--rule);border-radius:999px;padding:3px 10px}
.pill b{color:var(--text)}
.controls{display:flex;gap:12px;align-items:center;background:var(--surface);border:1px solid var(--rule);border-radius:8px;padding:8px 12px}
.controls input[type=text]{border:1px solid var(--rule2);border-radius:6px;padding:5px 9px;font-size:12px;width:240px;outline:none}
.controls input[type=text]:focus{border-color:var(--accent)}
.controls label{font-size:11px;color:var(--muted);display:inline-flex;gap:4px;align-items:center;cursor:pointer}
.tw{background:var(--surface);border:1px solid var(--rule);border-radius:8px;overflow:auto}
table{width:100%;border-collapse:collapse}
thead th{background:#eef2f7;color:var(--muted);font-size:10.5px;font-weight:650;text-transform:uppercase;letter-spacing:.03em;padding:8px 12px;text-align:left;white-space:nowrap;cursor:pointer;user-select:none;border-bottom:1px solid var(--rule2);position:sticky;top:0}
thead th:hover{background:#e3e8f0}
thead th.sorted{color:var(--accent)}
tbody tr{border-bottom:1px solid var(--rule);transition:background .1s}
tbody tr:hover{background:#f0f5ff}
tbody tr.hidden{display:none}
td{padding:7px 12px;font-size:12px;vertical-align:top}
.tcell{max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
.sc{display:flex;align-items:center;gap:6px}
.bar{width:54px;height:5px;background:var(--rule2);border-radius:3px;flex-shrink:0;overflow:hidden}
.fill{height:5px;background:linear-gradient(to right,#C62828,#E65100,#2E7D32)}
.sn{min-width:42px;font-weight:600}
.vc{font-weight:600;text-align:center}
.vc.ok{color:#166534} .vc.warn{color:#9a3412}
.mc,.cc{min-width:200px}
.mrow{display:flex;align-items:center;gap:4px;font-size:11px;line-height:1.6;padding:1px 0}
.mrow .mt{color:var(--accent);font-weight:600;max-width:120px;overflow:hidden;text-overflow:ellipsis}
.mrow .arr{color:var(--faint)}
.mrow .md{max-width:150px;overflow:hidden;text-overflow:ellipsis}
.mrow .mp{font-size:10px;font-weight:600;background:var(--bg);border-radius:3px;padding:0 4px;color:var(--muted)}
.cc .ct{color:var(--accent);font-weight:700;font-size:11px;margin-top:3px}
.crow{display:flex;align-items:center;gap:4px;font-size:11px;line-height:1.6;padding:1px 0 1px 10px}
.crow .arr{color:var(--faint)}
.crow .cd{max-width:150px;overflow:hidden;text-overflow:ellipsis}
.tag{font-size:9px;font-weight:700;text-transform:uppercase;border-radius:3px;padding:0 4px}
.tag-exact{background:var(--exact-bg);color:var(--exact-fg)}
.tag-fuzzy{background:var(--fuzzy-bg);color:var(--fuzzy-fg)}
.cp{font-size:10px;font-weight:600;background:var(--bg);border-radius:3px;padding:0 4px;color:var(--muted)}
.uc{font-size:11px;color:var(--muted);max-width:240px}
.null{color:var(--faint)}
"""

_REPORT_JS = """
let sortCol=1, sortAsc=false;
function doFilter(){
  const t=document.getElementById('search').value.toLowerCase();
  const om=document.getElementById('only-match').checked;
  let v=0;
  document.querySelectorAll('#tbody tr').forEach(tr=>{
    const name=tr.querySelector('.tcell').title.toLowerCase();
    const sc=parseFloat(tr.dataset.score);
    const show=(!t||name.includes(t))&&(!om||sc>0);
    tr.classList.toggle('hidden',!show);
    if(show)v++;
  });
  document.getElementById('visible').textContent=v;
}
function sortBy(col,type){
  if(sortCol===col)sortAsc=!sortAsc; else{sortCol=col;sortAsc=false;}
  document.querySelectorAll('thead th').forEach((th,i)=>th.classList.toggle('sorted',i===col));
  const tb=document.getElementById('tbody');
  const rows=Array.from(tb.querySelectorAll('tr'));
  rows.sort((a,b)=>{
    let av,bv;
    if(type==='num'){av=parseFloat(a.dataset.score)||0;bv=parseFloat(b.dataset.score)||0;}
    else{av=(a.querySelector('.tcell')?.title||'').toLowerCase();bv=(b.querySelector('.tcell')?.title||'').toLowerCase();}
    if(av<bv)return sortAsc?-1:1;
    if(av>bv)return sortAsc?1:-1;
    return 0;
  });
  rows.forEach(r=>tb.appendChild(r));
}
window.addEventListener('DOMContentLoaded',()=>{sortBy(1,'num');doFilter();});
"""

_REPORT_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>tablefp — отчёт поиска</title>
<style>{css}</style>
</head>
<body>
<div class="app">
  <header><h1>tablefp — отчёт поиска</h1><span class="sub">{generated} · {session_files}</span></header>
  <main>
    <div class="row-wrap">
      <span class="pill">Результаты <b>{total}</b></span>
      <span class="pill">Шаблон <b>{template_name}</b></span>
      <span class="pill">Просканировано <b>{total_scanned}</b></span>
    </div>
    <div class="controls">
      <input type="text" id="search" placeholder="Фильтр по имени таблицы…" oninput="doFilter()">
      <label><input type="checkbox" id="only-match" onchange="doFilter()"> Только совпадения (&gt;0)</label>
      <span class="pill" style="margin-left:auto">Показано <b id="visible">{total}</b> / {total}</span>
    </div>
    <div class="tw">
      <table>
        <thead><tr>
          <th onclick="sortBy(0,'text')">Целевая таблица</th>
          <th onclick="sortBy(1,'num')" class="sorted">Оценка</th>
          <th onclick="sortBy(2,'num')">Проверено</th>
          <th>Соответствие столбцов</th>
          <th>Все совпадения</th>
          <th>Без пары</th>
        </tr></thead>
        <tbody id="tbody">{rows}</tbody>
      </table>
    </div>
  </main>
</div>
<script>{js}</script>
</body>
</html>"""


def generate_report(json_paths: list[str], output_path: str) -> None:
    """Read one or more JSON result files and produce an HTML report."""
    sessions = []
    for path in json_paths:
        p = Path(path)
        if not p.exists():
            print(f"  skip missing: {p}")
            continue
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        data["_file"] = p.name
        sessions.append(data)

    if not sessions:
        print("No valid JSON files found.")
        return

    rows = []
    for s in sessions:
        for r in s.get("results", []):
            rows.append({
                "file": s["_file"],
                "template": s.get("template", ""),
                "table": r.get("table", ""),
                "score": r.get("score", 0),
                "verified": r.get("verified_row_ratio"),
                "mapping": r.get("mapping", []),
                "unmatched": r.get("unmatched_template_cols", []),
                "candidates": r.get("candidates", []),
            })

    html_str = _render(rows, sessions)
    Path(output_path).write_text(html_str, encoding="utf-8")
    print(f"Report: {output_path}  ({len(rows)} rows, {len(sessions)} sessions)")


def _render(rows: list[dict], sessions: list[dict]) -> str:
    total = len(rows)
    generated = "Сформировано: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    session_files = ", ".join(s.get("_file", "") for s in sessions)
    template_name = sessions[0].get("template", "—") if sessions else "—"
    total_scanned = sum(s.get("total_tables", 0) for s in sessions) or "—"

    rows_html = ""
    for r in rows:
        has_verified = r["verified"] is not None
        verified_pct = f"{(r['verified']*100):.0f}%" if has_verified else "—"
        score_val = r["score"]
        score_pct = round(score_val * 100)

        mapping_cells = ""
        for m in r["mapping"]:
            cpct = round(m.get("containment", 0) * 100)
            mapping_cells += (
                f'<div class="mrow">'
                f'<span class="mt">{_escape(m.get("template_name", ""))}</span>'
                f'<span class="arr">→</span>'
                f'<span class="md">{_escape(m.get("db_column", ""))}</span>'
                f'<span class="mp">{cpct}%</span>'
                f'</div>'
            )
        if not mapping_cells:
            mapping_cells = '<span class="null">—</span>'

        cand_cells = ""
        last_tmpl = None
        for c in r["candidates"]:
            tname = c.get("template_name", "")
            kind = c.get("kind", "exact")
            ex = c.get("exact_containment")
            ng = c.get("ngram_containment")
            ex_pct = f"{ex*100:.0f}%" if ex is not None else "—"
            ng_pct = f"{ng*100:.0f}%" if ng is not None else "—"
            head = f'<div class="ct">{_escape(tname)}</div>' if tname != last_tmpl else ""
            last_tmpl = tname
            cand_cells += (
                head
                + f'<div class="crow">'
                f'<span class="arr">→</span>'
                f'<span class="cd">{_escape(c.get("db_column", ""))}</span>'
                f'<span class="tag tag-{kind}">{_KIND_RU.get(kind, kind)}</span>'
                f'<span class="cp" title="точное вхождение">т:{ex_pct}</span>'
                f'<span class="cp" title="нечёткое вхождение">н:{ng_pct}</span>'
                f'</div>'
            )
        if not cand_cells:
            cand_cells = '<span class="null">—</span>'

        unmatched = ", ".join(_escape(x) for x in r.get("unmatched", [])) or '<span class="null">—</span>'
        vr = "ok" if (has_verified and r["verified"] >= 0.5) else "warn"

        rows_html += (
            f'<tr data-score="{score_val}" data-verified="{r["verified"] or -1}">'
            f'<td class="tcell" title="{_escape(r["table"])}">{_escape(r["table"])}</td>'
            f'<td><div class="sc"><div class="bar"><div class="fill" style="width:{score_pct}%"></div></div>'
            f'<span class="sn">{score_val:.4f}</span></div></td>'
            f'<td class="vc {vr}">{verified_pct}</td>'
            f'<td class="mc">{mapping_cells}</td>'
            f'<td class="cc">{cand_cells}</td>'
            f'<td class="uc">{unmatched}</td>'
            f'</tr>'
        )

    return _REPORT_TEMPLATE.format(
        total=total,
        total_scanned=total_scanned,
        generated=generated,
        session_files=_escape(session_files),
        template_name=_escape(template_name),
        css=_REPORT_CSS,
        js=_REPORT_JS,
        rows=rows_html,
    )


# --------------------------------------------------------------------------- #
# Comparison report (compare.html) — compressed blob + virtual scroll
# --------------------------------------------------------------------------- #

_COMPARE_CSS = """
:root{
  --bg:#f5f7fa; --surface:#fff; --text:#1a2233; --muted:#6b7280; --faint:#9aa3b2;
  --rule:#e6e9ef; --rule2:#d8dde6; --accent:#2563eb;
  --exact-bg:#dcfce7; --exact-fg:#166534; --exact-cell:#f0fdf4;
  --fuzzy-bg:#ffedd5; --fuzzy-fg:#9a3412; --fuzzy-cell:#fff7ed;
  --src:#0f5132; --tgt:#1e40af; --rowh:30px;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:var(--bg);color:var(--text);font:13px/1.5 system-ui,-apple-system,'Segoe UI',Arial,sans-serif}
.app{display:flex;flex-direction:column;height:100vh}
header{display:flex;align-items:baseline;gap:14px;padding:12px 20px;background:var(--surface);border-bottom:1px solid var(--rule)}
header h1{font-size:15px;font-weight:650}
header .sub{font-size:11px;color:var(--faint)}
main{flex:1;min-height:0;display:flex;flex-direction:column;padding:10px 20px 12px;gap:8px}
.row-wrap{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.pill{display:inline-flex;gap:6px;align-items:center;font-size:11px;color:var(--muted);background:var(--surface);border:1px solid var(--rule);border-radius:999px;padding:3px 10px}
.pill b{color:var(--text)}
.panels{display:flex;gap:8px}
details.panel{background:var(--surface);border:1px solid var(--rule);border-radius:8px;flex:1;min-width:0}
details.panel>summary{cursor:pointer;padding:8px 12px;font-size:12px;font-weight:600;list-style:none;display:flex;align-items:center;gap:6px}
details.panel>summary::-webkit-details-marker{display:none}
details.panel>summary::before{content:'▸';color:var(--faint);transition:transform .15s}
details.panel[open]>summary::before{transform:rotate(90deg)}
details.panel .pbody{padding:8px 12px 10px;border-top:1px solid var(--rule);font-size:11px}
.legend{display:flex;gap:10px;flex-wrap:wrap}
.lg{display:flex;align-items:center;gap:4px}
.lg .src{color:var(--src);font-weight:600}
.lg .tgt{color:var(--tgt);font-weight:600}
.lg .arr{color:var(--faint)}
.lg .pct{color:var(--muted);background:var(--bg);border-radius:4px;padding:0 4px;font-size:10px}
.tag{font-size:9px;font-weight:700;text-transform:uppercase;border-radius:3px;padding:0 4px}
.tag-exact{background:var(--exact-bg);color:var(--exact-fg)}
.tag-fuzzy{background:var(--fuzzy-bg);color:var(--fuzzy-fg)}
.star{color:#d97706}
.controls{display:flex;gap:10px;align-items:center;background:var(--surface);border:1px solid var(--rule);border-radius:8px;padding:7px 10px}
.controls input[type=text]{border:1px solid var(--rule2);border-radius:6px;padding:5px 9px;font-size:12px;width:240px;outline:none}
.controls input[type=text]:focus{border-color:var(--accent)}
.controls label{font-size:11px;color:var(--muted);display:inline-flex;gap:4px;align-items:center;cursor:pointer}
.vp{flex:1;min-height:0;overflow:auto;background:var(--surface);border:1px solid var(--rule);border-radius:8px;position:relative}
.headrow,.r{display:grid;grid-template-columns:var(--cols)}
.headrow{position:sticky;top:0;z-index:6}
.headrow .h{position:relative;font-size:10.5px;font-weight:650;text-transform:uppercase;letter-spacing:.03em;color:var(--muted);padding:7px 10px;border-bottom:1px solid var(--rule2);background:var(--surface)}
.headrow .h .hl{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rz{position:absolute;top:0;right:0;bottom:0;width:6px;cursor:col-resize;z-index:9}
.rz:hover,.rz:active{background:var(--accent);opacity:.35}
.headrow .h.rn{background:#eef2f7}
.headrow .h.src{color:var(--src)}
.headrow .h.tgt{color:var(--tgt)}
.headrow .h.mt{color:var(--text);font-weight:700}
.headrow .h.st{position:sticky;z-index:7}
.spacer{position:relative}
.canvas{position:absolute;top:0;left:0}
.r{border-bottom:1px solid var(--rule)}
.r.um{opacity:.4}
.c{padding:5px 10px;font-size:12px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;background:var(--surface)}
.c.rn{color:var(--faint);font-size:11px;background:#eef2f7}
.c.div{background:var(--rule2);padding:0}
.c.src{border-left:1px solid var(--rule)}
.c.tgt{border-left:1px solid var(--rule)}
.c.mt{font-weight:600}
.c.st{position:sticky;z-index:2}
.c.rn.st{z-index:4}
.c.ck-exact{background:var(--exact-cell)}
.c.ck-fuzzy{background:var(--fuzzy-cell)}
.null{color:var(--faint)}
mark.exact{background:var(--exact-bg);color:var(--exact-fg);border-radius:2px;padding:0 1px}
mark.fuzzy{background:var(--fuzzy-bg);color:var(--fuzzy-fg);border-radius:2px;padding:0 1px}
.loading{padding:30px;text-align:center;color:var(--faint)}
.foot{font-size:11px;color:var(--faint);padding:0 2px;display:flex;justify-content:space-between}
"""

_COMPARE_JS = """
const B64=__B64__;
const KIND_RU={exact:'точное',fuzzy:'нечёткое'};
const MIN_COL=50, LS_PREFIX='tablefp:colw:';
let P=null, ROWS=[], view=[];
let baseW=[], curEff=[], userW={}, lastCw=0;
const ROW_H=30, OVER=14;
const $=id=>document.getElementById(id);
const isFixed=c=>c.k==='rownum'||c.k==='divider';
function sig(){return (P.meta&&P.meta.table||'?')+'|'+P.cols.filter(c=>!isFixed(c)).map(c=>c.l).join(',');}
function loadSizes(){try{const s=localStorage.getItem(LS_PREFIX+sig());if(s)userW=JSON.parse(s)||{};}catch(e){userW={};}}
function saveSizes(){try{localStorage.setItem(LS_PREFIX+sig(),JSON.stringify(userW));}catch(e){}}

function applyCols(){
  const vp=$('vp');
  const cw=vp.clientWidth;
  baseW=P.cols.map(c=>c.w);
  let eff=baseW.slice();
  let used=0, fi=[], fb=[];
  for(let i=0;i<P.cols.length;i++){
    const c=P.cols[i];
    if(isFixed(c)){used+=baseW[i];continue;}
    const u=userW[c.l];
    if(u!=null){eff[i]=u;used+=u;}
    else{fi.push(i);fb.push(baseW[i]);}
  }
  const fs=fb.reduce((a,b)=>a+b,0);
  const avail=cw-used;
  if(fi.length&&fs>0&&avail>fs){
    const sc=avail/fs;
    for(let k=0;k<fi.length;k++)eff[fi[k]]=Math.round(fb[k]*sc);
  }
  curEff=eff;
  document.documentElement.style.setProperty('--cols',eff.map(w=>w+'px').join(' '));
}

let dragI=-1, dragX=0, dragStart=0;
function onRzDown(e,i){
  e.preventDefault();e.stopPropagation();
  dragI=i;dragX=e.clientX;dragStart=curEff[i]||P.cols[i].w;
  document.body.style.cursor='col-resize';
  document.body.style.userSelect='none';
  window.addEventListener('mousemove',onRzMove);
  window.addEventListener('mouseup',onRzUp);
}
function onRzMove(e){
  if(dragI<0)return;
  const nw=Math.max(MIN_COL,Math.round(dragStart+(e.clientX-dragX)));
  userW[P.cols[dragI].l]=nw;saveSizes();applyCols();
}
function onRzUp(){
  dragI=-1;
  document.body.style.cursor='';
  document.body.style.userSelect='';
  window.removeEventListener('mousemove',onRzMove);
  window.removeEventListener('mouseup',onRzUp);
}

function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function hl(text,spans,cls){
  if(text==null) return '<span class="null">—</span>';
  if(!spans||!spans.length) return esc(text);
  let cl=spans.map(s=>[Math.max(0,s[0]),Math.min(text.length,s[1])]).filter(s=>s[1]>s[0]).sort((a,b)=>a[0]-b[0]);
  let out='',pos=0;
  for(const[s,e]of cl){let ss=Math.max(s,pos); if(ss>=e)continue; if(ss>pos)out+=esc(text.slice(pos,ss)); out+='<mark class="'+cls+'">'+esc(text.slice(ss,e))+'</mark>'; pos=e;}
  if(pos<text.length)out+=esc(text.slice(pos));
  return out;
}

async function init(){
  const bin=Uint8Array.from(atob(B64),c=>c.charCodeAt(0));
  const ds=new Response(bin).body.pipeThrough(new DecompressionStream('gzip'));
  const txt=await new Response(ds).text();
  P=JSON.parse(txt);
  ROWS=P.rows;
  buildHeader(P.meta||{});
  buildLegend(P.legend||[]);
  buildCols(P.cols||[]);
  $('total').textContent=ROWS.length;
  if(P.meta&&P.meta.only_matched) $('only-matched').checked=true;
  const vp=$('vp');
  vp.addEventListener('scroll',render,{passive:true});
  window.addEventListener('resize',render);
  const ro=new ResizeObserver(()=>{const cw=$('vp').clientWidth;if(cw!==lastCw){lastCw=cw;applyCols();render();}});
  ro.observe(vp);
  const ld=$('loading'); if(ld) ld.remove();
  applyFilter();
}

function buildHeader(m){
  if(m.table) $('m-table').textContent=m.table;
  $('m-score').textContent=(m.score!=null?m.score.toFixed(4):'—');
  $('m-verified').textContent=(m.verified==null?'—':Math.round(m.verified*100)+'%');
  if(m.anchor!=null) $('m-anchor').textContent=m.anchor;
  $('m-rows').textContent=(m.n_matched!=null?m.n_matched:0)+' / '+(m.n_rows!=null?m.n_rows:0);
  if(m.mode) $('m-mode').textContent=m.mode;
  if(m.generated) $('m-gen').textContent='Сформировано: '+m.generated;
  if(m.table) document.title='tablefp — '+m.table;
}

function buildLegend(legend){
  if(!legend.length){const p=$('legend-panel'); if(p) p.style.display='none'; return;}
  let h='';
  for(const p of legend){
    const tc=p.kind==='exact'?'tag-exact':'tag-fuzzy';
    const star=p.is_anchor?' <span class="star">★</span>':'';
    // When both match types exist (fuzzy enabled, text column), show both
    // exact and fuzzy containment, like the search report: т:X% н:Y%
    let pct;
    if(p.exact!=null&&p.ngram!=null){
      pct='<span class="pct" title="точное вхождение">т:'+Math.round(p.exact*100)+'%</span>'
        +'<span class="pct" title="нечёткое вхождение">н:'+Math.round(p.ngram*100)+'%</span>';
    }else{
      pct='<span class="pct">'+Math.round(p.containment*100)+'%</span>';
    }
    h+='<div class="lg"><span class="src">'+esc(p.source_col)+star+'</span>'
      +'<span class="arr">↔</span>'
      +'<span class="tgt">'+esc(p.target_col)+'</span>'
      +'<span class="tag '+tc+'">'+(KIND_RU[p.kind]||p.kind)+'</span>'
      +pct+'</div>';
  }
  $('legend').innerHTML=h;
}

function buildCols(cols){
  loadSizes();
  let h='';
  for(let i=0;i<cols.length;i++){
    const c=cols[i];
    const cls='h '+c.k+(c.mt?' mt':'')+(c.s?' st':'');
    const st=c.s?(' style="left:'+(c.so||0)+'px"'):'';
    const t=c.k==='rownum'?'#':esc(c.l||'');
    const rz=isFixed(c)?'':'<div class="rz" data-i="'+i+'"></div>';
    h+='<div class="'+cls+'"'+st+'><span class="hl">'+t+'</span>'+rz+'</div>';
  }
  $('headrow').innerHTML=h;
  document.querySelectorAll('.rz').forEach(el=>{
    el.addEventListener('mousedown',e=>onRzDown(e,parseInt(el.dataset.i)));
  });
  lastCw=$('vp').clientWidth;
  applyCols();
}

function rowText(r){
  if(r._t!=null) return r._t;
  let s='';
  for(const c of r.c){ if(c&&c.v!=null) s+=' '+c.v; }
  r._t=s.toLowerCase();
  return r._t;
}

function applyFilter(){
  const q=$('search').value.trim().toLowerCase();
  const om=$('only-matched').checked;
  const ou=$('only-unmatched').checked;
  view=[];
  for(let i=0;i<ROWS.length;i++){
    const r=ROWS[i];
    if(om&&!r.m) continue;
    if(ou&&r.m) continue;
    if(q&&!rowText(r).includes(q)) continue;
    view.push(i);
  }
  $('spacer').style.height=(view.length*ROW_H)+'px';
  render();
}

function render(){
  const vp=$('vp');
  const top=vp.scrollTop;
  const first=Math.max(0,Math.floor(top/ROW_H)-OVER);
  const last=Math.min(view.length,Math.ceil((top+vp.clientHeight)/ROW_H)+OVER);
  let html='';
  for(let i=first;i<last;i++){
    const ri=view[i];
    html+=renderRow(ri, ROWS[ri]);
  }
  const c=$('canvas');
  c.style.top=(first*ROW_H)+'px';
  c.innerHTML=html;
  $('visible').textContent=view.length;
}

function renderRow(i,row){
  let cells='';
  for(const col of P.cols){
    if(col.k==='rownum'){
      cells+='<div class="c rn'+(col.s?' st':'')+'" style="left:0">['+(i+1)+']</div>';
      continue;
    }
    if(col.k==='divider'){ cells+='<div class="c div"></div>'; continue; }
    const c=col.ci>=0?row.c[col.ci]:null;
    let inner;
    if(!c||c.v==null) inner='<span class="null">—</span>';
    else if(c.k==='exact'||c.k==='fuzzy') inner=hl(c.v,c.s||[],c.k);
    else inner=esc(c.v);
    const cls='c '+col.k+(col.mt?' mt':'')+((c&&c.k)?(' ck-'+c.k):'')+(col.s?' st':'');
    const st=col.s?(' style="left:'+(col.so||0)+'px"'):'';
    cells+='<div class="'+cls+'"'+st+'>'+inner+'</div>';
  }
  return '<div class="r'+(row.m?'':' um')+'">'+cells+'</div>';
}

window.addEventListener('DOMContentLoaded',init);
"""

_COMPARE_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<div class="app">
  <header>
    <h1>tablefp — сравнение</h1>
    <span class="sub" id="m-gen"></span>
  </header>
  <main>
    <div class="row-wrap">
      <span class="pill">Таблица <b id="m-table">—</b></span>
      <span class="pill">Оценка <b id="m-score">—</b></span>
      <span class="pill">Проверено <b id="m-verified">—</b></span>
      <span class="pill">Якорь <b id="m-anchor">—</b></span>
      <span class="pill">Совпало <b id="m-rows">—</b></span>
      <span class="pill">Столбцы <b id="m-mode">—</b></span>
    </div>
    <div class="panels">
      <details class="panel" open id="legend-panel">
        <summary>Совпавшие столбцы</summary>
        <div class="pbody legend" id="legend"></div>
      </details>
    </div>
    <div class="controls">
      <input type="text" id="search" placeholder="Фильтр строк по любому значению…" oninput="applyFilter()">
      <label><input type="checkbox" id="only-matched" onchange="applyFilter()"> Только совпавшие</label>
      <label><input type="checkbox" id="only-unmatched" onchange="applyFilter()"> Только несовпавшие</label>
      <span class="pill" style="margin-left:auto">Показано <b id="visible">0</b> / <b id="total">0</b></span>
    </div>
    <div class="vp" id="vp">
      <div class="headrow" id="headrow"></div>
      <div class="spacer" id="spacer"><div class="canvas" id="canvas"><div class="loading" id="loading">Распаковка…</div></div></div>
    </div>
    <div class="foot"><span>виртуальная прокрутка · gzip</span><span></span></div>
  </main>
</div>
<script>{js}</script>
</body>
</html>"""


def _build_compare_payload(data: dict) -> dict:
    rows = data.get("rows", [])
    source_columns = data.get("source_columns", [])
    target_columns = data.get("target_columns", [])
    pairs = data.get("matched_pairs", [])
    src_matched = {p["source_col"] for p in pairs}
    tgt_matched = {p["target_col"] for p in pairs}

    cols = []
    ci = 0
    cols.append({"k": "rownum", "l": "#", "w": 52, "s": True, "so": 0, "ci": -1, "mt": False})
    for j, name in enumerate(source_columns):
        sticky = (j == 0)
        cols.append({
            "k": "src", "l": name, "w": 180, "ci": ci,
            "s": sticky, "so": (52 if sticky else 0),
            "mt": name in src_matched,
        })
        ci += 1
    cols.append({"k": "divider", "l": "", "w": 4, "s": False, "so": 0, "ci": -1, "mt": False})
    for name in target_columns:
        cols.append({
            "k": "tgt", "l": name, "w": 200, "ci": ci,
            "s": False, "so": 0,
            "mt": name in tgt_matched,
        })
        ci += 1

    def cell_payload(cell):
        v = cell.get("value")
        k = cell.get("kind", "none")
        s = cell.get("spans") or []
        out = {"v": v}
        if k and k != "none":
            out["k"] = k
        if s:
            out["s"] = [[int(a), int(b)] for a, b in s]
        return out

    rows_payload = []
    for r in rows:
        cells = [cell_payload(c) for c in r.get("source", [])]
        cells += [cell_payload(c) for c in r.get("target", [])]
        rows_payload.append({"m": bool(r.get("matched")), "c": cells})

    legend = [
        {
            "source_col": p["source_col"], "target_col": p["target_col"],
            "kind": p["kind"], "containment": p["containment"],
            "is_anchor": bool(p.get("is_anchor")),
            "exact": p.get("exact_containment"),
            "ngram": p.get("ngram_containment"),
        }
        for p in pairs
    ]

    verified = data.get("verified_row_ratio")
    meta = {
        "table": data.get("table", "—"),
        "score": float(data.get("score", 0.0)),
        "verified": (float(verified) if verified is not None else None),
        "anchor": data.get("anchor", "—"),
        "n_matched": sum(1 for r in rows if r.get("matched")),
        "n_rows": len(rows),
        "mode": data.get("columns_mode", "all"),
        "only_matched": bool(data.get("only_matched", False)),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return {"meta": meta, "legend": legend, "cols": cols, "rows": rows_payload, "cfg": {"rowH": 30}}


def generate_comparison_report(data: dict, output_path: str) -> None:
    """Render a side-by-side row comparison for one table to self-contained HTML."""
    html_str = _render_comparison(data)
    Path(output_path).write_text(html_str, encoding="utf-8")
    n = len(data.get("rows", []))
    matched = sum(1 for r in data.get("rows", []) if r.get("matched"))
    print(f"Comparison: {output_path}  ({matched}/{n} rows matched)")


def _render_comparison(data: dict) -> str:
    payload = _build_compare_payload(data)
    blob = _pack(payload)
    js = _COMPARE_JS.replace("__B64__", json.dumps(blob))
    title = _escape(data.get("table", "сравнение"))
    return _COMPARE_TEMPLATE.format(title=title, css=_COMPARE_CSS, js=js)


_COMPARE_INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>tablefp — отчёты сравнения</title>
<style>{css}</style>
</head>
<body>
<div class="app">
  <header><h1>tablefp — отчёты сравнения</h1><span class="sub">{generated} · топ {n}</span></header>
  <main>
    <div class="row-wrap">
      <span class="pill">Шаблон <b>{template_name}</b></span>
      <span class="pill">Отчётов <b>{n}</b></span>
    </div>
    <div class="tw">
      <table>
        <thead><tr>
          <th>#</th>
          <th>Таблица</th>
          <th>Оценка</th>
          <th>Проверено</th>
          <th>Совпало строк</th>
          <th>Отчёт</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </main>
</div>
</body>
</html>"""


def generate_compare_index(entries: list[dict], output_path: str,
                           template_name: str = "—") -> None:
    """Render an index.html linking per-table compare reports (sorted by score desc)."""
    generated = "Сформировано: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows_html = ""
    for i, e in enumerate(entries, 1):
        score = e.get("score", 0.0)
        score_pct = round(score * 100)
        verified = e.get("verified")
        if verified is None:
            verified_pct = "—"
        else:
            verified_pct = f"{verified*100:.0f}%"
        vr = "ok" if (verified is not None and verified >= 0.5) else "warn"
        n_matched = e.get("n_matched", 0)
        n_rows = e.get("n_rows", 0)
        rows_html += (
            f'<tr>'
            f'<td class="sn">{i}</td>'
            f'<td class="tcell"><a href="{_escape(e.get("file", ""))}">{_escape(e.get("table", ""))}</a></td>'
            f'<td><div class="sc"><div class="bar"><div class="fill" style="width:{score_pct}%"></div></div>'
            f'<span class="sn">{score:.4f}</span></div></td>'
            f'<td class="vc {vr}">{verified_pct}</td>'
            f'<td>{n_matched} / {n_rows}</td>'
            f'<td><a href="{_escape(e.get("file", ""))}">открыть</a></td>'
            f'</tr>'
        )

    html_str = _COMPARE_INDEX_TEMPLATE.format(
        css=_REPORT_CSS,
        generated=generated,
        n=len(entries),
        template_name=_escape(template_name),
        rows=rows_html,
    )
    Path(output_path).write_text(html_str, encoding="utf-8")
    print(f"Index: {output_path}  ({len(entries)} reports)")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Generate HTML report from search results JSON")
    parser.add_argument("files", nargs="+", help="JSON result files")
    parser.add_argument("-o", "--output", default="report.html", help="Output HTML file")
    args = parser.parse_args()

    import glob
    expanded = []
    for f in args.files:
        matches = glob.glob(f)
        expanded.extend(matches if matches else [f])

    generate_report(expanded, args.output)


if __name__ == "__main__":
    main()
