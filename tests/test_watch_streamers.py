"""Tests fuer watch_streamers.py mit Fake-Kanaelen (kein Netzwerk noetig)."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gta_clipper  # noqa: E402
import watch_streamers as ws  # noqa: E402


def vod(i):
    return {"id": str(i), "url": f"https://www.twitch.tv/videos/{i}", "title": f"Stream {i}",
            "duration": 4 * 3600}


class FakeWorld:
    def __init__(self, vods, live=False, rc=0):
        self.vods = vods            # neuestes zuerst
        self.live = live
        self.rc = rc                # Exit-Code des Clippers, int oder dict id->int
        self.clipped = []

    def lister(self, c, limit):
        return self.vods[:limit]

    def live_check(self, c):
        return self.live

    def clipper(self, url, out, extra):
        vid = url.rsplit("/", 1)[-1]
        self.clipped.append(vid)
        return self.rc.get(vid, 0) if isinstance(self.rc, dict) else self.rc


CH = [{"platform": "twitch", "name": "streamer"}]


def run(world, state, **kw):
    opts = dict(out=Path("clips"), limit=5, first_run=1, dry_run=False, extra=[])
    opts.update(kw)
    return ws.check(CH, state, lister=world.lister, live_check=world.live_check,
                    clipper=world.clipper, **opts)


class TestCheck(unittest.TestCase):
    def test_exit_code_matches_clipper(self):
        self.assertEqual(ws.EXIT_NO_HIGHLIGHTS, gta_clipper.EXIT_NO_HIGHLIGHTS)

    def test_first_run_only_newest_then_only_new(self):
        state = {"channels": {}, "vods": {}}
        w = FakeWorld([vod(3), vod(2), vod(1)])
        run(w, state)
        self.assertEqual(w.clipped, ["3"])              # Archiv nicht nachholen
        self.assertEqual(state["vods"]["1"]["status"], "seen")

        w.vods = [vod(5), vod(4), vod(3), vod(2), vod(1)]   # naechster Tag
        w.clipped = []
        s = run(w, state)
        self.assertEqual(w.clipped, ["4", "5"])         # aelteste zuerst, nichts doppelt
        self.assertEqual(s["ok"], 2)

        w.clipped = []
        run(w, state)
        self.assertEqual(w.clipped, [])

    def test_live_stream_waits(self):
        state = {"channels": {"streamer": {}}, "vods": {}}
        w = FakeWorld([vod(2), vod(1)], live=True)
        s = run(w, state)
        self.assertEqual(w.clipped, ["1"])              # laufendes VOD 2 bleibt liegen
        self.assertEqual(s["skipped_live"], 1)
        w.live, w.clipped = False, []
        run(w, state)
        self.assertEqual(w.clipped, ["2"])

    def test_failed_retried_until_limit_no_highlights_not(self):
        state = {"channels": {"streamer": {}}, "vods": {}}
        w = FakeWorld([vod(2), vod(1)], rc={"2": 1, "1": ws.EXIT_NO_HIGHLIGHTS})
        for _ in range(5):
            run(w, state)
        self.assertEqual(w.clipped.count("2"), ws.MAX_ATTEMPTS)
        self.assertEqual(w.clipped.count("1"), 1)
        self.assertEqual(state["vods"]["1"]["status"], "no_highlights")

    def test_title_filter(self):
        state = {"channels": {"streamer": {}}, "vods": {}}
        vods = [vod(3), vod(2), vod(1)]
        vods[0]["title"] = "GTA 6 DAY 1 !!!"
        vods[1]["title"] = "Just Chatting"
        vods[2]["title"] = "gta vi weiter"
        w = FakeWorld(vods)
        run(w, state, match=r"gta ?(6|vi)")
        self.assertEqual(w.clipped, ["1", "3"])
        self.assertEqual(state["vods"]["2"]["status"], "seen")

    def test_dry_run_changes_nothing(self):
        state = {"channels": {}, "vods": {}}
        w = FakeWorld([vod(2), vod(1)])
        s = run(w, state, dry_run=True)
        self.assertEqual(w.clipped, [])
        self.assertEqual(s["new"], 1)
        self.assertEqual(state, {"channels": {}, "vods": {}})

    def test_broken_channel_does_not_stop_others(self):
        state = {"channels": {}, "vods": {}}
        w = FakeWorld([vod(1)])

        def lister(c, limit):
            if c["name"] == "kaputt":
                raise RuntimeError("404")
            return w.lister(c, limit)
        chans = [{"platform": "twitch", "name": "kaputt"}, {"platform": "twitch", "name": "ok"}]
        ws.check(chans, state, out=Path("clips"), limit=5, first_run=1, dry_run=False,
                 extra=[], lister=lister, live_check=w.live_check, clipper=w.clipper)
        self.assertEqual(w.clipped, ["1"])


class TestChannels(unittest.TestCase):
    def test_parse(self):
        p = ws.parse_channel
        self.assertEqual(p("Streamer_1"), {"platform": "twitch", "name": "streamer_1"})
        self.assertEqual(p("https://www.twitch.tv/Streamer/videos"),
                         {"platform": "twitch", "name": "streamer"})
        self.assertEqual(p("yt:@Kanal"), {"platform": "youtube", "name": "@Kanal"})
        self.assertEqual(p("youtube:Kanal"), {"platform": "youtube", "name": "@Kanal"})
        self.assertEqual(p("https://www.youtube.com/@Kanal/streams"),
                         {"platform": "youtube", "name": "@Kanal"})
        self.assertIsNone(p("  # Kommentar"))
        with self.assertRaises(ValueError):
            p("das ist kein kanal")

    def test_example_file_parses(self):
        chans = ws.load_channels(ROOT / "streamers.example.txt")
        self.assertEqual(chans, [{"platform": "twitch", "name": "beispielstreamer"}])

    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "state.json"
            st = {"channels": {"a": {}}, "vods": {"1": {"status": "ok"}}}
            ws.save_state(path, st)
            self.assertEqual(ws.load_state(path), st)


if __name__ == "__main__":
    unittest.main()
