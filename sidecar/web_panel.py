"""Canonical, dependency-free HTML for the read-only web panel."""

from __future__ import annotations

NONCE_PLACEHOLDER = "__NONCE__"

_PANEL_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Sidecar</title>
"""

_PANEL_BODY_START = """<style nonce="__NONCE__">
body{font:14px system-ui,sans-serif;margin:2rem;color:#1f2328;background:#fff}
form{display:flex;gap:.5rem;max-width:42rem}input{flex:1;padding:.55rem}
button{padding:.55rem 1rem}table{border-collapse:collapse;width:100%;margin-top:1rem}
th,td{border:1px solid #d0d7de;padding:.4rem;text-align:left;vertical-align:top}
#events{white-space:pre-wrap;overflow-wrap:anywhere;max-height:30rem;overflow:auto}
.muted{color:#59636e}
</style>
</head>
<body>
"""

_PANEL_MAIN = """<h1>Agent Sidecar</h1>
<form id="auth" method="post" action="/" autocomplete="off">
<label for="token">Token</label>
<input id="token" type="password" autocomplete="off" required>
<button id="connect" type="button">Connect</button>
</form>
<p id="message" class="muted">Enter the token from the private runtime file.</p>
<h2>Active sessions</h2>
<table>
<thead><tr><th>Agent</th><th>Session</th><th>Project</th><th>Status</th><th>Title</th></tr></thead>
<tbody id="sessions"></tbody>
</table>
<h2>Events</h2>
<div id="events" aria-live="polite"></div>
"""

_PANEL_SCRIPT = """<script nonce="__NONCE__">
"use strict";
const form=document.getElementById("auth");
const input=document.getElementById("token");
const connectButton=document.getElementById("connect");
const message=document.getElementById("message");
const sessions=document.getElementById("sessions");
const events=document.getElementById("events");
let bearer="";
let eventController=null;
function headers(){return {"Authorization":"Bearer "+bearer};}
function cell(row,value){
  const item=document.createElement("td");
  item.textContent=value===null||value===undefined?"":String(value);
  row.appendChild(item);
}
function showSessions(items){
  sessions.replaceChildren();
  items.filter(function(item){
    return item&&(item.status==="working"||item.status==="waiting");
  }).forEach(function(item){
    const row=document.createElement("tr");
    cell(row,item.agent);cell(row,item.session_id);cell(row,item.project);
    cell(row,item.status);cell(row,item.title);sessions.appendChild(row);
  });
}
function showEvent(item){
  const line=document.createElement("div");
  line.textContent=JSON.stringify(item);
  events.appendChild(line);
  while(events.childElementCount>200){events.removeChild(events.firstElementChild);}
}
async function loadStatus(){
  const response=await fetch("/api/v1/status",{
    method:"GET",headers:headers(),cache:"no-store",credentials:"omit"
  });
  if(!response.ok){throw new Error("Status request failed");}
  const payload=await response.json();
  showSessions(Array.isArray(payload.sessions)?payload.sessions:[]);
}
async function streamEvents(controller){
  const response=await fetch("/api/v1/events",{
    method:"GET",headers:headers(),cache:"no-store",credentials:"omit",
    signal:controller.signal
  });
  if(!response.ok){throw new Error("Event request failed");}
  const reader=response.body.getReader();
  const decoder=new TextDecoder();
  let pending="";
  while(true){
    const result=await reader.read();
    pending+=decoder.decode(result.value||new Uint8Array(),{stream:!result.done});
    const lines=pending.split("\\n");pending=lines.pop();
    lines.forEach(function(line){if(line){showEvent(JSON.parse(line));}});
    if(result.done){if(pending){showEvent(JSON.parse(pending));}break;}
  }
}
async function connect(event){
  event.preventDefault();
  bearer=input.value;input.value="";
  if(eventController){eventController.abort();}
  const controller=new AbortController();
  eventController=controller;
  message.textContent="Connecting…";
  try{
    await loadStatus();
    message.textContent="Connected";
    streamEvents(controller).catch(function(){
      if(!controller.signal.aborted){message.textContent="Event stream ended";}
    });
  }catch(error){
    message.textContent="Authentication or connection failed";
  }
}
form.addEventListener("submit",connect);
connectButton.addEventListener("click",connect);
</script>
</body>
</html>
"""


def _apply_nonce(fragment: str, nonce: str) -> str:
    return fragment.replace(NONCE_PLACEHOLDER, nonce)


def render_panel(
    nonce: str = NONCE_PLACEHOLDER,
    *,
    head_html: str = "",
    body_html: str = "",
    before_script_html: str = "",
) -> str:
    """Render the panel with explicit, build-time-only extension points."""
    return "".join(
        (
            _PANEL_HEAD,
            head_html,
            _apply_nonce(_PANEL_BODY_START, nonce),
            body_html,
            _PANEL_MAIN,
            before_script_html,
            _apply_nonce(_PANEL_SCRIPT, nonce),
        )
    )


PANEL_HTML = render_panel()

__all__ = ["NONCE_PLACEHOLDER", "PANEL_HTML", "render_panel"]
