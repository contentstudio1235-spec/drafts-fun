"""
Web wrapper for hosting (Render etc.): runs the relay loop in a background thread and serves

  GET  /health   tiny public status. Point your uptime pinger here. No X calls, no DB access.
  GET  /admin    password-protected control panel (HTTP Basic auth, user "admin").

Start with:  gunicorn app:app --workers 1 --threads 4 --timeout 120
(exactly ONE worker, otherwise you'd run several relay loops).
Local test:  python app.py
"""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import os
import threading
import time
from datetime import datetime, timezone

from flask import Flask, Response, abort, jsonify, redirect, request

import bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("app")

bot.load_env(bot.ROOT / ".env")
CFG = bot.Config.from_env()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
STATUS: dict = {"state": "starting", "started": int(time.time())}
STOP = threading.Event()
_started = False
_lock = threading.Lock()

app = Flask(__name__)


# ------------------------------------------------------------------ worker

def start_worker() -> None:
    global _started
    with _lock:
        if _started or os.environ.get("RUN_WORKER", "1") == "0":
            return
        CFG.validate()
        bot.init_store()
        _started = True
        threading.Thread(target=bot.run_forever, args=(CFG, STATUS, STOP), daemon=True, name="relay").start()
        log.info("Relay worker started")


# ------------------------------------------------------------------ auth

def _csrf() -> str:
    return hmac.new(ADMIN_PASSWORD.encode(), b"csrf-v1", hashlib.sha256).hexdigest()


def require_admin() -> None:
    if not ADMIN_PASSWORD:
        abort(Response("Admin panel disabled: set ADMIN_PASSWORD.", 503))
    a = request.authorization
    ok = a and hmac.compare_digest(a.username or "", "admin") and hmac.compare_digest(a.password or "", ADMIN_PASSWORD)
    if not ok:
        abort(Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="relay admin"'}))
    if request.method == "POST" and not hmac.compare_digest(request.form.get("csrf", ""), _csrf()):
        abort(Response("Bad form token, reload the page.", 400))


# ------------------------------------------------------------------ routes

@app.get("/")
@app.get("/health")
def health():
    # In-memory only, so pinging this every few minutes never touches X or the database.
    return jsonify(ok=True, state=STATUS.get("state"), last_check=STATUS.get("last_check"))


def _fmt(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if ts else "never"


@app.get("/admin")
def admin():
    require_admin()
    st = bot.init_store()
    settings = st.get_json("settings", {})
    eff = bot.apply_settings(CFG, settings)
    state = st.get_json("state", {})
    usage = state.get("usage", {})
    reads = usage.get("posts_read", 0) if usage.get("month") == bot.month_key() else 0
    rate = 0.001 if CFG.use_oauth1 else 0.005
    e = html.escape
    logs = "".join(
        f"<tr><td>{_fmt(l['ts'])}</td><td>{e(str(l['post_id']))}</td><td>{e(str(l['platform']))}</td>"
        f"<td class={'bad' if l['status'] == 'failed' else 'ok'}>{e(str(l['status']))}</td><td>{e(str(l['detail']))}</td></tr>"
        for l in st.recent_logs(30)
    ) or "<tr><td colspan=5>Nothing sent yet.</td></tr>"
    paused = bool(settings.get("paused"))
    chk = lambda v: "checked" if v else ""
    page = f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Relay admin</title>
<style>
body{{font:15px system-ui;background:#0f1115;color:#e6e6e6;max-width:880px;margin:24px auto;padding:0 16px}}
h1{{font-size:20px}} h2{{font-size:15px;margin:28px 0 8px;color:#9aa4b2;text-transform:uppercase;letter-spacing:.05em}}
.card{{background:#181b22;border:1px solid #262a33;border-radius:10px;padding:14px 16px}}
table{{width:100%;border-collapse:collapse;font-size:13px}} td,th{{padding:6px 8px;border-bottom:1px solid #262a33;text-align:left}}
label{{display:block;margin:10px 0 4px;color:#9aa4b2}} input[type=number]{{background:#0f1115;color:#fff;border:1px solid #333;border-radius:6px;padding:6px;width:120px}}
button{{background:#3b82f6;color:#fff;border:0;border-radius:6px;padding:8px 14px;cursor:pointer;margin-top:12px}}
button.warn{{background:#d97706}} button.grey{{background:#374151}}
.ok{{color:#4ade80}} .bad{{color:#f87171}} .row{{display:flex;gap:10px;flex-wrap:wrap}} form{{margin:0}}
</style>
<h1>DraftsFun X relay</h1>
<div class=card>
 State: <b>{e(str(STATUS.get('state')))}</b>{' (PAUSED)' if paused else ''}<br>
 Last check: {_fmt(STATUS.get('last_check'))}<br>
 Next check: {_fmt(STATUS.get('next_check'))}<br>
 Last error: <span class=bad>{e(str(STATUS.get('last_error') or '-'))}</span><br>
 X posts read this month: <b>{reads}</b> / {eff.monthly_cap} cap
 (about ${reads * rate:.3f} at ${rate}/post)<br>
 Auth: {'OAuth 1.0a (owned reads if this is your own app)' if CFG.use_oauth1 else 'Bearer token'}
</div>

<h2>Controls</h2>
<div class="row">
<form method=post action=/admin/pause><input type=hidden name=csrf value={_csrf()}>
 <button class="{'' if paused else 'warn'}">{'Resume' if paused else 'Pause'} checking</button></form>
<form method=post action=/admin/test><input type=hidden name=csrf value={_csrf()}>
 <button class=grey>Send test message</button></form>
</div>

<h2>Settings</h2>
<form class=card method=post action=/admin/settings><input type=hidden name=csrf value={_csrf()}>
 <label>Check every (seconds, min 60)</label><input type=number name=poll_seconds value={eff.poll_seconds} min=60 max=86400>
 <label>Discord preview length (characters)</label><input type=number name=preview_chars value={eff.discord_preview_chars} min=40 max=1500>
 <label>Monthly X read cap (0 = block all calls)</label><input type=number name=monthly_cap value={eff.monthly_cap} min=0 max=100000>
 <label><input type=checkbox name=telegram {chk(eff.tg_enabled)}> Post to Telegram</label>
 <label><input type=checkbox name=discord {chk(eff.discord_enabled)}> Post to Discord</label>
 <label><input type=checkbox name=ping_role {chk(settings.get('ping_role') is not False)}> Ping the Discord role</label>
 <button>Save settings</button>
</form>

<h2>Recent activity</h2>
<div class=card><table><tr><th>Time<th>Post<th>Where<th>Result<th>Detail</tr>{logs}</table></div>
<p style="color:#6b7280;font-size:12px">Changes apply from the next check. Tokens and keys are never shown here.</p>
"""
    return Response(page, mimetype="text/html")


def _update_settings(**kw) -> None:
    st = bot.init_store()
    s = st.get_json("settings", {})
    s.update(kw)
    st.set_json("settings", s)


@app.post("/admin/pause")
def pause():
    require_admin()
    _update_settings(paused=not bot.init_store().get_json("settings", {}).get("paused"))
    return redirect("/admin", 303)


@app.post("/admin/settings")
def settings():
    require_admin()
    f = request.form

    def num(k, lo, hi):
        try:
            return min(hi, max(lo, int(f.get(k, ""))))
        except ValueError:
            return None
    upd = dict(
        telegram="telegram" in f, discord="discord" in f, ping_role="ping_role" in f,
    )
    for key, lo, hi in (("poll_seconds", 60, 86400), ("preview_chars", 40, 1500), ("monthly_cap", 0, 100000)):
        v = num(key, lo, hi)
        if v is not None:
            upd[key] = v
    _update_settings(**upd)
    return redirect("/admin", 303)


@app.post("/admin/test")
def test_message():
    require_admin()
    msg = "✅ Test message from the relay admin panel."
    errors = []
    for t in bot.make_targets(bot.apply_settings(CFG, bot.init_store().get_json("settings", {}))):
        try:
            t.send_text(msg)
        except bot.RelayError as e:
            errors.append(f"{t.key}: {e}")
    if errors:
        return Response("Test failed:\n" + "\n".join(errors), 502, mimetype="text/plain")
    return redirect("/admin", 303)


start_worker()   # also runs under gunicorn, which imports this module

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), use_reloader=False)
