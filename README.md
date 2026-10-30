# Telegram Mirror Bot

A Telegram bot that takes a link, downloads the file, and re-uploads it to **Gofile**.

It handles direct file links, web pages where the file is hidden behind a "Download" button, video sites supported by yt-dlp, magnet links, and `.torrent` files.

```
Telegram link / .torrent
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
- Special handling for Google Drive single files, Dropbox, Pixeldrain links, MediaFire-style buttons, and GitHub releases.
- Video sites through yt-dlp (YouTube, Vimeo, TikTok, X/Twitter, Reddit, etc.).
- Magnet links and `.torrent` files through aria2c (DHT enabled, seeding disabled).
- Gofile guest uploads work with no account. An optional `GOFILE_TOKEN` uses your account.
- Progress bars, cancel buttons, per-user job limits, size limits and timeouts.
- Private mode with `ALLOWED_USER_IDS`.
- Runs on GitHub Actions, Docker, or any small VPS.

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
   | `GOFILE_TOKEN` | optional | Uses your Gofile account instead of guest uploads |

   Optional repository **variables** (Settings → Secrets and variables → Actions → Variables): `MAX_FILE_SIZE` (`8G`), `MAX_CONCURRENT_JOBS` (`1`), `JOB_TIMEOUT` (`7200`), `MULTI_FILE_MODE` (`largest`), `YTDLP_FORMAT` (`bv*+ba/b`).

5. **Enable Actions and run it.** Open the **Actions** tab → **Telegram Mirror Bot** → **Run workflow**. Wait for the job to start, then send your bot a link.

6. **Send a link.** The bot downloads the file, uploads it to Gofile, and replies with the public link.

### GitHub Actions limitations

- **One job is capped at 6 hours.** The workflow runs for 4h45m every 5 hours (`BOT_RUN_SECONDS=17100`). Messages sent during the short gap are delivered on the next run because Telegram keeps updates for 24 hours.
- **Cron is not exact.** GitHub can delay scheduled runs by several minutes.
- **Private repos run out of minutes.** 2,000 minutes/month is roughly 33 hours; this bot needs ~720 hours/month. Use a public repo or a real host.
- **Disk is limited.** The standard runner has about 14 GB of SSD. The default `MAX_FILE_SIZE=8G` and `MAX_CONCURRENT_JOBS=1` are chosen to fit. Lower them if you mirror many files.
- **Scheduled workflows can be disabled after 60 days without repository activity.** This workflow commits `.state/telegram.json` after each run, which keeps the repository active and also resumes the Telegram update offset.
- **State commits.** The workflow pushes a tiny state commit (`chore: update bot state [skip ci]`) when the Telegram offset changes, plus a weekly heartbeat so GitHub does not disable the schedule after 60 idle days. If `main` is protected, allow GitHub Actions to push, or change the workflow to use a cache instead.
- **Actions logs are public in a public repo.** Do not print tokens; the bot does not log them.
- **No inbound webhook.** The bot uses long polling, so it only handles messages while a workflow run is active.
- **Datacenter IPs get blocked.** YouTube and other video sites often ask for a sign-in from GitHub's IPs, and BitTorrent traffic may be throttled. For heavy video/torrent use, run the Docker image on a VPS.
- **End-of-run grace period.** The bot stops accepting new jobs when its run is ending and waits up to 20 minutes for active jobs to finish.

For real 24/7 operation, deploy the included Docker image to a VPS, Render, Railway, Fly.io, or similar.

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
| `/gf <link>` | Upload to Gofile |
| `/mirror <link>` | Same as `/gf` |
| `/status` | Active jobs |
| `/cancel` | Cancel your active jobs |
| `/id` | Show your Telegram user/chat ID |

You can also send a link, a magnet link, or a `.torrent` file directly.

## Supported links

| Source | Support |
|---|---|
| Direct file URLs (`.zip`, `.mp4`, `.pdf`, …) | ✅ aria2c |
| Pages with a Download button | ✅ heuristic resolver |
| GitHub releases | ✅ GitHub API picks the first asset |
| Google Drive single files | ✅ converted to a direct download URL |
| Dropbox share links | ✅ `dl=1` |
| Pixeldrain file links (download only) | ✅ direct API URL |
| MediaFire / SourceForge / similar pages | ✅ when the download link is in the HTML |
| YouTube, Vimeo, TikTok, X, Reddit, etc. | ✅ yt-dlp |
| Magnet links and `.torrent` files | ✅ aria2c |
| MEGA links | ❌ encrypted |
| Google Drive folders | ❌ not supported |
| Gofile download pages | ❌ reading them requires a premium token |
| Login/CAPTCHA/DRM-protected pages | ❌ not supported |

The resolver is intentionally best-effort. Sites that build download links in JavaScript after a login or CAPTCHA cannot be mirrored without site-specific code.

## Troubleshooting

**"That page does not expose a direct file"** — the page needs a login, CAPTCHA, or JavaScript that the resolver cannot execute. Try the original file URL.

**Gofile says the token is rejected** — remove `GOFILE_TOKEN` to use guest uploads, or create a fresh token from your Gofile profile.

**Torrents do not start** — install aria2c (`aria2c --version`) and make sure the workflow/Docker image includes it. A magnet with no seeders stops after 5 minutes (`--bt-stop-timeout=300`).

**Video download fails** — update yt-dlp (`pip install -U yt-dlp`). Age/region-locked sites may need `COOKIES_FILE`. GitHub Actions IPs are frequently blocked by YouTube; a VPS is more reliable for video sites.

**Torrents do not connect** — many datacenter networks block BitTorrent. Try a VPS, or use a direct link.

**The Actions run stops after 4h45m** — that is expected. The next scheduled run starts within about 15 minutes plus GitHub's cron delay.

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

The tests cover URL/filename handling, the HTML download-button resolver, the Gofile uploader with mocked HTTP responses, command parsing, and the downloader's file collection logic.

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
