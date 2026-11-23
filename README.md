# DraftsFun X Relay Bot

Forwards new posts from https://x.com/DraftsFun to:

- **Telegram:** the full post text, images, videos and GIFs, plus a "View on X" link.
- **Discord:** a short preview of the text and the x.com link. Discord shows the embed.

Replies and reposts are skipped.

## What it costs

X's API is pay-per-use. You're charged for each post returned, not for each request.

| Item | Price | How the bot uses it |
|---|---|---|
| Post read (standard) | $0.005 | Only new posts are returned (`since_id`) |
| Post read (Owned Read) | $0.001 | When the app belongs to @DraftsFun and uses its OAuth 1.0a keys |
| User lookup | $0.010 | Once, ever. The ID is saved |
| Same post re-read in one UTC day | free | X counts each post once per day |

**Example:** 5 posts a day is about 150 posts a month. That's **~$0.75/month** on standard reads, or **~$0.15/month** with Owned Reads. A check with no new posts returns nothing, so checking every 5 minutes doesn't add much.

Built-in safeguards:
- One API call per check, and no extra user data is requested.
- Replies and reposts are filtered out by X, so you aren't billed for them.
- `MAX_POSTS_READ_PER_MONTH` (default 300) stops the bot if anything goes wrong.
- After an error the bot waits 15+ minutes before retrying, so it doesn't use up calls.
- **Also set a spend limit in the X Developer Console.** This is your real safety net.

## 1. Get the X API keys

1. Go to https://console.x.com and sign in. **Use the @DraftsFun account if you can**, so you get the cheaper Owned Reads.
2. Create a project and an app.
3. **Billing:** buy a small amount of credit, e.g. $5. **Set a monthly spend limit** of around $2 to $5. Turn auto-recharge off.
4. **User authentication settings:** set app permissions to **Read** only. The bot never needs to post on X, so leaked keys can't be used to post either.
5. **Keys and tokens:**
   - **Option A (cheapest):** copy the API Key and Secret. Then generate the Access Token and Secret for @DraftsFun. Put all four in `.env`.
   - **Option B (simplest):** copy the **Bearer Token** into `X_BEARER_TOKEN`.

## 2. Telegram

1. Message @BotFather and send `/newbot`. Copy the token into `TELEGRAM_BOT_TOKEN`.
2. Add the bot to your channel or group as an **admin** with "Post messages" permission.
3. `TELEGRAM_CHAT_ID` is `@yourchannelname` for a public channel. For a private chat, use the numeric `-100…` ID.

## 3. Discord

Go to Channel Settings, then **Integrations**, then **Webhooks**. Create a **New Webhook**, name it (e.g. "DraftsFun on X"), and set an avatar. Click **Copy Webhook URL** and put it in `DISCORD_WEBHOOK_URL`.

A webhook can only post to that one channel. No bot token or gateway connection is needed.

## 4. Run

```bash
cd x-relay-bot
pip install -r requirements.txt
copy .env.example .env      # then fill in .env
python bot.py --dry-run     # fetch and print, posts nothing (uses ~5 post reads)
python bot.py               # check once
python bot.py --loop        # keep running, check every POLL_SECONDS
```

On the first real run, the bot forwards the newest `FIRST_RUN_POSTS` (default 1) and then remembers where it stopped in `state.json`. Don't delete that file. If you do, the next run counts as a first run again.

### Keep it running

- **Windows PC:** use Task Scheduler. Run `python C:\path\to\x-relay-bot\bot.py` every 5 to 10 minutes. This only works while the PC is on.
- **Small Linux VPS:** run it with a cron entry:
  ```
  */5 * * * * cd /opt/x-relay-bot && /usr/bin/python3 bot.py >> relay.log 2>&1
  ```
  Or run `python3 bot.py --loop` under systemd, pm2, or screen.

## Security

- `.env` and `state.json` are git-ignored. Never paste tokens into chats or screenshots.
- Error messages are scrubbed so Telegram and Discord tokens don't end up in logs.
- Discord messages block `@everyone`, `@here` and user pings. Only the optional `DISCORD_PING_ROLE_ID` can ping.
- If a key leaks, regenerate it in the X console, BotFather (`/revoke`), or Discord (delete the webhook).

## Test formatting without spending credits

```bash
python bot.py --sample sample_response.json
```

## Hosting free on Render (with admin panel)

Render's free web service sleeps when idle and has no permanent disk, so two extra pieces are needed: a free Postgres database to remember state, and a pinger to keep the service awake. Free-tier terms change, so check each provider's current limits.

1. **Database:** create a free Postgres database (for example at neon.tech) and copy its connection string. It is the `DATABASE_URL`.
2. **Push this folder to a private GitHub repo.** `.env` is git-ignored, so keys are not uploaded.
3. **Render:** New > Blueprint (uses `render.yaml`), or New > Web Service with:
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app --workers 1 --threads 4 --timeout 120` (keep it at exactly one worker)
   - Health check path: `/health`
4. **Environment variables:** add everything from your `.env` in the Render dashboard, plus `DATABASE_URL` and `ADMIN_PASSWORD` (a long random password).
   Set `FIRST_RUN_POSTS=0` so the first start doesn't repost an old post.
5. **Pinger:** create a free monitor (UptimeRobot, cron-job.org, etc.) that requests `https://<your-service>.onrender.com/health` every 5 minutes. That URL does not call X or touch the database, so pinging costs nothing.
6. **Admin panel:** open `https://<your-service>.onrender.com/admin`, log in as `admin` with your `ADMIN_PASSWORD`. You can pause or resume, change the check interval, Discord preview length, monthly read cap and ping role, turn Telegram or Discord on or off, send a test message, and see usage and recent activity.

Run the same app locally with `python app.py` (uses a local `data.db` unless `DATABASE_URL` is set).

If Render restarts the service, the database remembers the last post, so nothing is reposted.
