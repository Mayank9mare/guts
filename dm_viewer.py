#!/usr/bin/env python3
"""Local, read-only web viewer for the Guts bot's Slack DM conversations.

Serves a tiny single-page app at http://localhost:8765 that lists every DM the
bot (@guts) has, and shows each conversation BOTH sides — messages from guts and
replies from the user. Uses ONLY the bot token; it never sends/deletes anything
and never touches the Slack MCP (which would act as the admin's personal account).

Stdlib only. Independent of the running bot — start/stop it whenever.

    python3 dm_viewer.py [--port 8765]
"""
import argparse
import html
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

# Best-effort id->label map. The bot token typically lacks users:read, so we can't
# resolve display names from Slack — seed your own here if you want friendly labels
# (e.g. {"U0123ABCD": "Alex"}); unknown ids just fall back to the raw id.
KNOWN_LABELS: dict[str, str] = {}


def _env(key: str) -> str:
    for line in open(os.path.join(HERE, ".env")):
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in .env")


TOKEN = _env("SLACK_BOT_TOKEN")


def slack_get(method: str, **params) -> dict:
    """GET a Slack Web API method with the bot token. Read-only methods only."""
    url = f"https://slack.com/api/{method}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _discover_bot_user_id() -> str:
    """auth.test needs no extra scopes and returns this token's own bot user id —
    avoids hardcoding a Slack app's user id into the source."""
    r = slack_get("auth.test")
    uid = r.get("user_id") if r.get("ok") else None
    if not uid:
        raise SystemExit(f"auth.test failed: {r.get('error', r)}")
    return uid


BOT_USER_ID = _discover_bot_user_id()
KNOWN_LABELS.setdefault(BOT_USER_ID, "guts")


def label_for(user_id: str) -> str:
    return KNOWN_LABELS.get(user_id, user_id or "?")


def list_conversations() -> list[dict]:
    out = []
    cursor = None
    while True:
        params = {"types": "im", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = slack_get("conversations.list", **params)
        if not r.get("ok"):
            break
        for c in r.get("channels", []):
            uid = c.get("user", "")
            out.append({"channel_id": c.get("id"), "user_id": uid, "label": label_for(uid)})
        cursor = (r.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    out.sort(key=lambda c: c["label"].lower())
    return out


def _norm_msg(m: dict) -> dict:
    is_bot = bool(m.get("bot_id")) or m.get("user") == BOT_USER_ID
    uid = m.get("user", "")
    ts = float(m.get("ts", "0"))
    return {
        "ts": m.get("ts"),
        "is_bot": is_bot,
        "sender": "guts" if is_bot else label_for(uid),
        "user_id": uid,
        "text": m.get("text", "") or "",
        "iso_time": datetime.fromtimestamp(ts).strftime("%b %d, %H:%M") if ts else "",
    }


def messages_for(channel: str) -> list[dict]:
    r = slack_get("conversations.history", channel=channel, limit=100)
    if not r.get("ok"):
        return [{"ts": "0", "is_bot": False, "sender": "error",
                 "user_id": "", "text": f"Slack error: {r.get('error')}", "iso_time": ""}]
    by_ts = {}
    for m in r.get("messages", []):
        by_ts[m.get("ts")] = _norm_msg(m)
        # pull thread replies (Guts convos are assistant threads)
        if m.get("reply_count") or m.get("thread_ts") == m.get("ts"):
            rr = slack_get("conversations.replies", channel=channel, ts=m.get("ts"), limit=200)
            if rr.get("ok"):
                for rm in rr.get("messages", []):
                    by_ts[rm.get("ts")] = _norm_msg(rm)
    msgs = sorted(by_ts.values(), key=lambda x: float(x["ts"]))
    return msgs


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Guts DMs</title>
<style>
 *{box-sizing:border-box} body{margin:0;font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;
   background:#1a1d21;color:#d1d2d3;display:flex;height:100vh}
 #side{width:280px;border-right:1px solid #34373b;overflow-y:auto;flex:none;background:#19171d}
 #side h1{font-size:15px;padding:14px 16px;margin:0;color:#fff;border-bottom:1px solid #34373b}
 .conv{padding:11px 16px;cursor:pointer;border-bottom:1px solid #232529}
 .conv:hover{background:#27292d} .conv.active{background:#1164a3;color:#fff}
 .conv .lbl{font-weight:600} .conv .sub{font-size:11px;color:#9a9b9d;margin-top:2px}
 #main{flex:1;display:flex;flex-direction:column;min-width:0}
 #bar{padding:12px 18px;border-bottom:1px solid #34373b;display:flex;align-items:center;gap:12px;background:#222529}
 #bar .t{font-weight:600;font-size:15px;color:#fff} #bar .id{font-size:11px;color:#888}
 button{background:#2d7d46;color:#fff;border:0;border-radius:5px;padding:7px 14px;cursor:pointer;font-size:13px}
 button:hover{background:#358a4f}
 #msgs{flex:1;overflow-y:auto;padding:18px;display:flex;flex-direction:column;gap:10px}
 .row{display:flex} .row.guts{justify-content:flex-end}
 .bub{max-width:72%;padding:9px 13px;border-radius:12px;white-space:pre-wrap;word-wrap:break-word}
 .row.them .bub{background:#2c2f33;border-bottom-left-radius:3px}
 .row.guts .bub{background:#1164a3;color:#fff;border-bottom-right-radius:3px}
 .meta{font-size:10px;color:#9a9b9d;margin-bottom:3px}
 .empty{color:#777;margin:auto;text-align:center}
 .bub b{font-weight:700}
</style></head><body>
<div id="side"><h1>🗡️ Guts DMs</h1><div id="convs"></div></div>
<div id="main">
 <div id="bar"><span class="t" id="title">Select a conversation</span>
   <span class="id" id="cid"></span><span style="flex:1"></span>
   <button id="refresh" style="display:none">Refresh</button></div>
 <div id="msgs"><div class="empty">Pick someone on the left to see the DM.</div></div>
</div>
<script>
let cur=null;
function esc(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function fmt(s){return esc(s).replace(/\\*([^*\\n]+)\\*/g,'<b>$1</b>');}
async function loadConvs(){
 const r=await fetch('/api/conversations');const cs=await r.json();
 const box=document.getElementById('convs');box.innerHTML='';
 cs.forEach(c=>{const d=document.createElement('div');d.className='conv';
  d.innerHTML='<div class="lbl">'+esc(c.label)+'</div><div class="sub">'+esc(c.user_id)+'</div>';
  d.onclick=()=>{document.querySelectorAll('.conv').forEach(x=>x.classList.remove('active'));
   d.classList.add('active');open(c);};box.appendChild(d);});
}
async function open(c){
 cur=c;document.getElementById('title').textContent=c.label;
 document.getElementById('cid').textContent=c.channel_id;
 document.getElementById('refresh').style.display='inline-block';
 const m=document.getElementById('msgs');m.innerHTML='<div class="empty">Loading…</div>';
 const r=await fetch('/api/messages?channel='+encodeURIComponent(c.channel_id));
 const ms=await r.json();m.innerHTML='';
 if(!ms.length){m.innerHTML='<div class="empty">No messages.</div>';return;}
 ms.forEach(x=>{const row=document.createElement('div');row.className='row '+(x.is_bot?'guts':'them');
  row.innerHTML='<div><div class="meta">'+esc(x.sender)+' · '+esc(x.iso_time)+'</div>'+
   '<div class="bub">'+fmt(x.text||'')+'</div></div>';m.appendChild(row);});
 m.scrollTop=m.scrollHeight;
}
document.getElementById('refresh').onclick=()=>{if(cur)open(cur);};
loadConvs();
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
            elif parsed.path == "/api/conversations":
                self._send(200, json.dumps(list_conversations()))
            elif parsed.path == "/api/messages":
                qs = urllib.parse.parse_qs(parsed.query)
                ch = (qs.get("channel") or [""])[0]
                if not ch:
                    self._send(400, json.dumps({"error": "channel required"}))
                    return
                self._send(200, json.dumps(messages_for(ch)))
            else:
                self._send(404, json.dumps({"error": "not found"}))
        except Exception as e:  # noqa: BLE001 — surface as JSON, don't crash the server
            self._send(500, json.dumps({"error": str(e)}))

    def log_message(self, *args):  # quiet
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    srv = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Serving Guts DM viewer at http://localhost:{args.port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
