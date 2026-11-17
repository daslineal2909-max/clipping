"""Tests fuer gta_clipper.py mit simuliertem Chat (kein Netzwerk noetig).

Ausfuehren:  python -m unittest discover -s tests -v
"""

import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gta_clipper as gc  # noqa: E402

NORMAL = ["hi", "wie gehts", "welcher server ist das", "nice auto", "lol", "gg",
          "was geht chat", "hahah", "?", "wann kommt der heist", "ok", "true",
          "der cop ist lost", "first time chat", "musik song?", "<3"]
HYPE = ["KEKW", "KEKW KEKW KEKW", "LUL", "OMEGALUL", "💀💀💀", "CLIP IT", "clip that",
        "NO WAY", "?????", "LMAO", "ICANT", "W", "😂😂"]


def simulate_chat(hours=4.0, highlights=(2700, 6900, 11700), seed=1,
                  burst_rate=4.0, burst_len=40, decoys=()):
    """Simuliert einen Stream-Chat.

    - Grundrauschen steigt im Laufe des Streams von ~0.3 auf ~0.9 Nachrichten/s
      und hat langsame Wellen (Chat wird mal ruhiger, mal aktiver).
    - An jedem Highlight explodiert der Chat fuer burst_len Sekunden,
      ~10 s nach dem Ereignis (Chat-Verzoegerung), vor allem mit Hype-Nachrichten.
    - Decoys: kleinere Aktivitaetsschuebe ohne Hype (z.B. Frage an den Chat).
    """
    rng = random.Random(seed)
    dur = int(hours * 3600)
    msgs = []
    for s in range(dur):
        base = 0.3 + 0.6 * s / dur
        base *= 1.0 + 0.3 * ((s // 900) % 2)         # ruhige/aktive Phasen
        n = _poisson(rng, base)
        for _ in range(n):
            msgs.append({"time_in_seconds": s + rng.random(),
                         "message": rng.choice(NORMAL)})
    for h in highlights:
        for s in range(h + 10, h + 10 + burst_len):
            for _ in range(_poisson(rng, burst_rate)):
                text = rng.choice(HYPE) if rng.random() < 0.7 else rng.choice(NORMAL)
                msgs.append({"time_in_seconds": s + rng.random(), "message": text})
    for d in decoys:
        for s in range(d, d + 30):
            for _ in range(_poisson(rng, 1.2)):
                msgs.append({"time_in_seconds": s + rng.random(), "message": rng.choice(NORMAL)})
    msgs.sort(key=lambda m: m["time_in_seconds"])
    return msgs


def _poisson(rng, lam):
    # Knuth, reicht fuer kleine lambda
    import math
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def to_messages(raw):
    return [gc.ChatMessage(t=m["time_in_seconds"], text=m["message"]) for m in raw]


class TestHype(unittest.TestCase):
    def test_hype_detected(self):
        for t in ["KEKW", "lul", "OMEGALUL", "💀", "CLIP IT", "clip that", "?????",
                  "NO WAY", "W", "LMAO", "digga was"]:
            self.assertEqual(gc.hype_weight(t), 1.0, t)

    def test_normal_not_hype(self):
        for t in ["hi", "wie gehts", "welcher server", "what", "wo bist du", ""]:
            self.assertEqual(gc.hype_weight(t), 0.0, t)


class TestDetection(unittest.TestCase):
    def assert_found(self, found, expected, tol=60):
        for e in expected:
            self.assertTrue(any(abs(h.peak - e) <= tol for h in found),
                            f"Highlight bei {e}s nicht gefunden: {[h.peak for h in found]}")

    def test_finds_all_three_highlights_in_4h(self):
        expected = (2700, 6900, 11700)
        for seed in range(5):
            msgs = to_messages(simulate_chat(highlights=expected, seed=seed))
            found = gc.find_highlights(msgs, top=8)
            self.assert_found(found, expected)
            # Die drei echten Highlights sind die drei staerksten.
            top3 = sorted(found, key=lambda h: h.factor, reverse=True)[:3]
            self.assert_found(top3, expected)

    def test_no_false_positives_without_highlights(self):
        # Reines Grundrauschen: weder Standard (3.0) noch der Tipp 2.5 schlagen an.
        for seed in range(20):
            msgs = to_messages(simulate_chat(highlights=(), seed=seed))
            for thr in (3.0, 2.5):
                found = gc.find_highlights(msgs, top=8, threshold=thr)
                self.assertEqual(found, [], (seed, thr, [(h.peak, h.factor) for h in found]))

    def test_hype_beats_plain_activity(self):
        # Decoys: Chat wird lebhafter, aber ohne Lachen/Clip-Rufe. Echte Highlights
        # muessen davor landen.
        msgs = to_messages(simulate_chat(highlights=(5000, 11000), decoys=(2000, 9000), seed=3))
        found = gc.find_highlights(msgs, top=2)
        self.assert_found(found, (5000, 11000))

    def test_lower_threshold_finds_weaker_highlight(self):
        # Kurzer, schwacher Ausschlag: 1.5 Nachrichten/s extra fuer 25 s.
        raw = simulate_chat(highlights=(3000,), seed=7, burst_rate=1.5, burst_len=25)
        msgs = to_messages(raw)
        strict = gc.find_highlights(msgs, threshold=6.0)
        loose = gc.find_highlights(msgs, threshold=2.5)
        self.assertFalse(any(abs(h.peak - 3000) <= 60 for h in strict))
        self.assert_found(loose, (3000,))

    def test_min_gap_and_top(self):
        msgs = to_messages(simulate_chat(highlights=(2000, 2100, 6000, 9000), seed=2))
        found = gc.find_highlights(msgs, top=2, min_gap=180)
        self.assertEqual(len(found), 2)
        self.assertGreaterEqual(abs(found[0].peak - found[1].peak), 180)

    def test_clip_range_starts_before_spike(self):
        msgs = to_messages(simulate_chat(highlights=(4000,), seed=4))
        found = gc.find_highlights(msgs)
        gc.attach_clip_ranges(found, pre=90, post=30, window=30, duration=msgs[-1].t)
        h = next(h for h in found if abs(h.peak - 4000) <= 60)
        self.assertEqual(h.start, h.peak - 90)
        self.assertLess(h.start, 4000)         # Ereignis liegt im Clip
        self.assertGreater(h.end, 4000 + 10)   # Chat-Reaktion liegt im Clip

    def test_top_messages(self):
        msgs = to_messages(simulate_chat(highlights=(4000,), seed=5))
        found = gc.find_highlights(msgs)
        gc.attach_top_messages(found, msgs, 30)
        texts = [t.lower() for t, _ in found[0].top_messages]
        self.assertTrue(any(t in texts for t in ("kekw", "lul", "omegalul", "clip it")), texts)


class TestFormats(unittest.TestCase):
    def test_video_id(self):
        self.assertEqual(gc.video_id_from_url("https://www.twitch.tv/videos/2283746123"),
                         "2283746123")
        self.assertEqual(gc.video_id_from_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1"),
                         "dQw4w9WgXcQ")
        self.assertEqual(gc.video_id_from_url("https://youtu.be/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(gc.video_id_from_url("https://www.youtube.com/live/dQw4w9WgXcQ"),
                         "dQw4w9WgXcQ")

    def test_load_formats(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.json").write_text(json.dumps(
                [{"time_in_seconds": 5, "message": "KEKW"},
                 {"time_in_seconds": -3, "message": "vor dem Start"}]))
            (d / "b.json").write_text(json.dumps(
                {"comments": [{"content_offset_seconds": 7.5, "message": {"body": "LUL"}}]}))
            (d / "c.jsonl").write_text('{"time_in_seconds": 1, "message": "hi"}\n'
                                       '{"time_in_seconds": 2, "message": "yo"}\n')
            self.assertEqual([(m.t, m.text) for m in gc.load_chat_file(d / "a.json")],
                             [(5.0, "KEKW")])
            self.assertEqual([(m.t, m.text) for m in gc.load_chat_file(d / "b.json")],
                             [(7.5, "LUL")])
            self.assertEqual(len(gc.load_chat_file(d / "c.jsonl")), 2)


class TestCli(unittest.TestCase):
    def test_dry_run_with_chat_file(self):
        with tempfile.TemporaryDirectory() as d:
            chat = Path(d) / "chat.json"
            chat.write_text(json.dumps(simulate_chat(seed=11)), encoding="utf-8")
            r = subprocess.run(
                [sys.executable, str(ROOT / "gta_clipper.py"),
                 "https://www.twitch.tv/videos/123", "--chat-file", str(chat),
                 "--top", "8", "--dry-run", "--out", str(Path(d) / "clips")],
                capture_output=True, text=True, encoding="utf-8",
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("3 Highlights", r.stdout)
            self.assertIn("0:45:00", r.stdout)
            self.assertIn("--dry-run", r.stdout)
            self.assertFalse((Path(d) / "clips").exists())


if __name__ == "__main__":
    unittest.main()
