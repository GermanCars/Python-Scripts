#!/usr/bin/env python3
"""
yt_downloader.py — the ultimate single-file yt-dlp frontend.

Features
--------
* Video downloads with quality presets (best / 4k / 2160p / 1440p / 1080p /
  720p / 480p / 360p) or any raw yt-dlp format selector
* Audio extraction: mp3, m4a, aac, flac, opus, vorbis, ogg, wav
* Whole playlists, or slices of them (--playlist-start / --playlist-end /
  --playlist-items)
* Batch mode: feed it a text file of URLs
* Live progress bar with speed + ETA, per-playlist-item headers
* Metadata / thumbnail / chapter / subtitle embedding (needs ffmpeg)
* SponsorBlock section removal, cookies, cookies-from-browser, proxy,
  rate limiting, resume, concurrent fragment downloads
* JSON config file, environment self-check, self-update

Examples
--------
    python yt_downloader.py https://youtu.be/dQw4w9WgXcQ
    python yt_downloader.py download URL -q 1080p -o ~/Videos
    python yt_downloader.py audio   URL -a mp3 -b 320K --embed-thumbnail
    python yt_downloader.py info    URL
    python yt_downloader.py formats URL
    python yt_downloader.py download PLAYLIST_URL -p --playlist-items 1-10
    python yt_downloader.py batch   urls.txt
    python yt_downloader.py check
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NoReturn

try:
    import yt_dlp
except ImportError:  # pragma: no cover
    print(
        "ERROR: yt-dlp is not installed.\n\n"
        "Install it with:  pip install yt-dlp\n"
        "or run:           pip install -r requirements.txt",
        file=sys.stderr,
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# Terminal cosmetics
# ---------------------------------------------------------------------------

class C:
    """ANSI colour codes (automatically disabled when not a TTY)."""

    ON = sys.stdout.isatty()
    RESET = "\033[0m" if ON else ""
    BOLD = "\033[1m" if ON else ""
    DIM = "\033[2m" if ON else ""
    RED = "\033[31m" if ON else ""
    GREEN = "\033[32m" if ON else ""
    YELLOW = "\033[33m" if ON else ""
    BLUE = "\033[34m" if ON else ""
    MAGENTA = "\033[35m" if ON else ""
    CYAN = "\033[36m" if ON else ""


def paint(text: str, *codes: str) -> str:
    """Wrap *text* in ANSI colour codes (no-op when colours are off)."""
    if not codes or not C.ON:
        return text
    return "".join(codes) + text + C.RESET


class DownloadFailed(Exception):
    """Raised when a single URL could not be downloaded."""


def die(msg: str, code: int = 1) -> NoReturn:
    print(paint(f"ERROR: {msg}", C.RED, C.BOLD), file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def fmt_bytes(n: float | None) -> str:
    """Human friendly byte size."""
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_duration(seconds: float | None) -> str:
    """Seconds -> HH:MM:SS or MM:SS."""
    if seconds is None:
        return "??:??"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_rate(text: str) -> int:
    """'1M' / '512K' / '2G' / '1000000' -> bytes per second."""
    t = text.strip().upper().replace(" ", "")
    mult = 1
    if t.endswith("K"):
        t, mult = t[:-1], 1024
    elif t.endswith("M"):
        t, mult = t[:-1], 1024 ** 2
    elif t.endswith("G"):
        t, mult = t[:-1], 1024 ** 3
    try:
        return int(float(t) * mult)
    except ValueError:
        die(f"invalid rate: {text!r} (use e.g. 500K, 2M, 1000000)")


def make_bar(pct: float, width: int = 24) -> str:
    filled = max(0, min(width, int(width * pct / 100)))
    return "█" * filled + "░" * (width - filled)


def version_tuple(v: str) -> tuple:
    nums = re.findall(r"\d+", v.split("+")[0].split("-")[0])
    return tuple(int(x) for x in nums) or (0,)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ffmpeg_version() -> str | None:
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=5)
        return (out.stdout or "").splitlines()[0].strip()
    except Exception:
        return "found (could not read version)"


JS_RUNTIMES = ("deno", "node", "bun")


def detect_js_runtimes() -> dict[str, dict]:
    """Find JavaScript runtimes on PATH (needed by recent yt-dlp for YouTube)."""
    return {name: {} for name in JS_RUNTIMES if shutil.which(name)}


def parse_playlist_items(text: str) -> list[int]:
    """'1,3,5-8' -> [1, 3, 5, 6, 7, 8]  (1-based playlist positions)."""
    items: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                lo, _, hi = part.partition("-")
                items.extend(range(int(lo), int(hi) + 1))
            else:
                items.append(int(part))
        except ValueError:
            die(f"invalid --playlist-items: {part!r} (use e.g. 1,3,5-8)")
    return items


# ---------------------------------------------------------------------------
# Progress display (replaces yt-dlp's own, which we silence)
# ---------------------------------------------------------------------------

def make_progress_hooks():
    """Return (progress_hook, postprocessor_hook) for a fancy live bar."""

    def progress(d: dict[str, Any]) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes", 0)
            speed = d.get("speed")
            if total:
                pct = min(100.0, done / total * 100)
                eta = d.get("eta")
                line = (
                    f"  {paint(make_bar(pct), C.CYAN)} {pct:5.1f}% "
                    f"{paint(fmt_bytes(done) + ' / ' + fmt_bytes(total), C.DIM)}  "
                    f"{paint(fmt_bytes(speed) + '/s' if speed else '?/s', C.DIM)}  "
                    f"ETA {fmt_duration(eta) if eta is not None else '??:??'}"
                )
            else:
                line = (
                    f"  {paint('↓', C.CYAN)} {fmt_bytes(done)}  "
                    f"{paint(fmt_bytes(speed) + '/s' if speed else '', C.DIM)}"
                )
            sys.stdout.write("\r" + line.ljust(74))
            sys.stdout.flush()
        elif d.get("status") == "finished":
            sys.stdout.write("\r" + " " * 80 + "\r")
            sys.stdout.flush()

    def postprocessor(d: dict[str, Any]) -> None:
        if d.get("status") == "started":
            name = d.get("postprocessor", "PostProcessor")
            info = d.get("info") or ""
            label = {
                "ExtractAudio": "Converting audio",
                "FFmpegMetadata": "Embedding metadata",
                "EmbedThumbnail": "Embedding thumbnail",
                "FFmpegEmbedSubtitle": "Embedding subtitles",
                "FFmpegVideoRemuxer": "Remuxing video",
                "Merger": "Merging formats",
            }.get(name, name)
            sys.stdout.write(
                f"\r{paint('  ✦', C.MAGENTA)} {label}{(' — ' + info) if info else ''}\n"
            )
            sys.stdout.flush()

    return progress, postprocessor


class PlaylistLogger:
    """Logger that shows playlist item headers and swallows yt-dlp chatter."""

    _ENTRY_RE = re.compile(r'Downloading (\d+) of (\d+) in "(.*)" as')
    _PLAYLIST_RE = re.compile(r"\[download\] Downloading playlist: (.+)")

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        m = self._ENTRY_RE.match(msg)
        if m:
            n, total, title = m.group(1), m.group(2), m.group(3)
            print(f"\n  {paint(f'[{n}/{total}]', C.CYAN, C.BOLD)} {title}", flush=True)
            return
        m = self._PLAYLIST_RE.search(msg)
        if m:
            print(f"\n{paint('📃 Playlist: ' + m.group(1), C.BOLD)}", flush=True)
            return
        # everything else: our own progress bar covers it
        pass

    def warning(self, msg: str) -> None:
        print(paint(f"  warning: {msg}", C.YELLOW), file=sys.stderr, flush=True)

    def error(self, msg: str) -> None:
        print(paint(f"  error: {msg}", C.RED), file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Quality presets & option constants
# ---------------------------------------------------------------------------

# Each capped preset ends with /b so it gracefully falls back to the best
# single-file format when no height matches (e.g. direct video file URLs).
QUALITY_PRESETS: dict[str, str] = {
    "best": "bv*+ba/b",
    "4k": "bv*[height<=2160]+ba/b[height<=2160]/b",
    "2160p": "bv*[height<=2160]+ba/b[height<=2160]/b",
    "1440p": "bv*[height<=1440]+ba/b[height<=1440]/b",
    "1080p": "bv*[height<=1080]+ba/b[height<=1080]/b",
    "720p": "bv*[height<=720]+ba/b[height<=720]/b",
    "480p": "bv*[height<=480]+ba/b[height<=480]/b",
    "360p": "bv*[height<=360]+ba/b[height<=360]/b",
}

AUDIO_CODECS = ["mp3", "m4a", "aac", "flac", "opus", "vorbis", "ogg", "wav"]
SPONSORBLOCK_CATEGORIES = [
    "sponsor", "promo", "intro", "outro", "selfpromo",
    "filler", "interaction", "music_offtopic", "preview", "poi", "chapter", "all",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "output_dir": "downloads",
    "quality": "1080p",
    "audio_format": "mp3",
    "bitrate": "192K",
    "proxy": None,
    "cookies": None,
    "cookies_from_browser": None,
    "limit_rate": None,
    "concurrent_fragments": 4,
    "retries": 10,
    "sponsorblock": [],
}

CONFIG_FILENAMES = ("yt_downloader.json", ".yt_downloader.json")


# ---------------------------------------------------------------------------
# Config handling
# ---------------------------------------------------------------------------

def find_config(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            die(f"config file not found: {p}")
        return p
    for name in CONFIG_FILENAMES:
        p = Path.cwd() / name
        if p.is_file():
            return p
    p = Path.home() / ".yt_downloader.json"
    if p.is_file():
        return p
    return None


def load_config(path: Path | None) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if path and path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            die(f"could not read config {path}: {e}")
        if not isinstance(data, dict):
            die(f"config {path} must contain a JSON object")
        unknown = sorted(set(data) - set(DEFAULT_CONFIG))
        if unknown:
            print(paint(f"  warning: unknown config key(s) ignored: {', '.join(unknown)}", C.YELLOW))
        cfg.update({k: v for k, v in data.items() if v is not None})
    return cfg


def apply_cli(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    """CLI flags win over config-file values."""
    if getattr(args, "output_dir", None):
        cfg["output_dir"] = args.output_dir
    if getattr(args, "proxy", None):
        cfg["proxy"] = args.proxy
    if getattr(args, "cookies", None):
        cfg["cookies"] = args.cookies
    if getattr(args, "cookies_from_browser", None):
        cfg["cookies_from_browser"] = args.cookies_from_browser
    if getattr(args, "limit_rate", None):
        cfg["limit_rate"] = args.limit_rate
    if getattr(args, "concurrent", None):
        cfg["concurrent_fragments"] = args.concurrent
    if getattr(args, "retries", None):
        cfg["retries"] = args.retries
    if getattr(args, "sponsorblock", None):
        cats = [c.strip() for c in args.sponsorblock.split(",") if c.strip()]
        bad = [c for c in cats if c not in SPONSORBLOCK_CATEGORIES]
        if bad:
            die(f"unknown sponsorblock category(ies): {', '.join(bad)} (valid: {', '.join(SPONSORBLOCK_CATEGORIES)})")
        cfg["sponsorblock"] = cats
    if getattr(args, "audio_format", None):
        cfg["audio_format"] = args.audio_format
    if getattr(args, "bitrate", None):
        cfg["bitrate"] = args.bitrate


# ---------------------------------------------------------------------------
# yt-dlp option builder
# ---------------------------------------------------------------------------

def resolve_playlist(args: argparse.Namespace) -> bool:
    if args.no_playlist:
        return False
    if args.playlist:
        return True
    return bool(args.playlist_items) or args.playlist_start is not None or args.playlist_end is not None


def build_opts(cfg: dict[str, Any], args: argparse.Namespace, *, audio: bool, playlist: bool) -> dict[str, Any]:
    outdir = Path(cfg["output_dir"]).expanduser()
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        die(f"cannot create output directory {outdir}: {e}")

    opts: dict[str, Any] = {
        "outtmpl": str(outdir / "%(title)s [%(id)s].%(ext)s"),
        "noplaylist": not playlist,
        "quiet": not args.verbose,
        "no_warnings": True,
        "noprogress": True,
        "retries": int(cfg["retries"]),
        "fragment_retries": 10,
        "extractor_retries": 3,
        "concurrent_fragment_downloads": max(1, int(cfg["concurrent_fragments"])),
        "socket_timeout": 30,
        "restrictfilenames": args.restrict_filenames,
        "windowsfilenames": True,
        "overwrites": False,  # re-running is safe: existing files are skipped
        "ignoreerrors": False,
    }

    runtimes = detect_js_runtimes()
    if runtimes:
        opts["js_runtimes"] = runtimes
    elif not args.verbose and not getattr(args, "_js_warned", False):
        print(paint("  warning: no JavaScript runtime (deno/node/bun) found — YouTube "
                    "extraction may fail. Install one, e.g. Node.js (nodejs.org).", C.YELLOW))
        args._js_warned = True

    if args.resume:
        opts["continue"] = True
    if cfg["proxy"]:
        opts["proxy"] = cfg["proxy"]
    if cfg["cookies"]:
        opts["cookiesfile"] = str(Path(cfg["cookies"]).expanduser())
    if cfg["cookies_from_browser"]:
        opts["cookiesfrombrowser"] = (cfg["cookies_from_browser"],)
    if cfg["limit_rate"]:
        opts["limitrate"] = parse_rate(cfg["limit_rate"])
    if cfg["sponsorblock"]:
        opts["sponsorblock_remove"] = list(cfg["sponsorblock"])
    if args.info_json:
        opts["writeinfojson"] = True

    if not args.verbose:
        prog, pp = make_progress_hooks()
        opts["progress_hooks"] = [prog]
        opts["postprocessor_hooks"] = [pp]
        opts["logger"] = PlaylistLogger()

    have_ffmpeg = ffmpeg_available()

    if audio:
        opts["format"] = "bestaudio/best"
        codec = cfg["audio_format"]
        pp_list: list[dict[str, Any]] = [{"key": "FFmpegExtractAudio", "preferredcodec": codec}]
        if codec not in {"flac", "wav"}:  # lossless codecs ignore bitrate
            bitrate = str(cfg["bitrate"])
            if re.fullmatch(r"\d+", bitrate):
                bitrate += "K"
            pp_list[0]["preferredquality"] = bitrate
        if args.embed_metadata:
            pp_list.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True})
        if args.embed_thumbnail:
            opts["writethumbnail"] = True
            if have_ffmpeg:
                pp_list.append({"key": "EmbedThumbnail", "already_have_thumbnail": True})
            else:
                print(paint("  warning: ffmpeg required to embed the thumbnail — "
                            "it will be saved as a separate image instead.", C.YELLOW))
        if args.sub_langs and args.embed_subs and have_ffmpeg:
            pp_list.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": True})
        opts["postprocessors"] = pp_list
        if args.sub_langs:
            opts["subtitleslangs"] = [l.strip() for l in args.sub_langs.split(",") if l.strip()]
            opts["writesubtitles"] = True
            opts["writeautomaticsub"] = True
            opts["subtitlesformat"] = "srt/best"
    else:
        quality = args.quality or str(cfg["quality"])
        if args.format:
            selector = args.format
        elif quality in QUALITY_PRESETS:
            selector = QUALITY_PRESETS[quality]
        else:
            selector = quality  # treat as a raw yt-dlp format selector
            print(paint(f"  note: using raw format selector: {selector}", C.DIM))
        if not have_ffmpeg and "+" in selector:
            m = re.search(r"height<=\s*(\d+)", selector)
            cap = m.group(1) if m else "720"
            selector = f"b[height<={cap}]/b"
            print(paint("  note: ffmpeg not found — falling back to single-file (non-merged) formats.", C.YELLOW))
        opts["format"] = selector

        pp_list = []
        if args.embed_metadata:
            pp_list.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True})
        if args.embed_thumbnail:
            opts["writethumbnail"] = True
            if have_ffmpeg:
                pp_list.append({"key": "EmbedThumbnail", "already_have_thumbnail": True})
            else:
                print(paint("  warning: ffmpeg required to embed the thumbnail — "
                            "it will be saved as a separate image instead.", C.YELLOW))
        if args.sub_langs:
            opts["subtitleslangs"] = [l.strip() for l in args.sub_langs.split(",") if l.strip()]
            opts["writesubtitles"] = True
            opts["writeautomaticsub"] = True
            opts["subtitlesformat"] = "srt/best"
            if args.embed_subs and have_ffmpeg:
                pp_list.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": True})
            elif args.embed_subs:
                print(paint("  warning: ffmpeg required to embed subtitles — "
                            "they will be saved as separate .srt files.", C.YELLOW))
        if pp_list:
            opts["postprocessors"] = pp_list

    if args.playlist_start is not None:
        opts["playliststart"] = args.playlist_start
    if args.playlist_end is not None:
        opts["playlistend"] = args.playlist_end
    if args.playlist_items:
        opts["playlistitems"] = parse_playlist_items(args.playlist_items)

    return opts


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------

def collect_files(info: dict[str, Any] | None) -> list[str]:
    """Walk (playlist) info dicts and return every saved file path."""
    files: list[str] = []

    def walk(entry: dict[str, Any]) -> None:
        for rd in entry.get("requested_downloads") or []:
            if rd and rd.get("filepath"):
                files.append(rd["filepath"])
        for sub in entry.get("entries") or []:
            if isinstance(sub, dict):
                walk(sub)

    if info:
        walk(info)
    return files


def download_one(url: str, cfg: dict[str, Any], args: argparse.Namespace,
                 *, audio: bool = False, index: int | None = None,
                 total: int | None = None) -> list[str]:
    opts = build_opts(cfg, args, audio=audio, playlist=resolve_playlist(args))
    label = url if index is None else f"[{index}/{total}] {url}"
    print(paint(f"▶ {label}", C.BOLD))
    if resolve_playlist(args) and not audio:
        print(paint("  playlist mode: every item will be downloaded", C.DIM))

    started = time.monotonic()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        raise DownloadFailed(str(e))

    files = collect_files(info)
    elapsed = time.monotonic() - started

    if files:
        total_bytes = 0
        for f in files:
            try:
                total_bytes += Path(f).stat().st_size
            except OSError:
                pass
        print(paint("✔ Done", C.GREEN, C.BOLD))
        for f in files:
            print(f"    {paint('•', C.DIM)} {f}")
        plural = "s" if len(files) != 1 else ""
        summary = f"{fmt_bytes(total_bytes)} saved in {fmt_duration(elapsed)} ({len(files)} file{plural})"
        print(f"    {paint(summary, C.DIM)}")
    else:
        print(paint("✔ Done", C.GREEN, C.BOLD) + paint(" — no new files (they already exist)", C.DIM))
    return files


def require_url(url: str | None) -> str:
    if url:
        return url
    try:
        if sys.stdin.isatty():
            url = input("URL: ").strip()
        else:
            url = sys.stdin.readline().strip()
    except (EOFError, KeyboardInterrupt):
        url = ""
    if not url:
        die("no URL provided (pass one or run interactively)")
    return url


# ---------------------------------------------------------------------------
# info / formats commands
# ---------------------------------------------------------------------------

def cmd_info(url: str, cfg: dict[str, Any], args: argparse.Namespace) -> None:
    opts = build_opts(cfg, args, audio=False, playlist=resolve_playlist(args))
    opts["extract_flat"] = "in_playlist"  # fast: no need for full entries
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        die(str(e))
    if not info:
        die("no information found for that URL")

    if info.get("_type") == "playlist" or info.get("entries") is not None:
        entries = [e for e in (info.get("entries") or []) if e]
        print(paint(f"📃 Playlist: {info.get('title') or 'playlist'}", C.BOLD))
        who = info.get("channel") or info.get("uploader") or "unknown"
        print(f"  {paint(who, C.CYAN)} · {len(entries)} entries\n")
        for i, en in enumerate(entries, 1):
            print(f"  {paint(f'{i:>3}.', C.DIM)} {en.get('title') or '(no title)'}"
                  f"  {paint('[' + fmt_duration(en.get('duration')) + ']', C.DIM)}")
        return

    print(paint(f"📺 {info.get('title') or '(no title)'}", C.BOLD))
    lines: list[str] = []
    if info.get("channel") or info.get("uploader"):
        lines.append(f"Channel:   {info.get('channel') or info.get('uploader')}")
    if info.get("duration") is not None:
        lines.append(f"Duration:  {fmt_duration(info['duration'])}")
    else:
        lines.append("Duration:  LIVE")
    if info.get("view_count") is not None:
        lines.append(f"Views:     {info['view_count']:,}")
    if info.get("like_count") is not None:
        lines.append(f"Likes:     {info['like_count']:,}")
    if info.get("upload_date"):
        d = info["upload_date"]
        lines.append(f"Uploaded:  {d[:4]}-{d[4:6]}-{d[6:]}")
    lines.append(f"ID:        {info.get('id')}")
    heights = sorted({f.get("height") for f in (info.get("formats") or []) if f.get("height")})
    if heights:
        lines.append(f"Max video: {heights[-1]}p  ({len(info.get('formats') or [])} formats)")
    print("\n".join("  " + l for l in lines))

    desc = " ".join((info.get("description") or "").split())
    if desc:
        if len(desc) > 400:
            desc = desc[:400].rsplit(" ", 1)[0] + "…"
        print("\n  " + paint(desc, C.DIM))


def cmd_formats(url: str, cfg: dict[str, Any], args: argparse.Namespace) -> None:
    opts = build_opts(cfg, args, audio=False, playlist=False)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        die(str(e))
    if not info:
        die("no information found for that URL")
    if info.get("_type") == "playlist" or info.get("entries") is not None:
        die("that URL is a playlist — give a single-video URL to list formats")

    fmts = info.get("formats") or []
    if not fmts:
        die("no formats listed for that video")

    print(paint(f"Formats available for {info.get('title') or url}", C.BOLD))
    print()
    print(paint(f"{'ID':<14} {'EXT':<8} {'RESOLUTION':<14} {'FPS':<5} {'TBR':<6} {'SIZE':<10} NOTE", C.DIM))
    for f in fmts:
        fid = (f.get("format_id") or "?")[:14]
        ext = f.get("ext") or "?"
        if f.get("width") and f.get("height"):
            res = f"{f['width']}x{f['height']}"
        elif f.get("height"):
            res = f"{f['height']}p"
        else:
            res = "audio"
        fps = str(f.get("fps") or "")
        tbr = str(f.get("tbr") or "")
        size = fmt_bytes(f.get("filesize") if f.get("filesize") is not None else f.get("filesize_approx"))
        note = (f.get("format_note") or "")[:16]
        print(f"{fid:<14} {ext:<8} {res:<14} {fps:<5} {tbr:<6} {size:<10} {note}")

    subs = sorted(set((info.get("subtitles") or {}).keys()) | set((info.get("automatic_captions") or {}).keys()))
    if subs:
        shown = ", ".join(subs[:15]) + (f" … ({len(subs)} total)" if len(subs) > 15 else "")
        print(f"\n{paint('Subtitles:', C.BOLD)} {shown}")


# ---------------------------------------------------------------------------
# batch command
# ---------------------------------------------------------------------------

def cmd_batch(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    urls: list[str] = []
    p = Path(args.file).expanduser()
    if not p.is_file():
        die(f"batch file not found: {p}")
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    urls.extend(args.extra)
    if not urls:
        die("no URLs found in the batch file")

    print(paint(f"Batch download: {len(urls)} URL(s)", C.BOLD))
    ok = 0
    failed: list[tuple[str, str]] = []
    interrupted = False
    t0 = time.monotonic()

    for i, u in enumerate(urls, 1):
        try:
            download_one(u, cfg, args, index=i, total=len(urls))
            ok += 1
        except DownloadFailed as e:
            failed.append((u, " ".join(str(e).split())))
            print(paint(f"✘ {u}", C.RED) + f"\n    {paint(' '.join(str(e).split()), C.DIM)}")
        except KeyboardInterrupt:
            print()
            print(paint("Batch interrupted.", C.YELLOW))
            interrupted = True
            break

    print()
    print(paint("=" * 62))
    print(paint(f"Batch summary: {ok} succeeded, {len(failed)} failed "
                f"in {fmt_duration(time.monotonic() - t0)}", C.BOLD))
    for u, err in failed:
        print(paint(f"  ✘ {u}", C.RED) + paint(f"  ({err[:80]})", C.DIM))

    if interrupted:
        sys.exit(130)
    sys.exit(1 if failed else 0)


# ---------------------------------------------------------------------------
# check / update commands
# ---------------------------------------------------------------------------

def cmd_check(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    print(paint("Environment check", C.BOLD))
    hard_fail = False

    py_ok = sys.version_info >= (3, 9)
    print(f"  Python      {sys.version.split()[0]}"
          + (paint("  ✔", C.GREEN) if py_ok else paint("  ✘ (3.9 or newer required)", C.RED)))
    hard_fail |= not py_ok

    ver = getattr(yt_dlp.version, "__version__", "unknown")
    print(f"  yt-dlp      {ver}  {paint('✔', C.GREEN)}")

    jss = [n for n in JS_RUNTIMES if shutil.which(n)]
    if jss:
        print(f"  JS runtime  {', '.join(jss)}  {paint('✔ (needed by YouTube)', C.DIM)}")
    else:
        print("  JS runtime  " + paint("NOT FOUND  ✘", C.RED)
              + paint("  (install Node.js / Deno / Bun — recent yt-dlp needs it for YouTube)", C.DIM))

    ffv = ffmpeg_version()
    if ffv:
        print(f"  ffmpeg      {ffv}  {paint('✔', C.GREEN)}")
    else:
        print("  ffmpeg      " + paint("NOT FOUND  ✘", C.RED)
              + paint("  (install ffmpeg for audio conversion, stream merging & metadata embedding)", C.DIM))

    outdir = Path(cfg["output_dir"]).expanduser()
    try:
        outdir.mkdir(parents=True, exist_ok=True)
        probe = outdir / ".write_test"
        probe.write_text("ok")
        probe.unlink()
        print(f"  Output dir  {outdir.resolve()}  {paint('writable ✔', C.GREEN)}")
    except OSError as e:
        print(f"  Output dir  {outdir}  {paint('not writable ✘', C.RED)} ({e})")
        hard_fail = True

    try:
        import urllib.request
        req = urllib.request.Request("https://www.youtube.com", method="HEAD",
                                     headers={"User-Agent": "Mozilla/5.0"})
        urllib.request.urlopen(req, timeout=5)
        print("  Network     " + paint("reachable ✔", C.GREEN))
    except Exception:
        print("  Network     " + paint("unreachable — downloads will fail  ✘", C.RED))
        hard_fail = True

    return 1 if hard_fail else 0


def cmd_update(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    current = getattr(yt_dlp.version, "__version__", "0.0.0")
    print(f"Installed yt-dlp: {current}")
    latest: str | None = None
    try:
        import urllib.request
        with urllib.request.urlopen("https://pypi.org/pypi/yt-dlp/json", timeout=10) as r:
            latest = json.loads(r.read()).get("info", {}).get("version")
    except Exception as e:
        print(paint(f"Could not reach PyPI: {e}", C.YELLOW))
        die("offline — cannot verify the latest version", 1)
    if not latest:
        die("could not determine the latest yt-dlp version")

    if version_tuple(current) >= version_tuple(latest):
        print(paint(f"✔ Already up to date ({latest}).", C.GREEN))
        return

    print(f"Latest on PyPI:   {latest}")
    if args.check:
        print(paint("Update available — run without --check to install it.", C.YELLOW))
        sys.exit(1)
    print("Upgrading…")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"])
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        die(f"pip upgrade failed ({e}) — try manually: python -m pip install -U yt-dlp")
    print(paint("✔ Upgraded. Re-run 'check' to verify.", C.GREEN))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

EPILOG = """\
examples:
  python yt_downloader.py https://youtu.be/dQw4w9WgXcQ
  python yt_downloader.py download URL -q 1080p -o ~/Videos
  python yt_downloader.py audio   URL -a mp3 -b 320K --embed-thumbnail
  python yt_downloader.py info    URL
  python yt_downloader.py formats URL
  python yt_downloader.py download PLAYLIST_URL -p --playlist-items 1-10
  python yt_downloader.py batch   urls.txt
  python yt_downloader.py check
"""

SUBCOMMANDS = {"download", "audio", "info", "formats", "batch", "check", "update", "help"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yt_downloader.py",
        description="The ultimate single-file yt-dlp downloader.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    p.add_argument("--version", action="version",
                   version=f"%(prog)s (yt-dlp {getattr(yt_dlp.version, '__version__', '?')})")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-o", "--output-dir", metavar="DIR",
                        help="output directory (default: downloads)")
    common.add_argument("-q", "--quality", metavar="QUALITY",
                        help=f"quality preset: {', '.join(QUALITY_PRESETS)} — or a raw yt-dlp format selector")
    common.add_argument("-f", "--format", dest="format", metavar="FORMAT",
                        help="raw yt-dlp format selector (overrides -q)")
    common.add_argument("-a", "--audio-format", dest="audio_format", choices=AUDIO_CODECS,
                        metavar="CODEC", help="audio codec for the 'audio' command (default: mp3)")
    common.add_argument("-b", "--bitrate", metavar="BR",
                        help="audio bitrate, e.g. 128K, 192K, 320K (default: 192K)")
    common.add_argument("-p", "--playlist", action="store_true",
                        help="include the whole playlist when a playlist URL is given")
    common.add_argument("--no-playlist", action="store_true",
                        help="ignore the playlist, download only the first video")
    common.add_argument("--playlist-start", type=int, metavar="N",
                        help="first playlist item to download (1-based)")
    common.add_argument("--playlist-end", type=int, metavar="N",
                        help="last playlist item to download (1-based)")
    common.add_argument("--playlist-items", metavar="ITEMS",
                        help='playlist items, e.g. "1,3,5-8" (implies --playlist)')
    common.add_argument("--cookies", metavar="FILE",
                        help="Netscape cookies file for member-only / age-restricted content")
    common.add_argument("--cookies-from-browser", metavar="BROWSER",
                        help="load cookies from a browser: chrome, firefox, safari, edge, brave, opera, vivaldi")
    common.add_argument("--proxy", metavar="URL",
                        help="proxy URL, e.g. http://127.0.0.1:7890")
    common.add_argument("--limit-rate", metavar="RATE",
                        help="max download speed, e.g. 500K, 2M")
    common.add_argument("--concurrent", type=int, metavar="N",
                        help="concurrent fragment downloads for DASH/HLS (default: 4)")
    common.add_argument("--retries", type=int, metavar="N",
                        help="retries per fragment (default: 10)")
    common.add_argument("--sponsorblock", metavar="CATS",
                        help=f"remove SponsorBlock sections: {', '.join(SPONSORBLOCK_CATEGORIES)}")
    common.add_argument("--restrict-filenames", action="store_true",
                        help="ASCII-only filenames (for very old filesystems)")
    common.add_argument("--continue", dest="resume", action="store_true",
                        help="resume interrupted downloads")
    common.add_argument("--info-json", action="store_true",
                        help="also save a .info.json sidecar with full metadata")
    common.add_argument("--embed-metadata", action="store_true",
                        help="embed title, artist, chapters & description (needs ffmpeg)")
    common.add_argument("--embed-thumbnail", action="store_true",
                        help="embed the thumbnail as cover art (needs ffmpeg)")
    common.add_argument("--embed-subs", action="store_true",
                        help="embed downloaded subtitles into the file (needs ffmpeg)")
    common.add_argument("--sub-langs", metavar="LANGS",
                        help='download subtitles, e.g. "en.*,en,de" (patterns allowed)')
    common.add_argument("-c", "--config", metavar="FILE",
                        help="path to a JSON config file")
    common.add_argument("-v", "--verbose", action="store_true",
                        help="show yt-dlp's own output (instead of the fancy bar)")

    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    for name, help_ in (
        ("download", "download a video (default command)"),
        ("audio", "download audio only (mp3/m4a/flac/…)"),
        ("info", "show metadata for a video or playlist"),
        ("formats", "list all available formats"),
    ):
        sp = sub.add_parser(name, help=help_, parents=[common],
                            formatter_class=argparse.RawDescriptionHelpFormatter)
        sp.add_argument("url", nargs="?", help="video or playlist URL (prompts if omitted)")

    bp = sub.add_parser("batch", help="download every URL listed in a text file",
                        parents=[common], formatter_class=argparse.RawDescriptionHelpFormatter)
    bp.add_argument("file", help="text file with one URL per line (# comments allowed)")
    bp.add_argument("extra", nargs="*", help="additional URLs")

    sub.add_parser("check", help="verify the environment (yt-dlp, ffmpeg, network, output dir)",
                   parents=[common])
    up = sub.add_parser("update", help="upgrade yt-dlp to the latest version", parents=[common])
    up.add_argument("--check", action="store_true", help="only report the latest version, don't install")
    return p


def main() -> None:
    # Keep unicode symbols safe on any console (e.g. legacy Windows code pages).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass

    # Bare URL (no subcommand) → treat as `download`.
    argv = sys.argv[1:]
    first = next((a for a in argv if not a.startswith("-")), None)
    if first and first not in SUBCOMMANDS:
        argv.insert(0, "download")

    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        sys.exit(0)

    cfg_path = find_config(args.config)
    cfg = load_config(cfg_path)
    apply_cli(cfg, args)
    if cfg_path:
        print(paint(f"config: {cfg_path}", C.DIM))

    if args.command == "download":
        try:
            download_one(require_url(args.url), cfg, args)
        except DownloadFailed as e:
            die(str(e))
    elif args.command == "audio":
        if not ffmpeg_available():
            print(paint("warning: ffmpeg not found — you need ffmpeg to convert audio "
                        "(see https://ffmpeg.org for your platform).", C.YELLOW))
        try:
            download_one(require_url(args.url), cfg, args, audio=True)
        except DownloadFailed as e:
            die(str(e))
    elif args.command == "info":
        cmd_info(require_url(args.url), cfg, args)
    elif args.command == "formats":
        cmd_formats(require_url(args.url), cfg, args)
    elif args.command == "batch":
        cmd_batch(cfg, args)
    elif args.command == "check":
        sys.exit(cmd_check(cfg, args))
    elif args.command == "update":
        cmd_update(cfg, args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        print(paint("Interrupted — resume later with --continue", C.YELLOW))
        sys.exit(130)
    except DownloadFailed as e:
        die(str(e))
