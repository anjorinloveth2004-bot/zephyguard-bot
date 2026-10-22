# assistant-bot

A Telegram group assistant (aiogram 3). Each group brings its own AI key (OpenAI-compatible or Gemini), set up in a private chat with the bot. Group features: /ask Q&A with optional live web search and citations, summaries, moderation (blacklist, links, flood, captcha on join, warns), scheduled group lock windows, and a points leaderboard with weekly or monthly resets. Settings, including per-group timezone, live only in the bot's DM. The group side holds no keys and no settings.

## Features at a glance
- BYOK: admins paste an OpenAI-compatible or Gemini key in DM. It is validated with one tiny call, encrypted with AES-256-GCM, and the message is deleted. Only a masked key (sk-…abcd) is ever shown.
- Per-group timezone: set it in DM under the timezone menu (common list or any IANA name). Default UTC. Scheduled lock windows, lock/unlock notices and leaderboard resets all follow it.
- Opt-in web access: an admin turns on "web access for /ask" in DM. The super admin supplies one shared search key (Tavily, Serper or Brave). Answers then cite numbered sources. With no search key set, web access quietly falls back to no-web.
- Roles: super admins (SUPER_ADMIN_IDS) get dashboard commands (stats, force disconnect, ban group). Group admins manage settings in DM. Members use /ask, /points and /leaderboard.

## Local run
1. Python 3.11+ (3.12 recommended).
2. python -m venv .venv && source .venv/bin/activate
3. pip install -r requirements.txt
4. cp .env.example .env and fill in BOT_TOKEN, BOT_USERNAME, ENCRYPTION_KEY. For a no-server test, set DATABASE_URL=sqlite+aiosqlite:///./data/bot.db
5. python -m app.main
6. Run tests: pytest -q tests

Get a bot token from @BotFather. Turn off privacy mode (/setprivacy, Disable) if you want the bot to read ordinary group messages for points, moderation and summaries.

## Environment variables
| Variable | Required | Purpose |
|---|---|---|
| BOT_TOKEN | yes | Token from @BotFather. |
| BOT_USERNAME | yes | Bot username without @. Used for mention detection. |
| ENCRYPTION_KEY | yes | Encrypts stored group API keys. Generate with: python -c "import secrets; print(secrets.token_urlsafe(32))". Changing it after keys are stored makes them unreadable. |
| DATABASE_URL | yes (prod) | postgresql://user:pass@host:5432/db?sslmode=require. Plain URLs are normalized to asyncpg. Use sqlite+aiosqlite:///./data/bot.db for local dev. |
| SUPER_ADMIN_IDS | no | Comma-separated Telegram user IDs with dashboard access. |
| TAVILY_API_KEY | no | Shared web search key (priority 1). |
| SERPER_API_KEY | no | Shared web search key (priority 2). |
| BRAVE_API_KEY | no | Shared web search key (priority 3). |
| POOL_PROVIDER | no | openai or gemini. Enables the operator-paid free tier for groups without a key. Empty disables it. |
| POOL_API_KEY | no | Key for the pooled free tier. Keep it in your host's secret store. |
| POOL_MODEL | no | Model for the pool. |
| POOL_DAILY_CALLS | no | Total pool calls per UTC day (default 500). |
| POOL_GROUP_DAILY_CALLS | no | Pool calls per group per UTC day (default 30). |
| DEFAULT_OPENAI_MODEL | no | Default gpt-4o-mini. |
| DEFAULT_GEMINI_MODEL | no | Default gemini-2.5-flash. |
| WEBHOOK_BASE_URL | no | Public https URL. Empty means long polling. |
| WEBHOOK_SECRET | no | Secret token checked on webhook calls. Set it whenever WEBHOOK_BASE_URL is set. |
| PORT | no | HTTP port (default 8080). Hosts usually inject it. |
| LOG_LEVEL | no | INFO by default. |

## Deploy on Render
render.yaml is included (Docker web service, /healthz health check). Use the Starter plan. Free web services sleep when idle, which means missed group messages and late lock notices.
1. Push the repo to GitHub. In Render choose New > Blueprint and select it.
2. Fill in BOT_TOKEN, BOT_USERNAME, DATABASE_URL, SUPER_ADMIN_IDS. ENCRYPTION_KEY and WEBHOOK_SECRET are generated.
3. After the first deploy, set WEBHOOK_BASE_URL to https://<service>.onrender.com and redeploy. The bot switches to webhook mode.

## Deploy on Railway
railway.json builds the Dockerfile and sets restart-on-failure and the /healthz check.
1. New Project > Deploy from GitHub repo.
2. Add a Postgres plugin, or point DATABASE_URL at an external Postgres. Railway injects DATABASE_URL automatically when the plugin is in the same project.
3. Set BOT_TOKEN, BOT_USERNAME, ENCRYPTION_KEY, SUPER_ADMIN_IDS. Railway generates a public domain. Set WEBHOOK_BASE_URL to it and WEBHOOK_SECRET to a random string. Or leave WEBHOOK_BASE_URL empty and the bot runs in polling mode on a worker-style service (no HTTP needed, but /healthz will not respond, so disable the healthcheck).

## Deploy on Koyeb
koyeb.yaml is an informational spec. Koyeb also accepts the same settings in its UI or CLI.
1. Create a Web Service from the GitHub repo, builder Dockerfile, port 8080, health check /healthz.
2. Add the secrets listed in koyeb.yaml as secrets (BOT_TOKEN, ENCRYPTION_KEY, DATABASE_URL, WEBHOOK_SECRET, and optional search keys). Set BOT_USERNAME as a plain value.
3. After the first deploy, set WEBHOOK_BASE_URL to https://<app>.koyeb.app and redeploy.

## VPS (any Docker host)
1. Install Docker and Docker Compose. Clone the repo, cp .env.example .env, and fill it in.
2. Use an external Postgres (Neon, Supabase, RDS) or run your own. Put its URL in DATABASE_URL.
3. docker compose up -d --build. Logs: docker compose logs -f bot.
4. For webhooks, put Caddy or nginx in front for TLS and proxy to 127.0.0.1:8080, then set WEBHOOK_BASE_URL to your https domain. Without a domain, leave WEBHOOK_BASE_URL empty and the bot polls.
5. Upgrade: git pull && docker compose up -d --build.

## Notes
- Never put real keys in code or commit .env. .gitignore and .dockerignore already exclude it.
- Keep ENCRYPTION_KEY stable. Losing or changing it means every stored group key has to be re-entered.
- Web search results and AI replies are provided as-is. Check the cited sources for anything important.
