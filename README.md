# Python scripts

A collection of useful Python utilities.

## 🎬 `yt_downloader.py` — the ultimate yt-dlp downloader

A single-file, batteries-included CLI on top of [yt-dlp](https://github.com/yt-dlp/yt-dlp).
Downloads video and audio from YouTube and [1000+ other sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md),
with live progress, playlists, batch mode, metadata embedding and smart fallbacks.

### Features

| | |
|---|---|
| ▶ Video | Quality presets `best / 4k / 2160p / 1440p / 1080p / 720p / 480p / 360p`, or any raw yt-dlp format selector (`-f`) |
| 🎵 Audio | Extract to `mp3 / m4a / aac / flac / opus / vorbis / ogg / wav` with your chosen bitrate |
| 📃 Playlists | Whole playlists, or slices: `--playlist-start`, `--playlist-end`, `--playlist-items 1,3,5-8` |
| 📦 Batch | Feed a text file of URLs; per-item error handling + summary report |
| 📊 Progress | Live bar with %, size, speed and ETA; per-item headers for playlists |
| 🏷️ Embedding | Metadata/chapters (`--embed-metadata`), cover art (`--embed-thumbnail`), subtitles (`--sub-langs` + `--embed-subs`) |
| 🧹 SponsorBlock | `--sponsorblock sponsor,intro` removes those sections automatically |
| 🔒 Access | `--cookies FILE`, `--cookies-from-browser chrome`, `--proxy URL` |
| ⚙️ Resilience | `--continue` resume, retries, rate limit (`--limit-rate 2M`), concurrent fragments (`--concurrent 8`) |
| 🔎 Introspection | `info` and `formats` subcommands |
| 🩺 Self-service | `check` (environment doctor) and `update` (self-update yt-dlp) |
| ⚙️ Config | JSON config file so you don't retype defaults (`config.example.json`) |

> Without ffmpeg the script still works: it detects the missing binary and
> automatically falls back to single-file (non-merged) formats.

### Install

```bash
# 1. Python 3.9+ and the one dependency
pip install -r requirements.txt

# 2. ffmpeg (for audio conversion, merging, embedding)
#    macOS:   brew install ffmpeg
#    Windows: winget install ffmpeg   (or choco install ffmpeg)
#    Debian/Ubuntu: sudo apt install ffmpeg

# 3. A JavaScript runtime — recent yt-dlp versions need it to parse YouTube
#    (any one is enough):
#    Node.js:  nodejs.org  /  brew install node  /  winget install nodejs
#    Deno:     curl -fsSL https://deno.land/install.sh | sh
#    Bun:      curl -fsSL https://bun.sh/install | bash

# 4. Verify everything
python yt_downloader.py check
```

### Quick start

```bash
# Download a video in 1080p (the subcommand is optional)
python yt_downloader.py https://youtu.be/dQw4w9WgXcQ -q 1080p

# Audio only → 320k mp3 with cover art + metadata
python yt_downloader.py audio URL -a mp3 -b 320K --embed-thumbnail --embed-metadata

# Watch a playlist's metadata without downloading
python yt_downloader.py info PLAYLIST_URL

# List every format a video offers
python yt_downloader.py formats URL

# Download items 1–10 of a playlist into ~/Videos
python yt_downloader.py download PLAYLIST_URL -p --playlist-items 1-10 -o ~/Videos

# Batch: one URL per line in a text file (# comments allowed)
python yt_downloader.py batch urls.txt

# Remove sponsors + intro, embed metadata, keep a metadata sidecar
python yt_downloader.py download URL --sponsorblock sponsor,intro --embed-metadata --info-json

# Interrupted? Just resume:
python yt_downloader.py download URL --continue
```

### Commands

| Command | What it does |
|---|---|
| `download [url]` | Download a video (default when you pass a bare URL) |
| `audio [url]` | Download audio only (codec via `-a`, bitrate via `-b`) |
| `info [url]` | Show title, channel, duration, views, upload date, description |
| `formats [url]` | Table of every available format + subtitle languages |
| `batch file [urls…]` | Download a list of URLs with a final success/failure report |
| `check` | Verify Python, yt-dlp, ffmpeg, output dir and network |
| `update [--check]` | Upgrade yt-dlp to the latest PyPI release |

### Common options (all download commands)

| Option | Meaning |
|---|---|
| `-o DIR` | Output directory (default `./downloads`) |
| `-q QUALITY` | Preset (`best, 4k, 2160p, …, 360p`) **or** a raw format selector. Presets are *max* heights: if nothing matches (e.g. a 4K-only video, or a direct file URL) it gracefully falls back to the best available format |
| `-f FORMAT` | Raw yt-dlp format selector, e.g. `bv*[height<=720][ext=webm]+ba` |
| `-a CODEC` / `-b BR` | Audio codec / bitrate (e.g. `flac`, `320K`) |
| `-p` / `--no-playlist` | Include / exclude the whole playlist |
| `--playlist-start N`, `--playlist-end N`, `--playlist-items 1,3,5-8` | Slice a playlist |
| `--cookies FILE` / `--cookies-from-browser BROWSER` | Private / age-restricted content |
| `--proxy URL` | Route downloads through a proxy |
| `--limit-rate RATE` | Cap speed (`500K`, `2M`) |
| `--concurrent N` | Parallel fragment downloads (DASH/HLS speedup, default 4) |
| `--retries N` | Retries per fragment (default 10) |
| `--sponsorblock CATS` | Cut sponsor/intro/outro/etc. sections |
| `--continue` | Resume a partially downloaded file |
| `--embed-metadata` / `--embed-thumbnail` / `--embed-subs` / `--sub-langs` | Enrich the file |
| `--info-json` | Save a `.info.json` metadata sidecar |
| `--restrict-filenames` | ASCII-only filenames |
| `-c FILE` | Use a specific JSON config file |
| `-v` | Show raw yt-dlp output instead of the pretty progress bar |

Run `python yt_downloader.py <command> -h` for full details.

### Configuration file

Copy `config.example.json` to `yt_downloader.json` (in this folder) or
`~/.yt_downloader.json`, then edit. CLI flags always override the file.

```json
{
  "output_dir": "~/Videos",
  "quality": "1080p",
  "audio_format": "mp3",
  "bitrate": "192K",
  "concurrent_fragments": 6,
  "sponsorblock": ["sponsor", "intro"]
}
```

All keys are optional. `cookies` (path to a Netscape cookies file),
`cookies_from_browser`, `proxy` and `limit_rate` are supported too.

### Tips

* **Re-running is safe** — existing files are skipped, not re-downloaded.
* **Age-restricted / members-only**: export cookies from your browser
  (e.g. the "Get cookies.txt" extension) and pass `--cookies cookies.txt`,
  or use `--cookies-from-browser chrome`.
* **Private links on mobile?** `--proxy` works with any HTTP/SOCKS proxy.
* **Best quality that keeps its thumbnail**:
  `python yt_downloader.py download URL -f "bv*[ext=mp4]+ba[ext=m4a]/b" --embed-thumbnail`
* **yt-dlp gets stale** when sites change — run `python yt_downloader.py update` occasionally.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `Sign in to confirm you're not a bot` | Use `--cookies-from-browser` or `--cookies`; or `--proxy` with a clean IP |
| `ffmpeg not found` warnings | Install ffmpeg (see Install) |
| YouTube: `No supported JavaScript runtime` | Install Node.js / Deno / Bun (see Install) |
| A site is broken / "No video formats found" | Run `python yt_downloader.py update` — yt-dlp fixes land daily |
| Slow downloads | Raise `--concurrent 8`, avoid `--limit-rate`, check `check`'s network line |
