#!/usr/bin/env python3
"""
DraftsFun X -> Telegram + Discord relay.

Checks @DraftsFun for new posts and forwards them:
  * Telegram: full text, images, videos and GIFs, plus a "View on X" link.
  * Discord:  a short text preview and the x.com link (via a webhook).

Cost controls (X API is pay-per-use, billed per post returned):
  * One API call per check: GET /2/users/:id/tweets with since_id, so a check with
    no new posts returns nothing.
  * Replies and reposts are filtered out by X, so they are never returned or billed.
  * No author/user expansions (user reads cost 2x a post read).
  * The account's user ID is looked up once and saved.
  * If the app belongs to @DraftsFun and you use its OAuth 1.0a keys, timeline reads
    count as "Owned Reads" (about 5x cheaper).
  * A local monthly cap (MAX_POSTS_READ_PER_MONTH) stops calls if something goes wrong.

Usage:
  python bot.py                 check once, then exit (for cron / Task Scheduler)
  python bot.py --loop          keep running, check every POLL_SECONDS
  python bot.py --dry-run       fetch and print, post nothing, save nothing
  python bot.py --sample f.json format a saved API response offline (no X call)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import sys
import time
import urllib.parse
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import requests

import store as store_mod

ROOT = Path(__file__).resolve().parent
STORE: store_mod.Store | None = None   # set by init_store()
X_API = "https://api.x.com/2"
UA = "draftsfun-relay/1.0"

TG_CAPTION_LIMIT = 1024
TG_MESSAGE_LIMIT = 4096
TG_UPLOAD_LIMIT = 49 * 1024 * 1024   # Bot API upload cap is 50 MB
TG_PHOTO_LIMIT = 10 * 1024 * 1024
DISCORD_LIMIT = 2000

log = logging.getLogger("relay")


# --------------------------------------------------------------------------- config

def load_env(path: Path) -> None:
    """Tiny .env loader so we don't need python-dotenv. Real env vars win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        sys.exit(f"{name} must be a number")


@dataclass
class Config:
    username: str
    user_id: str
    bearer: str
    api_key: str
    api_secret: str
    access_token: str
    access_secret: str
    tg_token: str
    tg_chat: str
    discord_webhook: str
    discord_role_id: str
    discord_preview_chars: int
    poll_seconds: int
    first_run_posts: int
    max_posts_per_run: int
    monthly_cap: int
    tg_enabled: bool = True
    discord_enabled: bool = True

    @property
    def use_oauth1(self) -> bool:
        return all([self.api_key, self.api_secret, self.access_token, self.access_secret])

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls(
            username=env("X_USERNAME", "DraftsFun").lstrip("@"),
            user_id=env("X_USER_ID"),
            bearer=env("X_BEARER_TOKEN"),
            api_key=env("X_API_KEY"),
            api_secret=env("X_API_SECRET"),
            access_token=env("X_ACCESS_TOKEN"),
            access_secret=env("X_ACCESS_TOKEN_SECRET"),
            tg_token=env("TELEGRAM_BOT_TOKEN"),
            tg_chat=env("TELEGRAM_CHAT_ID"),
            discord_webhook=env("DISCORD_WEBHOOK_URL"),
            discord_role_id=env("DISCORD_PING_ROLE_ID"),
            discord_preview_chars=env_int("DISCORD_PREVIEW_CHARS", 220),
            # Floor of 60s: empty checks are cheap, but there is no reason to hammer X.
            poll_seconds=max(60, env_int("POLL_SECONDS", 300)),
            first_run_posts=max(0, env_int("FIRST_RUN_POSTS", 1)),
            max_posts_per_run=max(1, env_int("MAX_POSTS_PER_RUN", 5)),
            monthly_cap=env_int("MAX_POSTS_READ_PER_MONTH", 300),
        )
        return cfg

    def validate(self, need_x: bool = True) -> None:
        if need_x and not (self.bearer or self.use_oauth1):
            sys.exit("Set X_BEARER_TOKEN, or all four OAuth 1.0a keys (see .env.example).")
        if not (self.tg_token and self.tg_chat) and not self.discord_webhook:
            sys.exit("Set Telegram (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID) and/or DISCORD_WEBHOOK_URL.")


# --------------------------------------------------------------------------- state

def init_store() -> store_mod.Store:
    global STORE
    if STORE is None:
        STORE = store_mod.open_store()
    return STORE


def load_state() -> dict:
    try:
        return init_store().get_json("state", {})
    except ValueError:
        log.error("Stored state is corrupt; refusing to run so we don't repost old posts. Fix or delete it.")
        sys.exit(1)


def save_state(state: dict) -> None:
    init_store().set_json("state", state)


def apply_settings(cfg: Config, s: dict) -> Config:
    """Overlay values changed in the admin panel on top of the .env defaults."""
    def num(key, lo, hi, default):
        try:
            return min(hi, max(lo, int(s.get(key, default))))
        except (TypeError, ValueError):
            return default
    return replace(
        cfg,
        poll_seconds=num("poll_seconds", 60, 86400, cfg.poll_seconds),
        discord_preview_chars=num("preview_chars", 40, 1500, cfg.discord_preview_chars),
        monthly_cap=num("monthly_cap", 0, 100000, cfg.monthly_cap),
        discord_role_id="" if s.get("ping_role") is False else cfg.discord_role_id,
        tg_enabled=s.get("telegram", True) is not False,
        discord_enabled=s.get("discord", True) is not False,
    )


def month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def count_reads(state: dict, n: int) -> None:
    usage = state.setdefault("usage", {})
    if usage.get("month") != month_key():
        usage.clear()
        usage["month"] = month_key()
        usage["posts_read"] = 0
        usage["user_reads"] = 0
    usage["posts_read"] = usage.get("posts_read", 0) + n


# --------------------------------------------------------------------------- errors

class RelayError(Exception):
    pass


class RateLimited(RelayError):
    def __init__(self, wait: int):
        super().__init__(f"rate limited, wait {wait}s")
        self.wait = wait


def scrub(msg: str, *secrets_: str) -> str:
    """Remove tokens from error text (requests errors include the full URL)."""
    for s in secrets_:
        if s:
            msg = msg.replace(s, "***")
    return msg


# --------------------------------------------------------------------------- X API

def _pct(s: str) -> str:
    return urllib.parse.quote(str(s), safe="~")


class XClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA

    def _oauth1_header(self, method: str, url: str, params: dict) -> str:
        c = self.cfg
        oauth = {
            "oauth_consumer_key": c.api_key,
            "oauth_nonce": secrets.token_hex(16),
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_token": c.access_token,
            "oauth_version": "1.0",
        }
        all_params = {**params, **oauth}
        param_str = "&".join(f"{_pct(k)}={_pct(v)}" for k, v in sorted(all_params.items()))
        base = "&".join([method.upper(), _pct(url), _pct(param_str)])
        key = f"{_pct(c.api_secret)}&{_pct(c.access_secret)}"
        sig = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
        oauth["oauth_signature"] = sig
        return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))

    def get(self, path: str, params: dict) -> dict:
        url = X_API + path
        if self.cfg.use_oauth1:
            auth = self._oauth1_header("GET", url, params)
        else:
            auth = f"Bearer {self.cfg.bearer}"
        try:
            r = self.s.get(url, params=params, headers={"Authorization": auth}, timeout=20)
        except requests.RequestException as e:
            raise RelayError(f"X request failed: {type(e).__name__}") from None
        if r.status_code == 429:
            reset = int(r.headers.get("x-rate-limit-reset", time.time() + 900))
            raise RateLimited(max(60, reset - int(time.time())))
        if r.status_code in (401, 403):
            raise RelayError(f"X auth error {r.status_code}: {r.text[:300]} (check keys / credits / spend limit)")
        if r.status_code == 402:
            raise RelayError("X says payment required: out of credits or spend limit reached.")
        if r.status_code >= 400:
            raise RelayError(f"X error {r.status_code}: {r.text[:300]}")
        return r.json()

    def user_id(self, username: str) -> str:
        data = self.get(f"/users/by/username/{username}", {})
        if "data" not in data:
            raise RelayError(f"Could not find @{username}: {data.get('errors')}")
        return data["data"]["id"]

    def new_posts(self, user_id: str, since_id: str | None) -> dict:
        params = {
            # X's minimum is 5. With since_id we allow 10 so a busy hour isn't missed;
            # we're billed only for posts actually returned.
            "max_results": "10" if since_id else "5",
            "exclude": "replies,retweets",
            "tweet.fields": "created_at,entities,note_tweet,attachments",
            "expansions": "attachments.media_keys",
            "media.fields": "type,url,preview_image_url,variants,width,height",
        }
        if since_id:
            params["since_id"] = since_id
        return self.get(f"/users/{user_id}/tweets", params)


# --------------------------------------------------------------------------- post model

@dataclass
class Media:
    kind: str                  # photo | video | animated_gif
    url: str = ""              # photo url
    video_urls: list[str] = field(default_factory=list)  # mp4s, best first
    preview: str = ""


@dataclass
class Post:
    id: str
    text: str
    link: str
    media: list[Media]


_MEDIA_LINK = re.compile(r"/(photo|video)/\d+$")


def build_post(tw: dict, media_map: dict, username: str) -> Post:
    note = tw.get("note_tweet") or {}
    text = note.get("text") or tw.get("text", "")
    entities = note.get("entities") or tw.get("entities") or {}

    # Swap t.co links for real ones; drop the t.co link that just points at attached media.
    for u in entities.get("urls", []):
        tco = u.get("url", "")
        expanded = u.get("unwound_url") or u.get("expanded_url") or tco
        if u.get("media_key") or _MEDIA_LINK.search(expanded):
            text = text.replace(tco, "")
        else:
            text = text.replace(tco, expanded)

    text = html.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text).strip()

    media: list[Media] = []
    for key in (tw.get("attachments") or {}).get("media_keys", []):
        m = media_map.get(key)
        if not m:
            continue
        if m["type"] == "photo" and m.get("url"):
            media.append(Media("photo", url=m["url"]))
        elif m["type"] in ("video", "animated_gif"):
            mp4s = [v for v in m.get("variants", []) if v.get("content_type") == "video/mp4"]
            mp4s.sort(key=lambda v: v.get("bit_rate", 0), reverse=True)
            media.append(Media(m["type"], video_urls=[v["url"] for v in mp4s],
                               preview=m.get("preview_image_url", "")))

    return Post(id=tw["id"], text=text, link=f"https://x.com/{username}/status/{tw['id']}", media=media)


def parse_response(data: dict, username: str) -> list[Post]:
    media_map = {m["media_key"]: m for m in (data.get("includes") or {}).get("media", [])}
    tweets = sorted(data.get("data") or [], key=lambda t: int(t["id"]))  # oldest first
    return [build_post(t, media_map, username) for t in tweets]


# --------------------------------------------------------------------------- downloads

_dl = requests.Session()
_dl.headers["User-Agent"] = UA


def download(url: str, limit: int) -> bytes | None:
    """Download into memory; give up (return None) if it's bigger than `limit`."""
    try:
        with _dl.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            size = int(r.headers.get("content-length") or 0)
            if size > limit:
                return None
            buf = bytearray()
            for chunk in r.iter_content(256 * 1024):
                buf += chunk
                if len(buf) > limit:
                    return None
            return bytes(buf)
    except requests.RequestException as e:
        log.warning("download failed (%s): %s", type(e).__name__, url)
        return None


def photo_bytes(m: Media) -> bytes | None:
    return download(m.url + "?name=large", TG_PHOTO_LIMIT) or download(m.url, TG_PHOTO_LIMIT)


def video_bytes(m: Media) -> bytes | None:
    for url in m.video_urls:          # best quality first, fall back if too big
        data = download(url, TG_UPLOAD_LIMIT)
        if data:
            return data
    return None


# --------------------------------------------------------------------------- Telegram

class Telegram:
    key = "telegram"

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat = chat_id
        self.base = f"https://api.telegram.org/bot{token}/"
        self.s = requests.Session()

    def call(self, method: str, data: dict, files: dict | None = None, _retry: bool = True) -> dict:
        try:
            r = self.s.post(self.base + method, data=data, files=files, timeout=180)
            j = r.json()
        except (requests.RequestException, ValueError) as e:
            raise RelayError(scrub(f"Telegram {method} failed: {type(e).__name__}", self.token)) from None
        if j.get("ok"):
            return j["result"]
        wait = (j.get("parameters") or {}).get("retry_after")
        if wait and _retry:
            time.sleep(min(int(wait), 60) + 1)
            return self.call(method, data, files, _retry=False)
        raise RelayError(scrub(f"Telegram {method}: {j.get('description')}", self.token))

    def send_text(self, html_text: str) -> None:
        self.call("sendMessage", {
            "chat_id": self.chat,
            "text": html_text,
            "parse_mode": "HTML",
            "link_preview_options": json.dumps({"is_disabled": True}),
        })

    def send(self, post: Post) -> None:
        body = html.escape(post.text, quote=False)
        link = f'<a href="{post.link}">View on X</a>'
        full = f"{body}\n\n{link}" if body else link
        visible_len = len(post.text) + len("\n\nView on X")

        # Collect uploadable media
        items: list[tuple[str, bytes]] = []   # (telegram type, bytes)
        missing_video = False
        for m in post.media:
            if m.kind == "photo":
                b = photo_bytes(m)
                if b:
                    items.append(("photo", b))
            else:
                b = video_bytes(m)
                if b:
                    items.append(("animation" if m.kind == "animated_gif" else "video", b))
                else:
                    missing_video = True   # too big for a bot upload
                    if m.preview:
                        pb = download(m.preview, TG_PHOTO_LIMIT)
                        if pb:
                            items.append(("photo", pb))
        if missing_video:
            full += "\n▶️ Video too large for Telegram, watch it on X."
            visible_len += 50

        if not items:
            for chunk in split_html(full, TG_MESSAGE_LIMIT):
                self.send_text(chunk)
            return

        caption_fits = visible_len <= TG_CAPTION_LIMIT
        caption = full if caption_fits else ""

        if len(items) == 1:
            kind, blob = items[0]
            method = {"photo": "sendPhoto", "video": "sendVideo", "animation": "sendAnimation"}[kind]
            data = {"chat_id": self.chat}
            if caption:
                data.update(caption=caption, parse_mode="HTML")
            if kind == "video":
                data["supports_streaming"] = "true"
            self.call(method, data, files={kind: (f"media.{'jpg' if kind == 'photo' else 'mp4'}", blob)})
        else:
            group, files = [], {}
            for i, (kind, blob) in enumerate(items[:10]):
                name = f"f{i}"
                entry = {"type": "photo" if kind == "photo" else "video", "media": f"attach://{name}"}
                if kind != "photo":
                    entry["supports_streaming"] = True
                if i == 0 and caption:
                    entry.update(caption=caption, parse_mode="HTML")
                group.append(entry)
                files[name] = (f"{name}.{'jpg' if kind == 'photo' else 'mp4'}", blob)
            self.call("sendMediaGroup", {"chat_id": self.chat, "media": json.dumps(group)}, files=files)

        if not caption_fits:   # long post: media first, then the full text
            for chunk in split_html(full, TG_MESSAGE_LIMIT):
                self.send_text(chunk)


def split_html(text: str, limit: int) -> list[str]:
    """Split on newlines so we never cut inside an HTML tag or entity."""
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:            # one giant line: hard cut on a space
            cut = line.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            out.append(line[:cut])
            line = line[cut:].lstrip()
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return [c for c in out if c.strip()]


# --------------------------------------------------------------------------- Discord

class Discord:
    key = "discord"

    def __init__(self, webhook: str, role_id: str, preview_chars: int, username: str):
        self.webhook = webhook
        self.role_id = role_id
        self.preview_chars = preview_chars
        self.username = username
        self.s = requests.Session()

    def format(self, post: Post) -> str:
        preview = post.text
        if len(preview) > self.preview_chars:
            preview = preview[: self.preview_chars].rsplit(" ", 1)[0].rstrip(" ,.;:-") + "…"
        # <url> stops Discord embedding links inside the preview, so only the X post embeds.
        preview = re.sub(r"(https?://\S+)", r"<\1>", preview)
        quoted = "\n".join(f"> {line}" if line else ">" for line in preview.splitlines())
        head = f"<@&{self.role_id}> " if self.role_id else ""
        parts = [f"{head}**New post from @{self.username}**"]
        if quoted:
            parts.append(quoted)
        parts.append(post.link)
        return "\n".join(parts)[:DISCORD_LIMIT]

    def send_text(self, content: str) -> None:
        r = self.s.post(self.webhook, json={"content": content, "allowed_mentions": {"parse": []}}, timeout=20)
        if r.status_code >= 400:
            raise RelayError(scrub(f"Discord {r.status_code}: {r.text[:200]}", self.webhook))

    def send(self, post: Post, _retry: bool = True) -> None:
        payload = {
            "content": self.format(post),
            # Never let post text trigger @everyone / @here / user pings.
            "allowed_mentions": {"parse": [], "roles": [self.role_id] if self.role_id else []},
        }
        try:
            r = self.s.post(self.webhook, params={"wait": "true"}, json=payload, timeout=20)
        except requests.RequestException as e:
            raise RelayError(f"Discord webhook failed: {type(e).__name__}") from None
        if r.status_code == 429 and _retry:
            wait = float((r.json() or {}).get("retry_after", 5))
            time.sleep(min(wait, 60) + 1)
            return self.send(post, _retry=False)
        if r.status_code >= 400:
            raise RelayError(scrub(f"Discord {r.status_code}: {r.text[:200]}", self.webhook))


# --------------------------------------------------------------------------- main flow

def make_targets(cfg: Config) -> list:
    targets = []
    if cfg.tg_token and cfg.tg_chat and cfg.tg_enabled:
        targets.append(Telegram(cfg.tg_token, cfg.tg_chat))
    if cfg.discord_webhook and cfg.discord_enabled:
        targets.append(Discord(cfg.discord_webhook, cfg.discord_role_id, cfg.discord_preview_chars, cfg.username))
    return targets


def run_once(cfg: Config, state: dict, dry_run: bool = False) -> None:
    targets = make_targets(cfg)
    if not targets:
        log.info("No platform enabled; skipping the X call.")
        return
    last = state.setdefault("last_posted", {})

    # A platform that was switched off restarts from "now" when switched back on,
    # instead of backfilling (and paying for) everything it missed.
    for key in [k for k in last if k not in {t.key for t in targets}]:
        del last[key]

    # A newly added platform starts from "now" instead of backfilling old posts.
    known = [int(v) for v in last.values() if v]
    for t in targets:
        if not last.get(t.key) and known:
            last[t.key] = str(max(known))

    usage = state.get("usage", {})
    if usage.get("month") == month_key() and usage.get("posts_read", 0) >= cfg.monthly_cap:
        log.error("Monthly read cap reached (%s posts). Raise MAX_POSTS_READ_PER_MONTH to continue.",
                  cfg.monthly_cap)
        return

    x = XClient(cfg)
    user_id = cfg.user_id or state.get("user_id")
    if not user_id:
        user_id = x.user_id(cfg.username)        # one-time $0.01 lookup
        state["user_id"] = user_id
        state.setdefault("usage", {}).setdefault("user_reads", 0)
        log.info("Saved user id %s for @%s (put X_USER_ID=%s in .env to skip this forever)",
                 user_id, cfg.username, user_id)
        if not dry_run:
            save_state(state)

    pending = [int(last[t.key]) for t in targets if last.get(t.key)]
    since_id = str(min(pending)) if pending else None
    first_run = since_id is None

    data = x.new_posts(user_id, since_id)
    posts = parse_response(data, cfg.username)
    count_reads(state, len(posts))
    log.info("Fetched %d new post(s)%s", len(posts), " [first run]" if first_run else "")

    if first_run:
        newest = posts[-1].id if posts else None
        posts = posts[-cfg.first_run_posts:] if cfg.first_run_posts else []
        if not posts and newest:               # mark everything as seen without posting
            for t in targets:
                last[t.key] = newest

    posts = posts[: cfg.max_posts_per_run]     # rest will be picked up next check

    if dry_run:
        for p in posts:
            print_post(p, cfg)
        return

    failed: set[str] = set()
    for p in posts:
        for t in targets:
            if t.key in failed or int(p.id) <= int(last.get(t.key) or 0):
                continue
            try:
                t.send(p)
                last[t.key] = p.id
                log.info("Posted %s to %s", p.id, t.key)
                init_store().add_log(p.id, t.key, "sent")
            except RelayError as e:
                failed.add(t.key)   # stop this platform for now so order is kept; retried next check
                log.error("%s: %s", t.key, e)
                init_store().add_log(p.id, t.key, "failed", str(e))
            save_state(state)
    save_state(state)


def print_post(p: Post, cfg: Config) -> None:
    print("=" * 60)
    print(f"POST {p.id}  {p.link}")
    print(f"media: {[(m.kind, m.url or (m.video_urls[:1] or [''])[0]) for m in p.media]}")
    print("--- Telegram text ---")
    print(p.text + "\n\nView on X")
    print("--- Discord message ---")
    print(Discord("", cfg.discord_role_id, cfg.discord_preview_chars, cfg.username).format(p))


def _acquire_lease(owner: str, wait: int) -> bool:
    """Stops two copies of the bot (e.g. during a Render redeploy) from checking at once."""
    st = init_store()
    now = time.time()
    cur = st.get_json("lease", None)
    if cur and cur.get("until", 0) > now and cur.get("owner") != owner:
        return False
    st.set_json("lease", {"owner": owner, "until": now + wait * 2 + 120})
    return True


def cycle(cfg: Config, owner: str, status: dict, dry_run: bool = False) -> int:
    """One check. Returns how many seconds to wait before the next one."""
    try:
        settings = init_store().get_json("settings", {})
        eff = apply_settings(cfg, settings)
        wait = eff.poll_seconds
        if settings.get("paused"):
            status.update(state="paused", next_wait=min(wait, 60))
            return min(wait, 60)
        if not dry_run and not _acquire_lease(owner, wait):
            status.update(state="standby (another copy is active)")
            return wait
        state = load_state()
        run_once(eff, state, dry_run=dry_run)
        status.update(state="running", last_check=int(time.time()), last_error="")
        return wait
    except RateLimited as e:
        log.warning("X rate limit hit, sleeping %ss", e.wait)
        status.update(state="rate limited", last_error=str(e))
        return e.wait
    except Exception as e:   # keep the loop alive; back off so errors can't burn API calls
        msg = scrub(f"{type(e).__name__}: {e}", cfg.bearer, cfg.api_secret, cfg.access_secret,
                    cfg.tg_token, cfg.discord_webhook)
        log.error("%s", msg)
        status.update(state="error", last_error=msg[:300], last_check=int(time.time()))
        return max(cfg.poll_seconds, 900)


def run_forever(cfg: Config, status: dict, stop) -> None:
    owner = secrets.token_hex(6)
    while not stop.is_set():
        wait = cycle(cfg, owner, status)
        status["next_check"] = int(time.time()) + wait
        stop.wait(wait)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", action="store_true", help="keep running and check every POLL_SECONDS")
    ap.add_argument("--dry-run", action="store_true", help="fetch and print, don't post or save")
    ap.add_argument("--sample", metavar="JSON", help="format a saved X API response offline")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env(ROOT / ".env")
    cfg = Config.from_env()

    if args.sample:
        data = json.loads(Path(args.sample).read_text(encoding="utf-8"))
        for p in parse_response(data, cfg.username):
            print_post(p, cfg)
        return

    cfg.validate()
    log.info("Auth: %s", "OAuth 1.0a (Owned Reads if this is your own app/account)" if cfg.use_oauth1 else "Bearer token")
    status: dict = {}
    if args.loop and not args.dry_run:
        import threading
        run_forever(cfg, status, threading.Event())
        return
    cycle(cfg, secrets.token_hex(6), status, dry_run=args.dry_run)
    if status.get("state") == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
