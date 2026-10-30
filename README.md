# Telegram Mirror Bot

A Telegram bot that takes a link or a file, downloads it if needed, and re-uploads it to **Gofile**.

It handles direct file links, web pages where the file is hidden behind a "Download" button, video sites supported by yt-dlp, magnet links, `.torrent` files, and files sent directly in Telegram.

```
Telegram link / .torrent / file
          │
          ▼
   Resolver (HTML buttons, meta refresh, data-url, JSON-LD, Google Drive, GitHub releases…)
          │
          ▼
   aria2c (HTTP / torrent)  or  yt-dlp (video sites)
          │
          ▼
   Gofile guest/account upload
          │
          ▼
   Public Gofile link sent back in Telegram
```

## Features

- Direct HTTP/HTTPS downloads with aria2c multi-connection downloading.
- "Download button" pages: scans links, buttons, `data-*` URLs, meta refresh, embedded video/audio, JSON-LD and common JavaScript redirects.
- Special handling for Google Drive single files, Dropbox, Pixeldrain links, Gofile share links, MediaFire-style buttons, and GitHub releases.
- Video sites through yt-dlp (YouTube, Vimeo, TikTok, X/Twitter, Reddit, etc.).
- Magnet links and `.torrent` files through aria2c (DHT enabled, seeding disabled).
- Files sent directly in Telegram — documents, photos, video, audio, voice notes, animations and stickers — are uploaded to Gofile. Telegram's cloud Bot API limits bot downloads to 20 MB per attachment.
- Gofile share links (`gofile.io/d/...`) resolve through Gofile's API using a temporary guest token. Folder links are supported; the bot downloads the largest file or every file in `MULTI_FILE_MODE=zip`.
- Gofile guest uploads work with no account. An optional `GOFILE_TOKEN` uses your account.
- Progress bars, cancel buttons, per-user job limits, size limits and timeouts.
- Private mode with `ALLOWED_USER_IDS`.
- Runs on GitHub Actions with automatic run hand-off, Docker, or any small VPS.

## Quick start on GitHub Actions

> GitHub Actions is not a true 24/7 host. Read [Actions limitations](#github-actions-limitations) before choosing it.

1. **Create the repository** and push this folder to it.

   ```bash
   git init
   git add .
   git commit -m "Initial mirror bot"
   git branch -M main
   git remote add origin https://github.com/YOUR_NAME/mirror-bot.git
   git push -u origin main
   ```

   A **public** repository is strongly recommended. Standard GitHub-hosted runners are free for public repositories; private repositories only get 2,000 minutes/month, which is not enough to run a bot all month.

2. **Create the Telegram bot.** Talk to [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token.

3. **Get your Telegram user ID.** Message [@userinfobot](https://t.me/userinfobot), or leave `ALLOWED_USER_IDS` empty for the first run, start the bot and send `/id`.

4. **Add repository secrets** (Settings → Secrets and variables → Actions → Secrets):

   | Secret | Required | Purpose |
   |---|---|---|
   | `TELEGRAM_BOT_TOKEN` | yes | BotFather token |
   | `ALLOWED_USER_IDS` | recommended | Comma-separated IDs, e.g. `123456789,987654321` |
   | `GOFILE_TOKEN` | optional | Uses your Gofile account for uploads and share-link downloads instead of guest tokens |

   Optional repository **variables** (Settings → Secrets and variables → Actions → Variables): `MAX_FILE_SIZE` (`8G`), `MAX_CONCURRENT_JOBS` (`1`), `JOB_TIMEOUT` (`7200`), `MULTI_FILE_MODE` (`largest`), `YTDLP_FORMAT` (`bv*+ba/b`), `GOFILE_WT_SALT` (only needed if Gofile rotates its website-token salt), `AUTO_RESTART` (`true`; set to `false` to stop the automatic hand-off).

5. **Enable Actions and run it.** Open the **Actions** tab → **Telegram Mirror Bot** → **Run workflow**. Leave `run_seconds` blank for the normal 4h45m run, or set a small value such as `60` for a short test. Wait for the job to start, then send your bot a link.

6. **Send a link or a file.** The bot downloads the file if needed, uploads it to Gofile, and replies with the public link.

### GitHub Actions limitations

- **One job is capped at 6 hours.** Each run polls for 4h45m (`BOT_RUN_SECONDS=17100`), then queues the next run before exiting. The 5-hour schedule is a fallback. A run that crashes without queuing is restarted by the keep-alive workflow within about 30 minutes. Messages sent while no run is active are delivered when the next run starts because Telegram keeps updates for 24 hours.
- **Only one poller at a time.** Telegram allows a single `getUpdates` consumer per bot token, so the workflow's concurrency group serializes runs. A single run can still process several uploads concurrently via `MAX_CONCURRENT_JOBS`.
- **Cron is not exact.** GitHub can delay scheduled runs by several minutes.
- **Private repos run out of minutes.** 2,000 minutes/month is roughly 33 hours; this bot needs ~720 hours/month. Use a public repo or a real host.
- **Disk is limited.** The standard runner has about 14 GB of SSD. The default `MAX_FILE_SIZE=8G` and `MAX_CONCURRENT_JOBS=1` are chosen to fit. Lower them if you mirror many files.
- **Scheduled workflows can be disabled after 60 days without repository activity.** This workflow commits `.state/telegram.json` after each run, which keeps the repository active and also resumes the Telegram update offset.
- **State commits.** The workflow pushes a tiny state commit (`chore: update bot state [skip ci]`) when the Telegram offset changes, plus a weekly heartbeat so GitHub does not disable the schedule after 60 idle days. If `main` is protected, allow GitHub Actions to push, or change the workflow to use a cache instead.
- **Actions logs are public in a public repo.** Do not print tokens; the bot does not log them.
- **No inbound webhook.** The bot uses long polling, so it only handles messages while a workflow run is active. The keep-alive workflow starts runs; it does not receive messages itself.
- **Datacenter IPs get blocked.** YouTube and other video sites often ask for a sign-in from GitHub's IPs, and BitTorrent traffic may be throttled. For heavy video/torrent use, run the Docker image on a VPS.
- **End-of-run grace period.** The bot stops accepting new jobs when its run is ending and waits up to 20 minutes for active jobs to finish.

The automatic hand-off makes Actions effectively continuous, but it is still best-effort: GitHub can delay or disable schedules, and a run that fails before the hand-off waits for the keep-alive check. For guaranteed 24/7 operation, deploy the included Docker image to a VPS, Render, Railway, Fly.io, or similar.

## Local setup

Requirements: Python 3.10+ (3.12 recommended), [aria2](https://aria2.github.io/) and ffmpeg. Without aria2, direct HTTP downloads still work through Python, but torrents/magnets do not.

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
# edit .env and set TELEGRAM_BOT_TOKEN
python -m bot
```

Install the system tools:

```powershell
# Windows
winget install aria2.aria2
winget install Gyan.FFmpeg
```

```bash
# Debian/Ubuntu
sudo apt update && sudo apt install -y aria2 ffmpeg
# macOS
brew install aria2 ffmpeg
```

## Docker

```bash
docker build -t mirror-bot .
docker run -d --name mirror-bot \
  --env-file .env \
  -v mirror-data:/data \
  --restart unless-stopped \
  mirror-bot
```

The image includes aria2 and ffmpeg. Downloads and state live in `/data`.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | BotFather token (required) |
| `ALLOWED_USER_IDS` | empty | Comma-separated Telegram user IDs. Empty means anyone can use the bot. |
| `GOFILE_TOKEN` | empty | Optional Gofile account token. Empty = guest uploads. |
| `MAX_FILE_SIZE` | `8G` | Maximum downloaded file size |
| `MAX_CONCURRENT_JOBS` | `2` | Parallel jobs (`1` in the Actions workflow) |
| `JOB_TIMEOUT` | `7200` | Per-job timeout in seconds |
| `BOT_RUN_SECONDS` | `0` | Stop after N seconds; `0` = run until stopped |
| `MULTI_FILE_MODE` | `largest` | For torrents/multi-file downloads: upload the largest file, or `zip` everything |
| `YTDLP_FORMAT` | `bv*+ba/b` | yt-dlp format selector |
| `COOKIES_FILE` | empty | Optional Netscape cookies file for restricted video sites |
| `DOWNLOAD_DIR` | `downloads` | Temporary download directory |
| `STATE_FILE` | `.state/telegram.json` | Telegram offset state |
| `ARIA2C_PATH` | `aria2c` | Path to the aria2c binary |
| `ALLOW_PRIVATE_URLS` | `0` | **Dangerous.** Allows localhost/private IP links. Keep off in production. |
| `LOG_LEVEL` | `INFO` | Python log level |

## Commands

| Command | Description |
|---|---|
| `/start`, `/help` | Usage |
| `/gf <link>` | Upload a link, or reply to a file with `/gf` |
| `/mirror <link>` | Same as `/gf` |
| `/status` | Active jobs |
| `/cancel` | Cancel your active jobs |
| `/id` | Show your Telegram user/chat ID |

You can also send a link, a magnet link, a `.torrent` file, or any file directly. Files sent in Telegram are uploaded to Gofile; cloud bots can download attachments up to 20 MB. To mirror a file you already sent, reply to it with `/gf` or `/mirror`.

## Supported links

| Source | Support |
|---|---|
| Direct file URLs (any type: `.zip`, `.iso`, `.apk`, `.pdf`, `.mp3`, `.mp4`, …) | ✅ aria2c |
| Files sent to the bot in Telegram (documents, photos, video, audio, voice, stickers) | ✅ direct upload, ≤20 MB |
| Pages with a Download button | ✅ heuristic resolver |
| GitHub releases | ✅ GitHub API picks the first asset |
| Google Drive single files | ✅ converted to a direct download URL |
| Dropbox share links | ✅ `dl=1` |
| Pixeldrain file links (download only) | ✅ direct API URL |
| Gofile share links (`gofile.io/d/...`) | ✅ Gofile API, guest token, folders supported |
| MediaFire / SourceForge / similar pages | ✅ when the download link is in the HTML |
| YouTube, Vimeo, TikTok, X, Reddit, etc. | ✅ yt-dlp |
| Magnet links and `.torrent` files | ✅ aria2c |
| MEGA links | ❌ encrypted |
| Google Drive folders | ❌ not supported |
| Password-protected Gofile folders | ❌ not supported |
| Login/CAPTCHA/DRM-protected pages | ❌ not supported |

The resolver is intentionally best-effort. Sites that build download links in JavaScript after a login or CAPTCHA cannot be mirrored without site-specific code.

## Troubleshooting

**"That page does not expose a direct file"** — the page needs a login, CAPTCHA, or JavaScript that the resolver cannot execute. Try the original file URL.

**A file sent in Telegram is rejected as too large** — Telegram's cloud Bot API only lets bots download attachments up to 20 MB. Send a direct link to the file instead; the bot can then mirror files up to `MAX_FILE_SIZE`.

**Gofile says the token is rejected** — remove `GOFILE_TOKEN` to use guest uploads, or create a fresh token from your Gofile profile.

**A Gofile share link fails** — the bot reads share links with a temporary guest token, so no account is needed. Password-protected folders are not supported. If Gofile changes its website-token salt, set `GOFILE_WT_SALT` to the current value from `https://gofile.io/js/wt.obf.js`; the default is `12af056dacea0b`.

**Torrents do not start** — install aria2c (`aria2c --version`) and make sure the workflow/Docker image includes it. A magnet with no seeders stops after 5 minutes (`--bt-stop-timeout=300`).

**Video download fails** — update yt-dlp (`pip install -U yt-dlp`). Age/region-locked sites may need `COOKIES_FILE`. GitHub Actions IPs are frequently blocked by YouTube; a VPS is more reliable for video sites.

**Torrents do not connect** — many datacenter networks block BitTorrent. Try a VPS, or use a direct link.

**The Actions run stops after 4h45m** — that is expected. The run queues its successor before exiting, so the bot normally comes back within a minute or two. The keep-alive workflow also checks every 30 minutes.

**The bot is offline for more than 30 minutes** — open the Actions tab and check for failed runs. If the `Queue next run` step failed, the keep-alive workflow should start a new run within 30 minutes; you can also run the workflow manually. To stop automatic restarts, set the repository variable `AUTO_RESTART` to `false` and disable the workflow.

**The state push fails** — the repository's default branch is probably protected. Allow GitHub Actions to push, or replace the state step with `actions/cache` restore/save.

**Disk full** — lower `MAX_FILE_SIZE` and keep `MAX_CONCURRENT_JOBS=1`. Each job deletes its temporary files when it finishes.

## Security and legal notes

- Set `ALLOWED_USER_IDS`. An open bot lets anyone use your runner, API keys and bandwidth.
- The bot blocks localhost/private-network URLs to reduce SSRF risk, but do not expose it to untrusted users.
- Never commit `.env` or tokens. The GitHub Actions secrets are the right place.
- Only mirror content you have the right to copy. Respect the terms of service of the source site and Gofile.
- Gofile guest uploads create a new public folder per upload. Use `GOFILE_TOKEN` if you want uploads tied to your account.

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

The tests cover URL/filename handling, the HTML download-button resolver, Gofile share-link resolution and the Gofile uploader with mocked HTTP responses, command parsing, and the downloader's header and file-collection logic.

## Project layout

```
bot.py                     Telegram handlers and polling lifecycle
mirror/
  config.py                Environment settings
  resolver.py              Page → direct link
  downloader.py            aria2c / yt-dlp / requests engines
  jobs.py                  Queue, progress state, download → upload pipeline
  state.py                 Telegram update offset persistence
  uploaders/
    gofile.py              Gofile upload fleet API
.github/workflows/
  bot.yml                  Scheduled bot run on GitHub Actions
  tests.yml                Unit tests
Dockerfile                 aria2 + ffmpeg + bot
```

## License

MIT
