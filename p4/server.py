"""Poltergeist P4 server: control-room page, Banshee dispatch and PA voice.

Run:  python server.py          (then open http://localhost:8000)
Demo: python simulate.py        (in a second terminal, fakes P1-P3)
"""
import asyncio
import io
import json
import os
import socket
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

import pa
from banshee import Banshee
from engine import OPEN_STATUSES, RULES, Engine

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path=os.path.join(HERE, ".env")):
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


load_env()
PORT = int(os.environ.get("PORT", 8000))
VENUE_PATH = os.environ.get("VENUE", os.path.join(HERE, "venue.json"))
VENUE = json.load(open(VENUE_PATH, encoding="utf-8"))
PUBLIC_URL = os.environ.get("PUBLIC_URL") or f"http://{lan_ip()}:{PORT}"

voice = pa.Voice(os.path.join(HERE, "pa_cache"))
engine = Engine(VENUE)
banshee = Banshee(engine, VENUE, token=os.environ.get("TELEGRAM_BOT_TOKEN"),
                  api_base=os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org"),
                  staff_code=os.environ.get("STAFF_CODE"))


def pa_texts_for_incident(inc):
    first_kind = inc["chain"][0]["kind"] if inc.get("chain") else (inc["kinds"][0] if inc["kinds"] else None)
    return pa.build(inc.get("pa"), VENUE["zones"][inc["zone"]], inc.get("redirect"), first_kind)


def on_incident(inc):
    async def go():
        await banshee.dispatch(inc)
        inc["pa_texts"] = pa_texts_for_incident(inc)
        await banshee.notify_public(inc, inc["pa_texts"])
        await voice.prewarm(inc["pa_texts"])
    asyncio.get_running_loop().create_task(go())


engine.on_incident = on_incident


@asynccontextmanager
async def lifespan(app):
    tasks = [asyncio.create_task(banshee.run()), asyncio.create_task(banshee.poll())]
    print(f"\n  Control room:  http://localhost:{PORT}")
    print(f"  SOS QR sheet:  http://localhost:{PORT}/qr   (phones on the same Wi-Fi use {PUBLIC_URL})")
    print(f"  Telegram:      {banshee.status['detail'] or 'starting'}\n")
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Poltergeist P4", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


# ---------------- pages ----------------
@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.get("/sos/{zone}")
def sos_page(zone: str):
    if zone not in VENUE["zones"]:
        raise HTTPException(404, "Unknown zone")
    return FileResponse(os.path.join(HERE, "static", "sos.html"))


@app.get("/qr", response_class=HTMLResponse)
def qr_sheet():
    bot = banshee.username
    cards = "".join(
        f'<figure><b>{z.get("name", zid)}</b><div class="pair">'
        f'<div><img src="/api/qr/{zid}.png" alt=""><span>Need help? Scan to alert the control room.</span></div>'
        + (f'<div><img src="/api/qr-tg/{zid}.png" alt=""><span>Crowd updates for this area on Telegram.</span></div>'
           if bot else '')
        + f'</div><small>{PUBLIC_URL}/sos/{zid}</small></figure>'
        for zid, z in VENUE["zones"].items())
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>SOS QR codes</title>
<style>@font-face{{font-family:Overpass;src:url(/static/fonts/overpass-latin-700-normal.woff2)}}
body{{font-family:Overpass,system-ui,sans-serif;margin:24px;color:#222}} h1{{font-size:20px}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:24px}}
figure{{margin:0;border:2px solid #222;border-radius:12px;padding:16px;text-align:center;break-inside:avoid;display:grid;gap:8px}}
.pair{{display:flex;justify-content:center;gap:16px}} .pair div{{display:grid;gap:4px;width:180px}}
img{{width:180px;height:180px}} b{{font-size:22px}}
small{{color:#666;font-size:11px;word-break:break-all}}</style></head>
<body><h1>Print these and stick one in each zone</h1><main>{cards}</main></body></html>"""


# ---------------- data in (from P1-P3) ----------------
@app.post("/api/frame")
async def frame_in(request: Request, camera: str = "cam1"):
    data = await request.body()
    if not data:
        raise HTTPException(400, "Send JPEG bytes as the request body")
    prev = engine.frames.get(camera)
    size = prev["size"] if prev else None
    if size is None or (prev and prev["seq"] % 50 == 0):
        try:
            from PIL import Image
            size = list(Image.open(io.BytesIO(data)).size)
        except Exception:
            size = size or [16, 9]
    engine.frames[camera] = {"seq": (prev["seq"] + 1) if prev else 1, "jpeg": data, "ts": time.time(), "size": size}
    return {"ok": True}


@app.post("/api/zones")
async def zones_in(payload: dict):
    zones = payload.get("zones", payload)
    engine.ingest_zones(zones)
    return {"ok": True, "unknown_zones": sorted(engine.unknown_zones)}


@app.post("/api/event")
async def event_in(payload: dict):
    etype = payload.get("type")
    zone = payload.get("zone")
    if not etype or not zone:
        raise HTTPException(400, "Need 'type' and 'zone'")
    inc = engine.ingest_event(etype, zone, bbox=payload.get("bbox"), confidence=payload.get("confidence"),
                              note=payload.get("note"))
    if inc is None:
        raise HTTPException(404, f"Unknown zone '{zone}'. Add it to venue.json.")
    return {"ok": True, "incident": inc["id"]}


@app.post("/api/sos/{zone}")
async def sos_in(zone: str, payload: dict | None = None):
    payload = payload or {}
    kind = payload.get("kind") if payload.get("kind") in ("medical", "crowding") else None
    note = {"medical": "Someone needs medical help", "crowding": "Too crowded, people are being pushed"}.get(kind)
    inc = engine.ingest_event("sos", zone, sos_kind=kind, note=note, source="sos")
    if inc is None:
        raise HTTPException(404, "Unknown zone")
    return {"ok": True, "incident": inc["id"]}


# ---------------- control room actions ----------------
@app.post("/api/incidents/{inc_id}/accept")
async def act_accept(inc_id: int, payload: dict | None = None):
    ok, msg = await banshee.accept(inc_id, (payload or {}).get("post"), via="dashboard")
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 409)


@app.post("/api/incidents/{inc_id}/next")
async def act_next(inc_id: int):
    return {"ok": await banshee.redispatch(inc_id)}


@app.post("/api/incidents/{inc_id}/resolve")
async def act_resolve(inc_id: int):
    return {"ok": await banshee.resolve(inc_id, via="dashboard")}


@app.post("/api/test-alert/{zone}")
async def test_alert(zone: str, payload: dict | None = None):
    """Raise an alert by hand, e.g. for a rehearsal. type defaults to person_down."""
    etype = (payload or {}).get("type", "person_down")
    inc = engine.open_incident(etype, zone, source="dashboard") if zone in VENUE["zones"] else None
    if inc is None:
        raise HTTPException(404, "Unknown zone")
    return {"ok": True, "incident": inc["id"]}


@app.post("/api/reset")
async def reset():
    engine.reset()
    return {"ok": True}


@app.get("/api/incidents/{inc_id}/snapshot.jpg")
def snapshot(inc_id: int):
    data = engine.snapshots.get(inc_id)
    if not data:
        raise HTTPException(404)
    return Response(data, media_type="image/jpeg")


@app.get("/api/incidents/{inc_id}/public")
def incident_public(inc_id: int):
    """What the SOS page shows the person who asked for help."""
    inc = engine.incidents.get(inc_id)
    if not inc:
        raise HTTPException(404)
    post = banshee.posts.get(inc["accepted_by"] or inc["current_post"] or "", {})
    return {"status": inc["status"], "post": post.get("name"), "zone": inc["zone_name"]}


# ---------------- data out (to the page) ----------------
@app.get("/api/venue")
def venue():
    return {**VENUE, "public_url": PUBLIC_URL, "rules": {k: v["label"] for k, v in RULES.items()}}


def pa_focus():
    """Which announcement the PA panel should offer right now."""
    for inc in engine.incident_view():
        if inc["status"] in OPEN_STATUSES and inc.get("pa_texts"):
            redirect = (inc.get("redirect") or {}).get("name") if inc.get("pa") == "crowd" else None
            return {"for": "incident", "ref": inc["id"], "title": f"{inc['label']} at {inc['zone_name']}",
                    "redirect": redirect, "kind": inc.get("pa"), "texts": inc["pa_texts"]}
    warn = [z for z in engine.zones.values() if z["level"] in ("warning", "critical") and z["density"] is not None]
    if warn:
        z = max(warn, key=lambda z: z["density"])
        red = engine.redirect_for(z["id"])
        return {"for": "zone", "ref": z["id"], "title": f"{VENUE['zones'][z['id']]['name']} is getting crowded",
                "redirect": (red or {}).get("name"), "kind": "crowd",
                "texts": pa.build("crowd", VENUE["zones"][z["id"]], red)}
    return None


def state():
    now = time.time()
    incs = []
    for inc in engine.incident_view():
        incs.append({k: v for k, v in inc.items() if k != "kinds"})
    return {
        "now": now,
        "zones": engine.zone_view(),
        "incidents": incs,
        "stats": engine.stats(),
        "escalate_after_s": banshee.escalate_after,
        "telegram": banshee.status,
        "voice": {"available": voice.available, "error": voice.last_error},
        "pa": pa_focus(),
        "cameras": {cam: {"last_frame": f["ts"], "size": f["size"]} for cam, f in engine.frames.items()},
        "unknown_zones": sorted(engine.unknown_zones),
    }


@app.get("/api/state")
def state_once():
    return state()


@app.get("/api/stream")
async def stream(request: Request):
    async def gen():
        while not await request.is_disconnected():
            yield f"data: {json.dumps(state(), ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.5)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/video/{camera}")
async def video(camera: str, request: Request):
    async def gen():
        last = -1
        while not await request.is_disconnected():
            f = engine.frames.get(camera)
            if f and f["seq"] != last:
                last = f["seq"]
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(f["jpeg"])).encode() + b"\r\n\r\n" + f["jpeg"] + b"\r\n")
            await asyncio.sleep(0.04)
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/pa/audio")
async def pa_audio(lang: str, text: str):
    path = await voice.mp3(lang, text)
    if not path:
        raise HTTPException(503, voice.last_error or "Voice unavailable")
    return FileResponse(path, media_type="audio/mpeg")


@app.get("/api/qr-tg/{zone}.png")
def qr_tg_png(zone: str):
    import qrcode
    if not banshee.username or zone not in VENUE["zones"]:
        raise HTTPException(404, "Telegram isn't connected")
    img = qrcode.make(f"https://t.me/{banshee.username}?start=z_{zone}", box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png")


@app.get("/api/qr/{zone}.png")
def qr_png(zone: str):
    import qrcode
    img = qrcode.make(f"{PUBLIC_URL}/sos/{zone}", box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
