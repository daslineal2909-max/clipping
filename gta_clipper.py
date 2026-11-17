#!/usr/bin/env python3
"""gta_clipper.py - findet Chat-Highlights in Twitch-/YouTube-VODs und schneidet Rohclips.

Ablauf:
  1. Nur den Chat des VODs laden (chat-downloader) oder eine gespeicherte Chat-Datei lesen.
  2. Nachrichten pro Fenster (Standard 30 s) zaehlen. Hype-Nachrichten (KEKW, LUL, 💀,
     "CLIP IT", ...) zaehlen extra.
  3. Fenster, die deutlich ueber dem lokalen Grundrauschen liegen, werden zu Highlights.
  4. Nur diese Ausschnitte mit yt-dlp laden (nicht das ganze VOD). Jeder Clip beginnt
     --pre Sekunden vor dem Spike, weil der Chat verspaetet reagiert.
  5. clips.json mit Zeitpunkten, Scores und den haeufigsten Chat-Nachrichten schreiben.

Beispiele:
  python gta_clipper.py https://www.twitch.tv/videos/123456789 --top 8 --dry-run
  python gta_clipper.py https://www.twitch.tv/videos/123456789 --top 8
  python gta_clipper.py <URL> --chat-file chat.json --threshold 2.5
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median
from typing import Iterable

# ---------------------------------------------------------------------------
# Hype-Erkennung
# ---------------------------------------------------------------------------

# Einzelne Woerter/Emotes, die auf einen lustigen oder krassen Moment hindeuten.
HYPE_TOKENS = {
    # Lachen
    "kekw", "lul", "lulw", "omegalul", "lmao", "lmfao", "lol", "xd", "xdd", "icant",
    "pepelaugh", "kek", "haha", "hahaha", "kappa", "aware", "deadge",
    # Hype / Schock
    "pog", "pogu", "poggers", "pogchamp", "pogcrazy", "monkas", "monkaw", "omg",
    "wtf", "holy", "sheesh", "wow", "ez", "clap", "gg", "insane", "noway", "nowaying",
    # Deutsch
    "digga", "alter", "krass", "brutal", "safe", "ehrenlos",
}

# Emojis, die als Hype zaehlen.
HYPE_EMOJIS = ("💀", "😂", "🤣", "😭", "🔥", "😳", "🤯", "😱")

# Ganze Phrasen (Regex), meistens Clip-Aufforderungen.
HYPE_PATTERNS = [
    re.compile(r"\bclip\s*(it|that|das|dat)\b", re.IGNORECASE),
    re.compile(r"\bclipp?(ed|t)\b", re.IGNORECASE),
    re.compile(r"\bno\s*way\b", re.IGNORECASE),
    re.compile(r"\?{3,}"),       # "?????"
    re.compile(r"!{3,}"),        # "!!!!!"
    re.compile(r"\b(ha){3,}\b", re.IGNORECASE),
    re.compile(r"\bW+\b"),       # "W" / "WWWW" als Hype-Reaktion (gross geschrieben)
    re.compile(r"\bL+\b"),       # "L" / "LLLL" als Fail-Reaktion (gross geschrieben)
]

# Exit-Code, wenn der Chat geladen wurde, aber kein Moment ueber der Schwelle liegt.
# Kein Fehler: watch_streamers.py versucht so ein VOD nicht erneut.
EXIT_NO_HIGHLIGHTS = 3

TOKEN_RE = re.compile(r"[\w']+", re.UNICODE)


def hype_weight(text: str) -> float:
    """Gibt 1.0 zurueck, wenn die Nachricht nach Hype aussieht, sonst 0.0."""
    if not text:
        return 0.0
    if any(e in text for e in HYPE_EMOJIS):
        return 1.0
    tokens = {t.lower() for t in TOKEN_RE.findall(text)}
    if tokens & HYPE_TOKENS:
        return 1.0
    if any(p.search(text) for p in HYPE_PATTERNS):
        return 1.0
    return 0.0


# ---------------------------------------------------------------------------
# Chat laden
# ---------------------------------------------------------------------------

@dataclass
class ChatMessage:
    t: float      # Sekunden seit VOD-Start
    text: str


def _extract(obj: dict) -> ChatMessage | None:
    """Versteht chat-downloader- und TwitchDownloader-Formate."""
    t = obj.get("time_in_seconds")
    if t is None:
        t = obj.get("content_offset_seconds")       # TwitchDownloaderCLI
    if t is None and "t" in obj:
        t = obj["t"]                                # eigenes Cache-Format
    if t is None:
        return None
    msg = obj.get("message", obj.get("text", ""))
    if isinstance(msg, dict):                       # TwitchDownloaderCLI: {"body": ...}
        msg = msg.get("body", "")
    try:
        t = float(t)
    except (TypeError, ValueError):
        return None
    if t < 0:                                       # YouTube: Nachrichten vor Stream-Start
        return None
    return ChatMessage(t=t, text=str(msg or ""))


def load_chat_file(path: Path) -> list[ChatMessage]:
    """Liest JSON (Liste oder {"comments": [...]}) oder JSONL."""
    raw = path.read_text(encoding="utf-8")
    items: Iterable[dict]
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            items = data.get("comments") or data.get("messages") or []
        else:
            items = data
    except json.JSONDecodeError:
        items = [json.loads(line) for line in raw.splitlines() if line.strip()]
    msgs = [m for m in (_extract(o) for o in items if isinstance(o, dict)) if m]
    msgs.sort(key=lambda m: m.t)
    return msgs


def download_chat(url: str, cache: Path | None, verbose: bool = True) -> list[ChatMessage]:
    """Laedt nur den Chat des VODs. Speichert ihn optional als Cache."""
    try:
        from chat_downloader import ChatDownloader
    except ImportError:
        sys.exit("chat-downloader fehlt: pip install chat-downloader")

    msgs: list[ChatMessage] = []
    chat = ChatDownloader().get_chat(url, message_types=["text_message"])
    for i, item in enumerate(chat, 1):
        m = _extract(item)
        if m:
            msgs.append(m)
        if verbose and i % 5000 == 0:
            print(f"  ... {i} Nachrichten geladen (bei {fmt_ts(m.t if m else 0)})", file=sys.stderr)
    msgs.sort(key=lambda m: m.t)

    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps([{"t": m.t, "text": m.text} for m in msgs], ensure_ascii=False),
            encoding="utf-8",
        )
    return msgs


def video_id_from_url(url: str) -> str:
    for pat in (r"twitch\.tv/videos/(\d+)", r"[?&]v=([\w-]{11})", r"youtu\.be/([\w-]{11})",
                r"youtube\.com/(?:live|shorts)/([\w-]{11})"):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return re.sub(r"[^\w-]+", "_", url)[-40:].strip("_") or "vod"


# ---------------------------------------------------------------------------
# Highlight-Erkennung
# ---------------------------------------------------------------------------

@dataclass
class Highlight:
    peak: float               # Sekunde, an der das staerkste Fenster beginnt
    factor: float             # Score im Peak-Fenster / normaler Score drumherum
    z: float                  # Abstand zum Grundrauschen in Standardabweichungen
    score: float              # gewichtete Nachrichten im Peak-Fenster
    messages: int             # rohe Nachrichtenanzahl im Peak-Fenster
    hype: int                 # Hype-Nachrichten im Peak-Fenster
    start: float = 0.0        # Clip-Start
    end: float = 0.0          # Clip-Ende
    top_messages: list = field(default_factory=list)


def bucket_scores(msgs: list[ChatMessage], window: float, hype_bonus: float):
    """Zaehlt pro Fenster: Nachrichten, Hype-Nachrichten und den gewichteten Score."""
    if not msgs:
        return [], [], []
    n = int(msgs[-1].t // window) + 1
    counts = [0] * n
    hypes = [0] * n
    for m in msgs:
        i = int(m.t // window)
        counts[i] += 1
        if hype_weight(m.text):
            hypes[i] += 1
    scores = [c + hype_bonus * h for c, h in zip(counts, hypes)]
    return counts, hypes, scores


def local_baseline(scores: list[float], baseline_windows: int):
    """Normaler Score und Streuung rund um jedes Fenster.

    Der Chat wird im Laufe eines Streams oft aktiver. Deshalb vergleichen wir jedes
    Fenster mit seiner Umgebung (+- baseline_windows/2), nicht mit dem ganzen Stream.
    Median und MAD sind robust gegen die Spikes selbst.
    """
    n = len(scores)
    half = max(1, baseline_windows // 2)
    meds, sigmas = [], []
    for i in range(n):
        local = scores[max(0, i - half):min(n, i + half + 1)]
        med = median(local)
        mad = median(abs(s - med) for s in local)
        # Mindestens Poisson-Rauschen annehmen, damit ruhige Phasen nicht ausschlagen.
        sigmas.append(max(1.4826 * mad, math.sqrt(med), 1.0))
        meds.append(med)
    return meds, sigmas


def find_highlights(
    msgs: list[ChatMessage],
    *,
    window: float = 30.0,
    hype_bonus: float = 1.0,
    threshold: float = 3.0,
    min_z: float = 3.0,
    baseline: float = 1200.0,
    min_gap: float = 180.0,
    min_messages: int = 5,
    top: int = 8,
) -> list[Highlight]:
    """Findet Fenster, in denen der Chat `threshold`-mal so aktiv ist wie normal.

    Zusaetzlich muss der Ausschlag statistisch deutlich sein (z >= min_z) und das
    Fenster mindestens `min_messages` Nachrichten haben. So erzeugen ruhige Chats mit
    2 statt 0,5 Nachrichten keine Fehlalarme.
    """
    counts, hypes, scores = bucket_scores(msgs, window, hype_bonus)
    if not scores:
        return []
    meds, sigmas = local_baseline(scores, max(3, int(baseline // window)))
    # Untergrenze fuer den Normalwert, sonst waere 3 vs. 1 Nachricht schon "3x".
    floor = max(1.0, float(min_messages))
    factor = [s / max(m, floor) for s, m in zip(scores, meds)]
    z = [(s - m) / sd for s, m, sd in zip(scores, meds, sigmas)]

    def hot(i: int) -> bool:
        return factor[i] >= threshold and z[i] >= min_z and counts[i] >= min_messages

    # Zusammenhaengende Fenster ueber der Schwelle zu einem Ereignis zusammenfassen.
    events: list[Highlight] = []
    i, n = 0, len(scores)
    while i < n:
        if hot(i):
            j = i
            while j + 1 < n and hot(j + 1):
                j += 1
            k = max(range(i, j + 1), key=lambda x: scores[x])
            events.append(Highlight(
                peak=k * window, factor=round(factor[k], 2), z=round(z[k], 1),
                score=scores[k], messages=counts[k], hype=hypes[k],
            ))
            i = j + 1
        else:
            i += 1

    # Staerkste zuerst, Ereignisse zu nah an einem staerkeren verwerfen.
    events.sort(key=lambda h: (h.factor, h.z), reverse=True)
    picked: list[Highlight] = []
    for ev in events:
        if all(abs(ev.peak - p.peak) >= min_gap for p in picked):
            picked.append(ev)
        if len(picked) >= top:
            break
    return picked


def attach_clip_ranges(highlights: list[Highlight], pre: float, post: float,
                       window: float, duration: float | None) -> None:
    for h in highlights:
        # Spike-Mitte als Referenz: Clip startet pre Sekunden davor.
        h.start = max(0.0, h.peak - pre)
        h.end = h.peak + window + post
        if duration:
            h.end = min(h.end, duration)


def normalize_msg(text: str) -> str:
    t = re.sub(r"\s+", " ", text.strip())
    t = re.sub(r"(.)\1{3,}", r"\1\1\1", t)     # "KEKWWWWW" -> "KEKWWW"
    words = t.split(" ")
    # "KEKW KEKW KEKW" -> "KEKW"
    if len(words) > 1 and len(set(w.lower() for w in words)) == 1:
        t = words[0]
    return t[:80]


def attach_top_messages(highlights: list[Highlight], msgs: list[ChatMessage],
                        window: float, n: int = 8) -> None:
    """Haeufigste Nachrichten rund um den Spike, als Ideen fuer Titel."""
    times = [m.t for m in msgs]
    import bisect
    for h in highlights:
        lo = bisect.bisect_left(times, h.peak - window)
        hi = bisect.bisect_right(times, h.peak + 2 * window)
        c = Counter()
        display: dict[str, str] = {}
        for m in msgs[lo:hi]:
            norm = normalize_msg(m.text)
            if not norm:
                continue
            key = norm.lower()
            c[key] += 1
            display.setdefault(key, norm)
        h.top_messages = [[display[k], cnt] for k, cnt in c.most_common(n)]


# ---------------------------------------------------------------------------
# Clips schneiden
# ---------------------------------------------------------------------------

def fmt_ts(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def cut_clip(url: str, start: float, end: float, out_base: Path, fmt: str,
             precise: bool) -> Path | None:
    """Laedt nur den Abschnitt [start, end] mit yt-dlp."""
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg fehlt. yt-dlp braucht ffmpeg zum Schneiden.")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--quiet", "--no-warnings", "--progress",
        "-f", fmt,
        "--download-sections", f"*{start:.0f}-{end:.0f}",
        "--merge-output-format", "mp4",
        "-o", str(out_base) + ".%(ext)s",
        url,
    ]
    if precise:
        cmd.insert(-1, "--force-keyframes-at-cuts")
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        sys.exit("yt-dlp fehlt: pip install yt-dlp")
    except subprocess.CalledProcessError as e:
        print(f"  ! yt-dlp fehlgeschlagen (Exit {e.returncode})", file=sys.stderr)
        return None
    found = sorted(out_base.parent.glob(out_base.name + ".*"))
    return found[0] if found else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Findet Chat-Highlights in Twitch-/YouTube-VODs und schneidet Rohclips.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("url", help="VOD-URL (Twitch /videos/... oder YouTube-Stream)")
    p.add_argument("--top", type=int, default=8, help="maximale Anzahl Clips")
    p.add_argument("--threshold", type=float, default=3.0,
                   help="Chat muss so viel mal aktiver sein als normal. Tiefer = mehr Clips (z.B. 2.5)")
    p.add_argument("--min-z", type=float, default=3.0,
                   help="Mindest-Ausschlag in Standardabweichungen (Schutz vor Zufallsspitzen)")
    p.add_argument("--window", type=float, default=30.0, help="Fenstergroesse in Sekunden")
    p.add_argument("--hype-bonus", type=float, default=1.0,
                   help="Extra-Gewicht pro Hype-Nachricht (KEKW, 💀, CLIP IT, ...)")
    p.add_argument("--baseline", type=float, default=1200.0,
                   help="Sekunden Umgebung fuer das lokale Grundrauschen")
    p.add_argument("--min-gap", type=float, default=180.0,
                   help="Mindestabstand zwischen zwei Clips in Sekunden")
    p.add_argument("--min-messages", type=int, default=5,
                   help="Mindestanzahl Nachrichten im Peak-Fenster")
    p.add_argument("--pre", type=float, default=90.0,
                   help="Sekunden vor dem Spike (Chat reagiert verspaetet)")
    p.add_argument("--post", type=float, default=30.0, help="Sekunden nach dem Spike-Fenster")
    p.add_argument("--out", type=Path, default=Path("clips"), help="Ausgabeordner")
    p.add_argument("--chat-file", type=Path,
                   help="Chat aus Datei statt Download (chat-downloader- oder TwitchDownloader-JSON)")
    p.add_argument("--no-cache", action="store_true",
                   help="Chat nicht unter <out>/<id>/chat.json zwischenspeichern")
    p.add_argument("--format", default="bv*[height<=1080]+ba/b[height<=1080]/b",
                   help="yt-dlp-Formatauswahl")
    p.add_argument("--precise", action="store_true",
                   help="Framegenau schneiden (--force-keyframes-at-cuts, langsamer)")
    p.add_argument("--dry-run", action="store_true",
                   help="Nur Zeitpunkte anzeigen, nichts herunterladen")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    vid = video_id_from_url(args.url)
    out_dir = args.out / vid
    cache = out_dir / "chat.json"

    # 1. Chat
    if args.chat_file:
        print(f"Lese Chat aus {args.chat_file} ...", file=sys.stderr)
        msgs = load_chat_file(args.chat_file)
    elif cache.exists() and not args.no_cache:
        print(f"Nutze gespeicherten Chat {cache}", file=sys.stderr)
        msgs = load_chat_file(cache)
    else:
        print("Lade Chat (nur Chat, kein Video) ...", file=sys.stderr)
        msgs = download_chat(args.url, None if args.no_cache else cache)

    if not msgs:
        print("Keine Chat-Nachrichten gefunden. Hat das VOD einen Chat-Replay?", file=sys.stderr)
        return 1
    duration = msgs[-1].t
    print(f"{len(msgs)} Nachrichten ueber {fmt_ts(duration)}", file=sys.stderr)

    # 2. Highlights
    hl = find_highlights(
        msgs, window=args.window, hype_bonus=args.hype_bonus, threshold=args.threshold,
        min_z=args.min_z, baseline=args.baseline, min_gap=args.min_gap,
        min_messages=args.min_messages, top=args.top,
    )
    if not hl:
        print(f"Keine Highlights ueber Schwelle {args.threshold}. "
              f"Versuch --threshold {max(1.5, args.threshold - 0.5):.1f}", file=sys.stderr)
        return EXIT_NO_HIGHLIGHTS
    attach_clip_ranges(hl, args.pre, args.post, args.window, duration)
    attach_top_messages(hl, msgs, args.window)
    hl.sort(key=lambda h: h.peak)       # chronologisch ausgeben

    print(f"\n{len(hl)} Highlights:")
    print(f"{'#':>2}  {'Spike':>8}  {'Clip':>17}  {'Faktor':>6}  {'Msgs':>4}  {'Hype':>4}  Chat")
    for i, h in enumerate(hl, 1):
        chat = ", ".join(f"{t} ({c})" for t, c in h.top_messages[:3])
        clip = f"{fmt_ts(h.start)}-{fmt_ts(h.end)}"
        print(f"{i:>2}  {fmt_ts(h.peak):>8}  {clip:>17}  "
              f"{h.factor:>5.1f}x  {h.messages:>4}  {h.hype:>4}  {chat}")

    if args.dry_run:
        print("\n--dry-run: nichts heruntergeladen.")
        return 0

    # 3. Clips schneiden
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for i, h in enumerate(hl, 1):
        stamp = fmt_ts(h.peak).replace(":", "-")
        base = out_dir / f"clip_{i:02d}_{stamp}"
        print(f"\n[{i}/{len(hl)}] {fmt_ts(h.start)} - {fmt_ts(h.end)} -> {base.name}.mp4",
              file=sys.stderr)
        path = cut_clip(args.url, h.start, h.end, base, args.format, args.precise)
        entry = {"index": i, **asdict(h),
                 "start_ts": fmt_ts(h.start), "end_ts": fmt_ts(h.end),
                 "peak_ts": fmt_ts(h.peak), "file": str(path) if path else None}
        results.append(entry)

    manifest = out_dir / "clips.json"
    manifest.write_text(json.dumps(
        {"vod": args.url, "video_id": vid, "clips": results}, ensure_ascii=False, indent=2,
    ), encoding="utf-8")
    ok = sum(1 for r in results if r["file"])
    print(f"\n{ok}/{len(results)} Clips in {out_dir}, Details in {manifest}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
