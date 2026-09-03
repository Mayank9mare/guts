#!/usr/bin/env python3
"""Local, read-only web dashboard for the psychological personas Guts builds.

Guts keeps a rolling psych profile of everyone it talks to at profiles/<user_id>.md
(written by profile_manager.py). This serves a tiny single-page dashboard at
http://localhost:8766 that shows every persona as a card — name, the psychological
read, the tone Guts uses, expertise, and rolling history — with live search + sort.

Read-only. Stdlib only. Independent of the running bot — start/stop whenever:

    python3 persona_viewer.py [--port 8766]

It never calls Slack and never writes anything — it just parses the local .md files.
"""
import argparse
import json
import os
import re
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILES_DIR = os.path.join(HERE, "profiles")

# Best-effort id -> friendly label (same convention as dm_viewer.py). The profile's own
# `name:` field wins when present; this is just a fallback for the card subtitle / id.
# Seed your own here if you want friendly labels (e.g. {"U0123ABCD": "Alex"}) — empty by
# default, unknown ids just fall back to the raw id.
KNOWN_LABELS: dict[str, str] = {}

# Fields we pull out of the profile frontmatter for the card. Anything else in the
# file is preserved in `raw` and shown in the expanded view.
_FIELD_KEYS = ("name", "reads", "tone", "expertise", "history")


def _parse_profile(text: str) -> dict:
    """Parse a profile .md (YAML-ish frontmatter between --- fences) into a dict.

    Deliberately forgiving: the profiler is an LLM, so we don't assume strict YAML.
    We scan for `key: value` lines (value may run to the next known key), and treat a
    trailing block after `history:` as multi-line history. Unknown text lands in raw."""
    fields = {k: "" for k in _FIELD_KEYS}
    # Strip the leading/trailing --- fences if present.
    body = text.strip()
    if body.startswith("---"):
        body = body[3:]
    if body.endswith("---"):
        body = body[:-3]
    body = body.strip()

    lines = body.splitlines()
    cur_key = None
    buf: dict[str, list[str]] = {k: [] for k in _FIELD_KEYS}
    for ln in lines:
        m = re.match(r"^\s*([a-z_]+)\s*:\s*(.*)$", ln)
        if m and m.group(1).lower() in _FIELD_KEYS:
            cur_key = m.group(1).lower()
            rest = m.group(2).strip()
            # `history: |` block-scalar marker — start collecting following lines
            if rest and rest != "|":
                buf[cur_key].append(rest)
        elif cur_key is not None:
            # continuation / block content for the current key
            buf[cur_key].append(ln.strip())
    for k in _FIELD_KEYS:
        fields[k] = "\n".join(x for x in buf[k]).strip()
    return fields


def list_personas() -> list[dict]:
    out = []
    if not os.path.isdir(PROFILES_DIR):
        return out
    for fn in os.listdir(PROFILES_DIR):
        if not fn.endswith(".md"):
            continue
        uid = fn[:-3]
        path = os.path.join(PROFILES_DIR, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            mtime = os.path.getmtime(path)
        except Exception:
            continue
        parsed = _parse_profile(text)
        # Prefer a real name; but the profiler sometimes writes the raw uid as `name:`
        # (bot token lacks users:read for a Slack lookup). If the parsed name is empty OR
        # just the uid, fall back to the friendly label map, then the uid as last resort.
        parsed_name = (parsed.get("name") or "").strip()
        name = parsed_name if parsed_name and parsed_name != uid else KNOWN_LABELS.get(uid, uid)
        out.append({
            "user_id": uid,
            "name": name,
            "reads": parsed.get("reads", ""),
            "tone": parsed.get("tone", ""),
            "expertise": parsed.get("expertise", ""),
            "history": parsed.get("history", ""),
            "raw": text.strip(),
            "updated": mtime,
            "updated_iso": datetime.fromtimestamp(mtime).strftime("%b %d, %H:%M"),
        })
    # default: most-recently-updated first
    out.sort(key=lambda p: p["updated"], reverse=True)
    return out


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Guts Personas</title>
<style>
 *{box-sizing:border-box} body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
   background:#1a1d21;color:#d1d2d3}
 header{padding:16px 22px;border-bottom:1px solid #34373b;background:#19171d;display:flex;
   align-items:center;gap:16px;position:sticky;top:0;z-index:5}
 header h1{font-size:16px;margin:0;color:#fff;flex:none}
 header .count{font-size:12px;color:#9a9b9d}
 input,select{background:#2c2f33;color:#d1d2d3;border:1px solid #3a3d42;border-radius:6px;
   padding:7px 11px;font-size:13px}
 input{width:240px} input::placeholder{color:#777}
 #grid{padding:20px;display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}
 .card{background:#222529;border:1px solid #34373b;border-radius:10px;padding:16px;
   display:flex;flex-direction:column;gap:9px;transition:border-color .15s}
 .card:hover{border-color:#1164a3}
 .card .head{display:flex;align-items:baseline;gap:8px}
 .card .nm{font-weight:700;font-size:15px;color:#fff}
 .card .uid{font-size:11px;color:#777}
 .card .upd{margin-left:auto;font-size:11px;color:#8a8b8d;flex:none}
 .fld{font-size:13px} .fld .k{color:#e0a458;font-weight:600;text-transform:uppercase;
   font-size:10px;letter-spacing:.05em;display:block;margin-bottom:2px}
 .fld .v{white-space:pre-wrap;word-wrap:break-word;color:#cfd1d3}
 .hist .v{color:#9a9b9d;font-size:12px}
 .empty{color:#777;text-align:center;padding:60px;grid-column:1/-1}
 .raw{display:none;margin-top:6px;background:#17191c;border:1px solid #2c2f33;border-radius:6px;
   padding:10px;font:11px/1.4 ui-monospace,Menlo,monospace;color:#9a9b9d;white-space:pre-wrap}
 .togg{font-size:11px;color:#6a9fd4;cursor:pointer;user-select:none}
 .togg:hover{text-decoration:underline}
</style></head><body>
<header>
 <h1>🗡️ Guts — Personas</h1>
 <input id="q" placeholder="search name / read / tone…" oninput="render()">
 <select id="sort" onchange="render()">
   <option value="recent">sort: recently updated</option>
   <option value="name">sort: name (A→Z)</option>
 </select>
 <span class="count" id="count"></span>
 <span style="flex:1"></span>
 <button onclick="load()" style="background:#2d7d46;color:#fff;border:0;border-radius:5px;padding:7px 14px;cursor:pointer">Refresh</button>
</header>
<div id="grid"><div class="empty">Loading…</div></div>
<script>
let ALL=[];
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function fld(k,v){if(!v)return '';return '<div class="fld '+(k==='history'?'hist':'')+'"><span class="k">'+k+'</span><span class="v">'+esc(v)+'</span></div>';}
function card(p){
 return '<div class="card">'+
  '<div class="head"><span class="nm">'+esc(p.name)+'</span>'+
   '<span class="uid">'+esc(p.user_id)+'</span>'+
   '<span class="upd">upd '+esc(p.updated_iso)+'</span></div>'+
  fld('reads',p.reads)+fld('tone',p.tone)+fld('expertise',p.expertise)+fld('history',p.history)+
  '<span class="togg" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display===\\'block\\'?\\'none\\':\\'block\\'">show raw ▾</span>'+
  '<div class="raw">'+esc(p.raw)+'</div>'+
 '</div>';
}
function render(){
 const q=document.getElementById('q').value.toLowerCase().trim();
 const sort=document.getElementById('sort').value;
 let list=ALL.filter(p=>{
   if(!q)return true;
   return (p.name+' '+p.reads+' '+p.tone+' '+p.expertise+' '+p.history+' '+p.user_id).toLowerCase().includes(q);
 });
 if(sort==='name')list=[...list].sort((a,b)=>a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
 else list=[...list].sort((a,b)=>b.updated-a.updated);
 document.getElementById('count').textContent=list.length+' / '+ALL.length;
 const g=document.getElementById('grid');
 g.innerHTML=list.length?list.map(card).join(''):'<div class="empty">No personas yet. Guts writes one after it talks to someone.</div>';
}
async function load(){
 const r=await fetch('/api/personas');ALL=await r.json();render();
}
load();
</script></body></html>"""


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
        try:
            if parsed.path == "/":
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif parsed.path == "/api/personas":
                self._send(200, json.dumps(list_personas()))
            else:
                self._send(404, json.dumps({"error": "not found"}))
        except Exception as e:  # noqa: BLE001 — surface as JSON, don't crash the server
            self._send(500, json.dumps({"error": str(e)}))

    def log_message(self, *args):  # quiet
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    args = ap.parse_args()
    srv = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Serving Guts persona dashboard at http://localhost:{args.port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
