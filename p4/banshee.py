"""Banshee: sends each incident to the nearest suitable help post and escalates if nobody accepts.

Telegram is optional. Without a bot token the alert still goes through the same steps, and the
control room accepts on the post's behalf.

One bot, two kinds of user (saved in subscribers.json, so restarts keep them):
- Staff get every dispatch alert with Accept / Can't go. The first to accept takes it.
  If STAFF_CODE is set in .env, people join as staff with "/staff CODE".
- People at the event pick their zone and language (or scan a zone's QR code) and get only
  crowd updates for their zone, in their language, plus an all-clear. /where changes zone.
- A post with its own telegram_chat_id in venue.json gets its alerts only in that chat.
"""
import asyncio
import html
import json
import math
import os
import tempfile
import time

import httpx

import pa
from engine import OPEN_STATUSES, centroid

LANG_BUTTONS = [[{"text": "English", "callback_data": "lang:en"}, {"text": "हिन्दी", "callback_data": "lang:hi"},
                 {"text": "ಕನ್ನಡ", "callback_data": "lang:kn"}]]
ROLE_BUTTONS = [[{"text": "I work here (staff)", "callback_data": "role:staff"}],
                [{"text": "I'm at the event", "callback_data": "role:public"}]]

KIND_LABEL = {"medical": "Medical", "security": "Security", "police": "Police", "rpf": "Railway police"}


class Banshee:
    def __init__(self, engine, venue, token=None, api_base="https://api.telegram.org", log=print, staff_code=None):
        self.engine = engine
        self.venue = venue
        self.posts = {p["id"]: p for p in venue.get("posts", [])}
        self.escalate_after = float(venue.get("escalate_after_s", 60))
        self.token = (token or "").strip() or None
        self.api = f"{api_base.rstrip('/')}/bot{self.token}/" if self.token else None
        self.client = httpx.AsyncClient(timeout=40) if self.token else None
        self.log = log
        self.messages = {}   # incident id -> [{"chat_id", "message_id", "post", "photo"}]
        self.status = {"state": "off" if not self.token else "starting",
                       "detail": "No bot token, so posts accept from the dashboard." if not self.token else ""}
        self._offset = 0
        self.staff_code = (staff_code or "").strip() or None
        self.username = None
        self.subscribers = {}   # chat id (str) -> {"name", "role": "staff"|"public"|None, "lang", "zone"}
        self.subs_path = self._find_subs_path()
        self._load_subscribers()

    # ---------- subscribers ----------
    def _find_subs_path(self):
        here = os.path.dirname(os.path.abspath(__file__))
        for d in (here, os.path.join(tempfile.gettempdir(), "poltergeist")):
            try:
                os.makedirs(d, exist_ok=True)
                path = os.path.join(d, "subscribers.json")
                with open(path, "a", encoding="utf-8"):
                    pass
                return path
            except OSError:
                continue
        return None

    def _load_subscribers(self):
        try:
            with open(self.subs_path, encoding="utf-8") as f:
                data = f.read().strip()
            raw = json.loads(data) if data else {}
            # older files stored just a name: those people were staff
            self.subscribers = {str(k): (v if isinstance(v, dict) else {"name": v, "role": "staff"})
                                for k, v in raw.items()}
        except Exception:
            self.subscribers = {}

    def _save_subscribers(self):
        if not self.subs_path:
            return
        try:
            with open(self.subs_path, "w", encoding="utf-8") as f:
                json.dump(self.subscribers, f, ensure_ascii=False, indent=2)
        except OSError as e:
            self.log(f"[banshee] couldn't save subscribers: {e}")

    def _targets(self, post_id):
        """Chats that should get alerts for this post."""
        chat = self.posts.get(post_id, {}).get("telegram_chat_id")
        if chat:
            return [str(chat)]
        return [cid for cid, s in self.subscribers.items() if s.get("role") == "staff"]

    def _status_line(self, username=None):
        staff = sum(1 for s in self.subscribers.values() if s.get("role") == "staff")
        public = sum(1 for s in self.subscribers.values() if s.get("role") == "public")
        who = f"Connected as @{username}" if username else "Connected"
        return f"{who}, {staff} staff and {public} people at the event subscribed"

    def _refresh_status(self):
        if self.status.get("state") == "connected":
            self.status = {"state": "connected", "detail": self._status_line(self.username)}

    # ---------- choosing posts ----------
    def distance(self, zone_id, post):
        zmap = self.venue["zones"][zone_id].get("map")
        if not zmap or not post.get("pos"):
            return None
        cx, cy = centroid(zmap)
        return round(math.hypot(post["pos"][0] - cx, post["pos"][1] - cy))

    def chain_for(self, inc):
        cands = []
        for p in self.posts.values():
            if p.get("kind") in inc["kinds"]:
                d = self.distance(inc["zone"], p)
                cands.append({"id": p["id"], "name": p["name"], "kind": p["kind"],
                              "distance_m": d, "telegram": bool(self.token and self._targets(p["id"]))})
        # The best-suited team first (order of "kinds" in the rule), nearest first within a team.
        cands.sort(key=lambda c: (inc["kinds"].index(c["kind"]), c["distance_m"] is None, c["distance_m"] or 0))
        return cands

    def _post_in_chain(self, inc, post_id):
        for c in inc["chain"]:
            if c["id"] == post_id:
                return c
        return None

    # ---------- incident flow ----------
    async def dispatch(self, inc):
        inc["chain"] = self.chain_for(inc)
        if not inc["chain"]:
            inc["status"] = "unanswered"
            self.engine._log(inc, "unanswered", "No help post in venue.json can take this. Call for help directly.")
            return
        await self._send_next(inc)

    async def _send_next(self, inc, reason=None):
        prev = self._post_in_chain(inc, inc["current_post"]) if inc["current_post"] else None
        if prev is not None:
            await self._tg_close(inc, f"Reassigned: {reason}" if reason else "Reassigned")
        inc["step"] += 1
        if inc["step"] >= len(inc["chain"]):
            inc["status"] = "unanswered"
            inc["current_post"] = None
            self.engine._log(inc, "unanswered", "Every post was tried and nobody accepted. Call for help directly.")
            return
        post = inc["chain"][inc["step"]]
        inc["current_post"] = post["id"]
        inc["sent_at"] = time.time()
        inc["status"] = "dispatching"
        dist = f", {post['distance_m']} m away" if post["distance_m"] is not None else ""
        lead = f"{reason}. " if reason else ""
        self.engine._log(inc, "sent", f"{lead}Sent to {post['name']}{dist}")
        await self._tg_send(inc, post)

    async def accept(self, inc_id, post_id=None, via="dashboard", who=None, by_chat=None):
        inc = self.engine.incidents.get(inc_id)
        if not inc or inc["status"] not in ("dispatching", "unanswered", "new"):
            return False, "This alert isn't waiting for anyone."
        post_id = post_id or inc["current_post"]
        post = self._post_in_chain(inc, post_id) or ({"id": post_id, "name": self.posts[post_id]["name"]}
                                                    if post_id in self.posts else None)
        if post is None:
            return False, "Unknown help post."
        inc["status"] = "accepted"
        inc["accepted_by"] = post["id"]
        inc["accepted_at"] = time.time()
        inc["current_post"] = post["id"]
        secs = int(inc["accepted_at"] - inc["opened_at"])
        where = (f"on Telegram ({who})" if who else "on Telegram") if via == "telegram" else "from the control room"
        inc["accepted_by_person"] = who
        self.engine._log(inc, "accepted", f"{post['name']} accepted {where}, {secs} s after detection")
        await self._tg_mark_accepted(inc, post, via, who, by_chat)
        return True, f"{post['name']} is on the way."

    async def decline(self, inc_id, post_id, via="telegram"):
        inc = self.engine.incidents.get(inc_id)
        if not inc or inc["status"] != "dispatching" or inc["current_post"] != post_id:
            return False
        name = self.posts.get(post_id, {}).get("name", post_id)
        await self._send_next(inc, reason=f"{name} can't go")
        return True

    async def redispatch(self, inc_id):
        inc = self.engine.incidents.get(inc_id)
        if not inc or inc["status"] not in ("dispatching", "unanswered", "accepted"):
            return False
        if inc["status"] == "unanswered":
            inc["step"] = -1      # start the chain again
            inc["current_post"] = None
        inc["accepted_by"] = None
        await self._send_next(inc, reason="Control room asked for the next post")
        return True

    async def resolve(self, inc_id, via="dashboard"):
        inc = self.engine.incidents.get(inc_id)
        if not inc or inc["status"] not in OPEN_STATUSES:
            return False
        inc["status"] = "resolved"
        inc["closed_at"] = time.time()
        who = self.posts.get(inc["accepted_by"], {}).get("name") if via == "telegram" else "Control room"
        self.engine._log(inc, "resolved", f"Marked resolved by {who or 'the help post'}")
        await self._tg_close(inc, "Resolved")
        return True

    async def run(self):
        """Escalate alerts that nobody accepted in time."""
        while True:
            try:
                now = time.time()
                for inc in list(self.engine.incidents.values()):
                    if inc["status"] == "dispatching" and inc["sent_at"] and now - inc["sent_at"] >= self.escalate_after:
                        name = self.posts.get(inc["current_post"], {}).get("name", "The post")
                        await self._send_next(inc, reason=f"No answer from {name} in {int(self.escalate_after)} s")
                self.engine.evaluate(now)
                for inc in list(self.engine.incidents.values()):
                    if inc["status"] in ("cleared", "resolved") and inc.get("public_sent") and not inc.get("public_cleared"):
                        inc["public_cleared"] = True
                        await self._public_send(inc["zone"], lambda lang, zn: pa.ALL_CLEAR[lang].format(zone=zn))
            except Exception as e:
                self.log(f"[banshee] loop error: {e!r}")
            await asyncio.sleep(1)

    # ---------- crowd updates for people at the event ----------
    async def _public_send(self, zone, make_text):
        """Send each public subscriber in this zone a message in their language. Returns how many."""
        if not self.token:
            return 0
        zcfg = self.venue["zones"].get(zone, {})
        sent = 0
        for cid, sub in list(self.subscribers.items()):
            if sub.get("role") != "public" or sub.get("zone") != zone:
                continue
            lang = sub.get("lang") or "en"
            try:
                await self._send(cid, make_text(lang, html.escape(pa._name(zcfg, lang))))
                sent += 1
            except Exception as e:
                if "blocked" in str(e).lower():
                    self.subscribers.pop(cid, None)
                    self._save_subscribers()
        return sent

    async def notify_public(self, inc, texts):
        """Tell people standing in the incident's zone what to do (the PA message, in their language)."""
        if not texts:
            return
        n = await self._public_send(inc["zone"], lambda lang, zn: f"<b>{zn}</b>\n{html.escape(texts[lang])}")
        if n:
            inc["public_sent"] = n
            self.engine._log(inc, "note", f"Crowd update sent on Telegram to {n} {'person' if n == 1 else 'people'} "
                                          f"at {inc['zone_name']}")

    async def _send(self, cid, text, buttons=None):
        data = {"chat_id": cid, "parse_mode": "HTML", "text": text}
        if buttons is not None:
            data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
        return await self._call("sendMessage", data=data)

    def _zone_buttons(self):
        rows, row = [], []
        for zid, z in self.venue["zones"].items():
            row.append({"text": z.get("name", zid), "callback_data": f"zone:{zid}"})
            if len(row) == 2:
                rows.append(row)
                row = []
        return rows + ([row] if row else [])

    def _staff_welcome(self, who):
        return (f"You're on the staff list, {html.escape(who)}. Every alert comes here with Accept and Can't go "
                f"buttons; the first person to accept takes it. Send /stop to stop.")

    def _public_status(self, sub):
        zone = self.venue["zones"].get(sub.get("zone") or "", {}).get("name")
        lang = pa.LANG_NAMES.get(sub.get("lang") or "en")
        if not zone:
            return "Where are you now?"
        return (f"You'll get crowd updates for <b>{html.escape(zone)}</b> in {lang}.\n"
                f"Moved? Send /where. Change language: /lang. Stop: /stop.")

    async def _next_public_step(self, cid, sub):
        """Ask for whatever's missing (language, then zone), or confirm."""
        if not sub.get("lang"):
            await self._send(cid, "Which language do you want updates in?", LANG_BUTTONS)
        elif not sub.get("zone"):
            await self._send(cid, "Where are you now?", self._zone_buttons())
        else:
            await self._send(cid, self._public_status(sub))

    # ---------- Telegram ----------
    async def _call(self, method, data=None, files=None):
        r = await self.client.post(self.api + method, data=data, files=files)
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(f"{method}: {body.get('description', r.status_code)}")
        return body["result"]

    def _caption(self, inc, post):
        z = inc["zone_name"]
        lines = [f"<b>{html.escape(inc['label'])} at {html.escape(z)}</b>",
                 f"For: <b>{html.escape(post['name'])}</b>"
                 + (f" ({post['distance_m']} m away)" if post["distance_m"] is not None else "")]
        if inc.get("density") is not None and inc["type"] in ("crush_risk", "surge"):
            lines.append(f"{inc['density']:.1f} people per m²" + (", surging" if inc.get("surge") else ""))
        if inc.get("note"):
            lines.append(html.escape(str(inc["note"])))
        lines.append("Detected " + time.strftime("%H:%M:%S", time.localtime(inc["opened_at"])))
        lines.append(f"Tap Accept if you're going. It moves to the next post in {int(self.escalate_after)} s.")
        return "\n".join(lines)

    async def _tg_send(self, inc, post):
        targets = self._targets(post["id"]) if self.token else []
        if not targets:
            return
        markup = json.dumps({"inline_keyboard": [[
            {"text": "Accept", "callback_data": f"acc:{inc['id']}:{post['id']}"},
            {"text": "Can't go", "callback_data": f"dec:{inc['id']}:{post['id']}"}]]})
        caption = self._caption(inc, post)
        snap = self.engine.snapshots.get(inc["id"])
        failed = []
        for chat in targets:
            try:
                if snap:
                    res = await self._call("sendPhoto", data={"chat_id": chat, "caption": caption, "parse_mode": "HTML",
                                                              "reply_markup": markup},
                                           files={"photo": ("snapshot.jpg", snap, "image/jpeg")})
                else:
                    res = await self._call("sendMessage", data={"chat_id": chat, "text": caption,
                                                                "parse_mode": "HTML", "reply_markup": markup})
                self.messages.setdefault(inc["id"], []).append(
                    {"chat_id": chat, "message_id": res["message_id"], "post": post["id"], "photo": bool(snap),
                     "text": caption})
            except Exception as e:
                failed.append(self.subscribers.get(chat, {}).get("name", chat))
                self.status = {"state": "error", "detail": str(e)[:160]}
                if "blocked" in str(e).lower() and chat in self.subscribers:   # they blocked the bot
                    self.subscribers.pop(chat, None)
                    self._save_subscribers()
        if failed:
            self.engine._log(inc, "note", f"Telegram couldn't reach {', '.join(map(str, failed))}")

    async def _tg_edit(self, msg, text, buttons=None):
        markup = json.dumps({"inline_keyboard": buttons or []})
        method = "editMessageCaption" if msg["photo"] else "editMessageText"
        field = "caption" if msg["photo"] else "text"
        try:
            await self._call(method, data={"chat_id": msg["chat_id"], "message_id": msg["message_id"],
                                           field: text, "parse_mode": "HTML", "reply_markup": markup})
        except Exception as e:
            if "not modified" not in str(e):
                self.log(f"[banshee] edit failed: {e}")

    async def _tg_close(self, inc, note):
        if not self.token:
            return
        for msg in self.messages.get(inc["id"], []):
            if not msg.get("closed"):
                msg["closed"] = True
                await self._tg_edit(msg, msg["text"] + f"\n\n<b>{html.escape(note)}</b>")

    async def _tg_mark_accepted(self, inc, post, via, who=None, by_chat=None):
        if not self.token:
            return
        taker = html.escape(post["name"] + (f" ({who})" if who else ""))
        for msg in self.messages.get(inc["id"], []):
            if msg.get("closed"):
                continue
            mine = msg["post"] == post["id"] and (by_chat is None or str(msg["chat_id"]) == str(by_chat))
            if mine and via == "telegram":
                await self._tg_edit(msg, msg["text"] + "\n\n<b>You accepted. Go now.</b>",
                                    [[{"text": "Mark resolved", "callback_data": f"res:{inc['id']}:{post['id']}"}]])
            elif mine:
                await self._tg_edit(msg, msg["text"] + "\n\n<b>The control room accepted this for you. Go now.</b>",
                                    [[{"text": "Mark resolved", "callback_data": f"res:{inc['id']}:{post['id']}"}]])
            else:
                msg["closed"] = True
                await self._tg_edit(msg, msg["text"] + f"\n\n<b>Taken by {taker}</b>")

    async def poll(self):
        """Long-poll Telegram for button presses and /start messages."""
        if not self.token:
            return
        try:
            me = await self._call("getMe")
            await self._call("deleteWebhook")
            self.username = me.get("username")
            self.status = {"state": "connected", "detail": self._status_line(self.username)}
            self.log(f"[banshee] Telegram connected as @{me.get('username')}")
        except Exception as e:
            self.status = {"state": "error", "detail": f"Telegram rejected the bot: {e}"[:200]}
            self.log(f"[banshee] Telegram error: {e}")
        while True:
            try:
                updates = await self._call("getUpdates", data={
                    "offset": self._offset, "timeout": 25,
                    "allowed_updates": json.dumps(["callback_query", "message"])})
                self.status = {"state": "connected", "detail": self._status_line(self.username)}
                for up in updates:
                    self._offset = up["update_id"] + 1
                    await self._handle(up)
            except Exception as e:
                msg = str(e)
                if "Conflict" in msg:
                    msg = ("Another copy of server.py is using this bot, so button taps may go to the other one. "
                           "Close every other server window, on this laptop and your teammates', and keep just one.")
                    self.log(f"[banshee] {msg}")
                self.status = {"state": "error", "detail": msg[:200]}
                await asyncio.sleep(5)

    async def _handle_setup_button(self, cq):
        """role:, lang: and zone: buttons. Returns True if it was one of these."""
        data = cq.get("data", "")
        kind, _, value = data.partition(":")
        if kind not in ("role", "lang", "zone"):
            return False
        chat = (cq.get("message") or {}).get("chat", {})
        cid = str(chat.get("id"))
        frm = cq.get("from", {})
        who = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or cid
        sub = self.subscribers.setdefault(cid, {"name": who, "role": None})
        note = "Saved."
        if kind == "role" and value == "staff":
            if self.staff_code:
                await self._send(cid, "Send <code>/staff CODE</code> with the code from the control room.")
            else:
                sub["role"] = "staff"
                self.log(f"[banshee] {who} joined as staff (chat id {cid})")
                await self._send(cid, self._staff_welcome(who))
        elif kind == "role":
            sub["role"] = "public"
            await self._next_public_step(cid, sub)
        elif kind == "lang" and value in pa.LANGS:
            sub["lang"] = value
            sub["role"] = sub.get("role") or "public"
            await self._next_public_step(cid, sub)
        elif kind == "zone" and value in self.venue["zones"]:
            sub["zone"] = value
            sub["role"] = sub.get("role") or "public"
            self.log(f"[banshee] {who} is at {value}")
            await self._next_public_step(cid, sub)
            note = f"You're at {self.venue['zones'][value].get('name', value)}."
        self._save_subscribers()
        self._refresh_status()
        try:
            await self._call("answerCallbackQuery", data={"callback_query_id": cq["id"], "text": note})
        except Exception:
            pass
        return True

    async def _handle(self, up):
        if "message" in up:
            m = up["message"]
            chat = m["chat"]
            cid = str(chat["id"])
            who = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or cid
            text = (m.get("text") or "").strip()
            low = text.lower()
            sub = self.subscribers.setdefault(cid, {"name": who, "role": None})
            sub["name"] = who
            if low.startswith("/stop"):
                self.subscribers.pop(cid, None)
                self.log(f"[banshee] {who} unsubscribed")
                await self._send(cid, "You won't get any more messages. Send /start to join again.")
            elif low.startswith("/start z_"):          # scanned a zone's QR code
                zone = text.split("z_", 1)[1].strip()
                if zone in self.venue["zones"]:
                    if sub.get("role") != "staff":
                        sub["role"] = "public"
                    sub["zone"] = zone
                    self.log(f"[banshee] {who} joined crowd updates at {zone}")
                await self._next_public_step(cid, sub) if sub.get("role") == "public" else \
                    await self._send(cid, self._staff_welcome(who))
            elif low.startswith("/staff"):
                code = text[6:].strip()
                if self.staff_code and code != self.staff_code:
                    await self._send(cid, "That isn't the staff code. Ask the control room for it, then send "
                                          "<code>/staff CODE</code>.")
                else:
                    sub["role"] = "staff"
                    self.log(f"[banshee] {who} joined as staff (chat id {cid})")
                    await self._send(cid, self._staff_welcome(who))
            elif low.startswith("/where"):
                sub["role"] = sub.get("role") or "public"
                await self._send(cid, "Where are you now?", self._zone_buttons())
            elif low.startswith("/lang"):
                await self._send(cid, "Which language do you want updates in?", LANG_BUTTONS)
            elif sub.get("role") == "staff":
                await self._send(cid, self._staff_welcome(who))
            elif sub.get("role") == "public":
                await self._next_public_step(cid, sub)
            else:
                await self._send(cid, f"Hi {html.escape(who)}! This is Poltergeist, the crowd-safety bot for "
                                      f"{html.escape(self.venue.get('venue', 'this venue'))}. Who are you?", ROLE_BUTTONS)
            self._save_subscribers()
            self._refresh_status()
            return
        cq = up.get("callback_query")
        if not cq:
            return
        if await self._handle_setup_button(cq):
            return
        try:
            action, inc_id, post_id = cq.get("data", "").split(":")
            inc_id = int(inc_id)
        except ValueError:
            return
        reply = "Done."
        frm = cq.get("from", {})
        who = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or frm.get("username")
        chat_id = (cq.get("message") or {}).get("chat", {}).get("id")
        if action == "acc":
            inc = self.engine.incidents.get(inc_id)
            if inc and inc["status"] == "dispatching" and inc["current_post"] == post_id:
                ok, reply = await self.accept(inc_id, post_id, via="telegram", who=who, by_chat=chat_id)
            elif inc and inc["status"] == "accepted":
                reply = f"Already taken by {self.posts.get(inc['accepted_by'], {}).get('name', 'another post')}."
            else:
                reply = "This alert has moved on."
        elif action == "dec":
            reply = "Sent to the next post." if await self.decline(inc_id, post_id) else "This alert has moved on."
        elif action == "res":
            reply = "Marked resolved. Thank you." if await self.resolve(inc_id, via="telegram") else "Already closed."
        try:
            await self._call("answerCallbackQuery", data={"callback_query_id": cq["id"], "text": reply})
        except Exception:
            pass
