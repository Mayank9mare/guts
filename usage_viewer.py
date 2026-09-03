#!/usr/bin/env python3
"""Local, read-only observability dashboard for Guts — cost, tokens, tool/skill usage, and
per-run trace drill-down (the full span tree of any single run: every tool call, in order,
with its result). Reads usage_tracker.py's local JSONL files directly.

Stdlib only, no external fonts/CDN/JS libraries — fully offline. Independent of the running
bot — start/stop whenever:

    python3 usage_viewer.py [--port 8767]

Never calls Slack, never writes anything, never leaves the machine.
"""
import argparse
import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import usage_tracker as ut


def _days_param(qs) -> float | None:
    raw = (qs.get("days") or ["7"])[0]
    if raw == "all":
        return None
    try:
        return float(raw)
    except ValueError:
        return 7.0


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Guts — Usage</title>
<style>
:root{
  --ink:#0a0d10;--panel:#12171b;--panel-2:#0d1215;--line:#212b31;
  --fog:#7f8fa0;--text:#dbe4ea;--teal:#2fd4c4;--signal:#54b8f0;
  --alert:#f2a63c;--red:#ef6a5a;--ok:#54cc8e;--dim:#4a5863;
  --mono:ui-monospace,'SF Mono','Cascadia Code',Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ink);color:var(--text);font-family:var(--sans);
  font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased;
  background-image:radial-gradient(circle at 8% -10%, rgba(47,212,196,.06), transparent 40%),
                    radial-gradient(circle at 96% 0%, rgba(84,184,240,.05), transparent 44%);}
.wrap{max-width:1180px;margin:0 auto;padding:0 22px 60px}

header{border-bottom:1px solid var(--line);padding:26px 0 20px;position:sticky;top:0;
  background:rgba(10,13,16,.92);backdrop-filter:blur(6px);z-index:10}
header .row{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
h1{font-size:19px;font-weight:700;margin:0;color:#fff;letter-spacing:-.01em;display:flex;
  align-items:center;gap:9px}
.live{width:7px;height:7px;border-radius:50%;background:var(--ok);animation:blink 1.8s ease-in-out infinite;flex:none}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
header .sub{color:var(--fog);font-size:12.5px;margin-top:3px}
.spacer{flex:1}
.range{display:flex;gap:4px;background:var(--panel-2);border:1px solid var(--line);border-radius:8px;padding:3px}
.range button{background:none;border:0;color:var(--fog);font-family:var(--mono);font-size:11.5px;
  letter-spacing:.04em;padding:6px 12px;border-radius:6px;cursor:pointer;font-weight:500}
.range button:hover{color:var(--text)}
.range button.on{background:var(--teal);color:var(--ink);font-weight:700}

main{padding-top:26px}
.empty{text-align:center;padding:90px 20px;color:var(--fog)}
.empty .big{font-size:34px;margin-bottom:10px}
.empty code{font-family:var(--mono);background:var(--panel);border:1px solid var(--line);
  padding:2px 7px;border-radius:5px;color:var(--signal)}

.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:22px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:16px 17px}
.kpi .lab{font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--fog);margin-bottom:9px}
.kpi .val{font-size:24px;font-weight:700;color:#fff;letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.kpi .val.teal{color:var(--teal)}.kpi .val.red{color:var(--red)}
.kpi .sub{font-size:11.5px;color:var(--dim);margin-top:4px}
@media(max-width:920px){.kpis{grid-template-columns:repeat(2,1fr)}}

.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin-bottom:16px}
.panel h2{font-size:13px;font-weight:600;color:#fff;margin:0 0 16px;display:flex;align-items:center;
  justify-content:space-between}
.panel h2 .n{font-family:var(--mono);font-size:11px;color:var(--fog);font-weight:400}

.chart{display:flex;align-items:flex-end;gap:5px;height:120px;padding-top:6px}
.bar{flex:1;background:linear-gradient(180deg,rgba(47,212,196,.9),rgba(47,212,196,.35));
  border-radius:3px 3px 0 0;min-height:2px;position:relative;cursor:default;transition:opacity .12s}
.bar:hover{opacity:.75}
.bar .tip{position:absolute;bottom:100%;left:50%;transform:translateX(-50%);margin-bottom:6px;
  background:#000;border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-family:var(--mono);
  font-size:10.5px;white-space:nowrap;opacity:0;pointer-events:none;transition:opacity .12s;z-index:5}
.bar:hover .tip{opacity:1}
.chart-axis{display:flex;justify-content:space-between;margin-top:6px;font-family:var(--mono);
  font-size:10px;color:var(--dim)}

.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:820px){.cols{grid-template-columns:1fr}}
.blist{display:flex;flex-direction:column;gap:9px}
.brow{display:grid;grid-template-columns:120px 1fr auto;gap:10px;align-items:center;font-size:12.5px}
.brow .nm{font-family:var(--mono);color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.brow .track{height:7px;background:var(--panel-2);border-radius:4px;overflow:hidden;border:1px solid var(--line)}
.brow .fill{height:100%;background:linear-gradient(90deg,var(--signal),var(--teal));border-radius:4px}
.brow .cost{font-family:var(--mono);color:var(--fog);font-size:11.5px;text-align:right;min-width:58px}

.chips{display:flex;flex-wrap:wrap;gap:7px}
.chip{background:var(--panel-2);border:1px solid var(--line);border-radius:20px;padding:6px 12px;
  font-family:var(--mono);font-size:11.5px;color:var(--text);display:flex;gap:7px;align-items:center}
.chip b{color:var(--teal);font-weight:700}
.chip.skill b{color:var(--signal)}

table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);font-family:var(--mono);
  font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--fog);font-weight:500}
td{padding:10px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
tr.run{cursor:pointer}
tr.run:hover td{background:rgba(255,255,255,.02)}
td.mono{font-family:var(--mono);color:var(--fog);font-size:11.5px;white-space:nowrap}
td.cost{font-family:var(--mono);color:var(--teal);text-align:right;white-space:nowrap;font-weight:600}
.cmd-badge{font-family:var(--mono);font-size:11px;background:var(--panel-2);border:1px solid var(--line);
  padding:2px 8px;border-radius:5px;color:var(--signal)}
.dot-status{width:7px;height:7px;border-radius:50%;display:inline-block}
.dot-status.ok{background:var(--ok)}.dot-status.err{background:var(--red)}
.skill-tag{font-family:var(--mono);font-size:10.5px;color:var(--signal);background:rgba(84,184,240,.1);
  padding:1px 6px;border-radius:4px;margin-right:3px}

.trace-row td{background:var(--panel-2);padding:14px 18px}
.trace{display:flex;flex-direction:column;gap:7px}
.span{display:flex;align-items:center;gap:10px;font-family:var(--mono);font-size:11.5px;padding:7px 10px;
  background:var(--panel);border:1px solid var(--line);border-radius:7px}
.span .badge{padding:1px 7px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:.03em;flex:none}
.span .badge.tool_call{background:rgba(84,184,240,.15);color:var(--signal)}
.span .badge.tool_error{background:rgba(239,106,90,.15);color:var(--red)}
.span .badge.error{background:rgba(239,106,90,.15);color:var(--red)}
.span .badge.result{background:rgba(84,204,142,.15);color:var(--ok)}
.span .name{color:var(--text);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.span .meta{color:var(--dim);flex:none}
.trace-loading,.trace-empty{color:var(--fog);font-family:var(--mono);font-size:11.5px;padding:6px 2px}

footer{text-align:center;color:var(--dim);font-family:var(--mono);font-size:11px;padding:30px 0 10px}
</style></head><body>
<header><div class="wrap">
  <div class="row">
    <h1><span class="live"></span> Guts — Usage &amp; Observability</h1>
    <div class="spacer"></div>
    <div class="range" id="range">
      <button data-days="1">Today</button>
      <button data-days="7" class="on">7d</button>
      <button data-days="30">30d</button>
      <button data-days="all">All</button>
    </div>
  </div>
  <div class="sub">Local only — cost, tokens, tools &amp; skills invoked, per-run trace drill-down. Nothing here ever leaves this machine.</div>
</div></header>

<main class="wrap">
  <div id="content"><div class="empty">Loading…</div></div>
</main>
<footer>usage_viewer.py · read-only · <span id="refreshed"></span></footer>

<script>
let currentDays = '7';

function esc(s){return (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function money(n){return '$'+(n||0).toFixed( (n||0) < 1 ? 4 : 2 );}
function ms(n){ if(!n) return '—'; return n>=1000 ? (n/1000).toFixed(1)+'s' : Math.round(n)+'ms'; }
function tokens(n){ if(!n) return '0'; return n>=1000 ? (n/1000).toFixed(1)+'k' : String(n); }
function timeAgo(ts){
  const s = Math.max(0, (Date.now()/1000) - ts);
  if(s<60) return Math.round(s)+'s ago';
  if(s<3600) return Math.round(s/60)+'m ago';
  if(s<86400) return Math.round(s/3600)+'h ago';
  return Math.round(s/86400)+'d ago';
}

async function load(){
  const [summary, runs] = await Promise.all([
    fetch('/api/summary?days='+currentDays).then(r=>r.json()),
    fetch('/api/runs?days='+currentDays+'&limit=200').then(r=>r.json()),
  ]);
  render(summary, runs);
  document.getElementById('refreshed').textContent = 'updated ' + new Date().toLocaleTimeString();
}

function render(s, runs){
  const el = document.getElementById('content');
  if(!s.total_runs){
    el.innerHTML = '<div class="empty"><div class="big">◈</div>No usage recorded yet for this window.<br><br>'+
      'Send Guts a prompt in Slack, or run <code>!usage</code> there.</div>';
    return;
  }

  const kpis = `
    <div class="kpis">
      <div class="kpi"><div class="lab">Total Spend</div><div class="val teal">${money(s.total_cost_usd)}</div><div class="sub">${s.total_runs} run(s)</div></div>
      <div class="kpi"><div class="lab">Runs</div><div class="val">${s.total_runs}</div><div class="sub">${(s.total_cost_usd/Math.max(1,s.total_runs)).toFixed(3)} avg/run</div></div>
      <div class="kpi"><div class="lab">Tool Calls</div><div class="val">${s.total_tool_calls}</div><div class="sub">${(s.total_tool_calls/Math.max(1,s.total_runs)).toFixed(1)} avg/run</div></div>
      <div class="kpi"><div class="lab">Avg Duration</div><div class="val">${ms(s.avg_duration_ms)}</div><div class="sub">per run</div></div>
      <div class="kpi"><div class="lab">Errors</div><div class="val ${s.error_count?'red':''}">${s.error_count}</div><div class="sub">${s.total_runs? (100*s.error_count/s.total_runs).toFixed(0):0}% error rate</div></div>
    </div>`;

  const maxDay = Math.max(...s.daily.map(d=>d[1]), 0.0001);
  const chart = `
    <div class="panel">
      <h2>Daily spend <span class="n">last 14 days</span></h2>
      <div class="chart">
        ${s.daily.map(([day,cost])=>`<div class="bar" style="height:${Math.max(2,100*cost/maxDay)}%"><div class="tip">${day}<br>${money(cost)}</div></div>`).join('')}
      </div>
      <div class="chart-axis"><span>${s.daily[0]?s.daily[0][0]:''}</span><span>${s.daily.length?s.daily[s.daily.length-1][0]:''}</span></div>
    </div>`;

  const barList = (rows, max) => rows.slice(0,8).map(([label,cost])=>`
      <div class="brow">
        <div class="nm">${esc(label)}</div>
        <div class="track"><div class="fill" style="width:${Math.max(2,100*cost/max)}%"></div></div>
        <div class="cost">${money(cost)}</div>
      </div>`).join('');
  const maxUser = Math.max(...s.by_user.map(u=>u[1]), 0.0001);
  const maxCmd = Math.max(...s.by_command.map(u=>u[1]), 0.0001);

  const cols1 = `
    <div class="cols">
      <div class="panel"><h2>Spend by command</h2><div class="blist">${barList(s.by_command, maxCmd) || '<span class="trace-empty">no data</span>'}</div></div>
      <div class="panel"><h2>Spend by user</h2><div class="blist">${barList(s.by_user, maxUser) || '<span class="trace-empty">no data</span>'}</div></div>
    </div>`;

  const chips = (items, cls) => items.length ? items.map(([name,n])=>`<div class="chip ${cls}">${esc(name)} <b>${n}</b></div>`).join('') : '<span class="trace-empty">no data</span>';
  const cols2 = `
    <div class="cols">
      <div class="panel"><h2>Top tools</h2><div class="chips">${chips(s.top_tools,'')}</div></div>
      <div class="panel"><h2>Top skills invoked</h2><div class="chips">${chips(s.top_skills,'skill')}</div></div>
    </div>`;

  const rows = runs.map((r,i)=>{
    const skills = (r.skills_used||[]).map(sk=>`<span class="skill-tag">/${esc(sk)}</span>`).join('');
    return `
    <tr class="run" data-idx="${i}">
      <td class="mono">${timeAgo(r.ts)}</td>
      <td><span class="cmd-badge">${esc(r.command||'?')}</span></td>
      <td class="mono">${r.user_id ? esc(r.user_id) : '—'}</td>
      <td class="mono">${esc((r.model||'').replace('[1m]',''))}</td>
      <td class="cost">${money(r.cost_usd)}</td>
      <td class="mono">${ms(r.duration_ms)}</td>
      <td class="mono">${r.tool_call_count||0}</td>
      <td>${skills || '<span class="mono" style="color:var(--dim)">—</span>'}</td>
      <td><span class="dot-status ${r.is_error?'err':'ok'}"></span></td>
    </tr>
    <tr class="trace-row" id="trace-${i}" style="display:none"><td colspan="9"></td></tr>`;
  }).join('');

  const table = `
    <div class="panel">
      <h2>Recent runs <span class="n">${runs.length} shown · click a row to see its trace</span></h2>
      <div style="overflow-x:auto">
      <table>
        <thead><tr><th>When</th><th>Command</th><th>User</th><th>Model</th><th style="text-align:right">Cost</th>
          <th>Duration</th><th>Tools</th><th>Skills</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      </div>
    </div>`;

  el.innerHTML = kpis + chart + cols1 + cols2 + table;

  el.querySelectorAll('tr.run').forEach(tr=>{
    tr.addEventListener('click', () => toggleTrace(tr, runs[+tr.dataset.idx]));
  });
}

async function toggleTrace(tr, run){
  const box = document.getElementById('trace-'+tr.dataset.idx);
  const showing = box.style.display !== 'none';
  document.querySelectorAll('.trace-row').forEach(r=>r.style.display='none');
  if(showing) return;
  box.style.display = '';
  box.querySelector('td').innerHTML = '<div class="trace-loading">loading trace…</div>';
  try{
    const spans = await fetch('/api/trace?trace_id='+encodeURIComponent(run.trace_id)+'&ts='+run.ts).then(r=>r.json());
    if(!spans.length){
      box.querySelector('td').innerHTML = '<div class="trace-empty">no span data for this run (older than trace retention, or from before tracing was added)</div>';
      return;
    }
    box.querySelector('td').innerHTML = '<div class="trace">' + spans.map(sp=>{
      let label = sp.name || sp.span_type;
      if(sp.span_type==='tool_call'){
        if(sp.skill) label = 'Skill /' + sp.skill + (sp.args?(' '+sp.args):'');
        else if(sp.command_line) label = 'Bash ' + sp.command_line;
        else if(sp.file_path) label = sp.name + ' ' + sp.file_path;
      } else if(sp.span_type==='result'){
        label = 'result — ' + money(sp.cost_usd) + ', ' + ms(sp.duration_ms);
      } else if(sp.span_type==='tool_error' || sp.span_type==='error'){
        label = (sp.text||'').slice(0,140);
      }
      return `<div class="span"><span class="badge ${sp.span_type}">${sp.span_type}</span><span class="name">${esc(label)}</span></div>`;
    }).join('') + '</div>';
  }catch(e){
    box.querySelector('td').innerHTML = '<div class="trace-empty">failed to load trace: '+esc(e)+'</div>';
  }
}

document.getElementById('range').addEventListener('click', (e)=>{
  const btn = e.target.closest('button');
  if(!btn) return;
  document.querySelectorAll('#range button').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
  currentDays = btn.dataset.days;
  document.getElementById('content').innerHTML = '<div class="empty">Loading…</div>';
  load();
});

load();
setInterval(load, 30000);
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif parsed.path == "/api/summary":
                days = _days_param(qs)
                rows = ut.load_rows(since_days=days)
                s = ut.summarize(rows)
                s["daily"] = ut.daily_costs(ut.load_rows(since_days=30), days=14)
                self._send(200, json.dumps(s))
            elif parsed.path == "/api/runs":
                days = _days_param(qs)
                limit = int((qs.get("limit") or ["200"])[0])
                rows = ut.load_rows(since_days=days)
                rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
                self._send(200, json.dumps(rows[:limit]))
            elif parsed.path == "/api/trace":
                trace_id = (qs.get("trace_id") or [""])[0]
                ts_raw = (qs.get("ts") or [""])[0]
                if not trace_id or not ts_raw:
                    self._send(400, json.dumps({"error": "trace_id and ts required"}))
                    return
                self._send(200, json.dumps(ut.load_spans(trace_id, float(ts_raw))))
            else:
                self._send(404, json.dumps({"error": "not found"}))
        except Exception as e:  # noqa: BLE001 — surface as JSON, don't crash the server
            self._send(500, json.dumps({"error": str(e)}))

    def log_message(self, *args):  # quiet
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8767)
    args = ap.parse_args()
    srv = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Serving Guts usage dashboard at http://localhost:{args.port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
