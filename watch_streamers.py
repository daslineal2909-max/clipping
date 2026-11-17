#!/usr/bin/env python3
"""watch_streamers.py - prueft taeglich neue VODs deiner Streamer und clippt sie.

Liest die Kanaele aus streamers.txt, holt mit yt-dlp die neuesten VODs (nur die Liste,
kein Video) und startet gta_clipper.py fuer jedes VOD, das noch nicht bearbeitet wurde.
Was schon erledigt ist, steht in clips/state.json.

Gedacht fuer einen taeglichen Aufruf per cron oder Windows-Aufgabenplanung:
  python watch_streamers.py
  python watch_streamers.py --dry-run          # nur anzeigen, was neu ist
  python watch_streamers.py -- --top 5 --threshold 2.5   # Optionen fuer gta_clipper.py
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
CLIPPER = HERE / "gta_clipper.py"
EXIT_NO_HIGHLIGHTS = 3          # muss zu gta_clipper.EXIT_NO_HIGHLIGHTS passen
MAX_ATTEMPTS = 3                # fehlgeschlagene VODs so oft erneut versuchen


# ---------------------------------------------------------------------------
# Kanaele
# ---------------------------------------------------------------------------

def parse_channel(line: str) -> dict | None:
    """Versteht Twitch-/YouTube-URLs, 'yt:@handle' und blanke Twitch-Namen."""
    line = line.split("#", 1)[0].strip()
    if not line:
        return None
    m = re.match(r"(?:https?://)?(?:www\.|m\.)?twitch\.tv/([\w]+)", line, re.I)
    if m:
        return {"platform": "twitch", "name": m.group(1).lower()}
    m = re.match(r"(?:https?://)?(?:www\.|m\.)?youtube\.com/(@[\w.-]+|channel/[\w-]+|c/[\w.-]+)",
                 line, re.I)
    if m:
        return {"platform": "youtube", "name": m.group(1)}
    m = re.match(r"(?:yt|youtube):\s*(@?[\w.-]+)$", line, re.I)
    if m:
        name = m.group(1)
        return {"platform": "youtube", "name": name if name.startswith("@") else "@" + name}
    if re.fullmatch(r"[\w]{3,25}", line):
        return {"platform": "twitch", "name": line.lower()}
    raise ValueError(f"Kanal nicht erkannt: {line!r}")


def load_channels(path: Path) -> list[dict]:
    chans = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            c = parse_channel(line)
        except ValueError as e:
            sys.exit(f"{path}:{n}: {e}")
        if c:
            chans.append(c)
    return chans


def channel_label(c: dict) -> str:
    return re.sub(r"[^\w.-]+", "_", c["name"].lstrip("@"))


def listing_url(c: dict) -> str:
    if c["platform"] == "twitch":
        return f"https://www.twitch.tv/{c['name']}/videos?filter=archives&sort=time"
    return f"https://www.youtube.com/{c['name']}/streams"


# ---------------------------------------------------------------------------
# VODs abfragen (yt-dlp, nur Metadaten)
# ---------------------------------------------------------------------------

class _QuietLogger:
    """yt-dlp-Meldungen schlucken; Fehler kommen als Exception und werden unten gemeldet."""
    def debug(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _ydl(opts: dict):
    try:
        import yt_dlp
    except ImportError:
        sys.exit("yt-dlp fehlt: pip install yt-dlp")
    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "logger": _QuietLogger(),
                             **opts})


def list_vods(c: dict, limit: int) -> list[dict]:
    """Neueste VODs eines Kanals, neuestes zuerst."""
    with _ydl({"extract_flat": "in_playlist", "playlistend": limit}) as ydl:
        info = ydl.extract_info(listing_url(c), download=False)
    vods = []
    for e in (info or {}).get("entries") or []:
        if not e:
            continue
        # YouTube: laufende oder geplante Streams auslassen.
        if e.get("live_status") in ("is_live", "is_upcoming", "post_live"):
            continue
        url = e.get("url") or e.get("webpage_url")
        vid = str(e.get("id") or "")
        if c["platform"] == "twitch":
            vid = vid.lstrip("v")
            url = url or f"https://www.twitch.tv/videos/{vid}"
        elif not url or not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={vid}"
        if vid:
            vods.append({"id": vid, "url": url, "title": e.get("title") or "",
                         "duration": e.get("duration")})
    return vods[:limit]


def is_live(c: dict) -> bool:
    """Twitch: laeuft der Stream gerade? Dann waechst das neueste VOD noch."""
    if c["platform"] != "twitch":
        return False            # YouTube markiert Live-Streams in der Liste selbst
    try:
        with _ydl({}) as ydl:
            info = ydl.extract_info(f"https://www.twitch.tv/{c['name']}", download=False)
        return bool(info and info.get("is_live"))
    except Exception:           # "is offline" wirft einen Fehler
        return False


# ---------------------------------------------------------------------------
# Zustand
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"channels": {}, "vods": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Ablauf
# ---------------------------------------------------------------------------

def run_clipper(vod_url: str, out_dir: Path, extra: list[str]) -> int:
    cmd = [sys.executable, str(CLIPPER), vod_url, "--out", str(out_dir), *extra]
    return subprocess.run(cmd).returncode


def check(
    channels: list[dict],
    state: dict,
    *,
    out: Path,
    limit: int,
    first_run: int,
    dry_run: bool,
    extra: list[str],
    match: str | None = None,
    lister: Callable[[dict, int], list[dict]] = list_vods,
    live_check: Callable[[dict], bool] = is_live,
    clipper: Callable[[str, Path, list[str]], int] = run_clipper,
    save: Callable[[], None] = lambda: None,
) -> dict:
    """Prueft alle Kanaele. Gibt eine Zusammenfassung zurueck."""
    summary = {"new": 0, "ok": 0, "no_highlights": 0, "failed": 0, "skipped_live": 0,
               "list_failed": 0}
    for c in channels:
        label = channel_label(c)
        print(f"\n== {c['platform']}: {c['name']}", flush=True)
        try:
            vods = lister(c, limit)
        except Exception as e:  # Kanal down, Netz weg ...: andere Kanaele trotzdem pruefen
            msg = str(e).splitlines()[0].split("; please report")[0][:200]
            print(f"  ! Liste konnte nicht geladen werden: {msg}", flush=True)
            summary["list_failed"] += 1
            continue
        if vods and live_check(c):
            print(f"  laeuft gerade live, neuestes VOD {vods[0]['id']} kommt morgen dran")
            summary["skipped_live"] += 1
            vods = vods[1:]

        first_time = label not in state["channels"]
        todo = []
        for i, v in enumerate(vods):
            rec = state["vods"].get(v["id"])
            if rec and (rec["status"] in ("ok", "no_highlights", "seen")
                        or rec.get("attempts", 0) >= MAX_ATTEMPTS):
                continue
            if match and not re.search(match, v["title"], re.I):
                # Anderes Spiel / Just Chatting: nicht clippen.
                print(f"  uebersprungen (Titel passt nicht): {v['title'][:60]}")
                if not dry_run:
                    state["vods"][v["id"]] = {"channel": label, "title": v["title"],
                                              "status": "seen", "at": now_iso()}
                continue
            if first_time and i >= first_run:
                # Beim ersten Lauf nicht das ganze Archiv nachholen.
                if not dry_run:
                    state["vods"][v["id"]] = {"channel": label, "title": v["title"],
                                              "status": "seen", "at": now_iso()}
                continue
            todo.append(v)

        if not dry_run:
            state["channels"].setdefault(label, {"platform": c["platform"], "name": c["name"]})
            state["channels"][label]["checked"] = now_iso()
            save()

        if not todo:
            print("  nichts Neues")
            continue
        for v in reversed(todo):            # aelteste zuerst
            summary["new"] += 1
            dur = f" ({int(v['duration']) // 60} min)" if v.get("duration") else ""
            print(f"  neu: {v['id']} {v['title'][:60]}{dur}", flush=True)
            if dry_run:
                continue
            rc = clipper(v["url"], out / label, extra)
            prev = state["vods"].get(v["id"], {})
            status = {0: "ok", EXIT_NO_HIGHLIGHTS: "no_highlights"}.get(rc, "failed")
            state["vods"][v["id"]] = {
                "channel": label, "title": v["title"], "url": v["url"], "status": status,
                "attempts": prev.get("attempts", 0) + 1, "at": now_iso(),
                "clips": str(out / label / v["id"]),
            }
            summary[status] += 1
            save()                          # nach jedem VOD, falls der Lauf abbricht
            print(f"  -> {status}", flush=True)
    return summary


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]

    p = argparse.ArgumentParser(
        description="Prueft neue VODs deiner Streamer und startet gta_clipper.py dafuer.",
        epilog="Alles nach -- geht an gta_clipper.py, z.B.: -- --top 5 --threshold 2.5",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--streamers", type=Path, default=HERE / "streamers.txt",
                   help="Datei mit einem Kanal pro Zeile")
    p.add_argument("--out", type=Path, default=Path("clips"), help="Ausgabeordner")
    p.add_argument("--limit", type=int, default=5,
                   help="So viele neueste VODs pro Kanal anschauen")
    p.add_argument("--first-run", type=int, default=1,
                   help="Beim ersten Lauf eines Kanals nur so viele VODs clippen")
    p.add_argument("--match", metavar="REGEX",
                   help='Nur VODs clippen, deren Titel passt, z.B. "gta ?(6|vi)"')
    p.add_argument("--dry-run", action="store_true",
                   help="Nur anzeigen, was neu ist; nichts clippen, Zustand nicht aendern")
    args = p.parse_args(argv)

    if not args.streamers.exists():
        sys.exit(f"{args.streamers} fehlt. Kopier streamers.example.txt und trag deine Kanaele ein.")
    channels = load_channels(args.streamers)
    if not channels:
        sys.exit(f"Keine Kanaele in {args.streamers}.")

    state_path = args.out / "state.json"
    state = load_state(state_path)
    print(f"[{now_iso()}] pruefe {len(channels)} Kanaele", flush=True)
    s = check(channels, state, out=args.out, limit=args.limit, first_run=args.first_run,
              dry_run=args.dry_run, extra=extra, match=args.match,
              save=lambda: save_state(state_path, state))
    print(f"\nFertig: {s['new']} neu, {s['ok']} geclippt, {s['no_highlights']} ohne Highlight, "
          f"{s['failed']} fehlgeschlagen, {s['skipped_live']} gerade live, "
          f"{s['list_failed']} Kanaele nicht erreichbar.")
    return 1 if s["failed"] or s["list_failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
