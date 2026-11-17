# clipping

`gta_clipper.py` findet in einem Twitch- oder YouTube-VOD die Momente, in denen der Chat explodiert, und schneidet dort Rohclips raus. Aus diesen Rohclips macht [OpenShorts](https://github.com/mutonby/openshorts) dann fertige 9:16-Shorts.

## Schnellstart

Du brauchst Python 3.9+ und `ffmpeg`.

```bash
pip install -r requirements.txt
```

```bash
python gta_clipper.py https://www.twitch.tv/videos/123456789 --top 8 --dry-run
```

Mit `--dry-run` siehst du nur die gefundenen Zeitpunkte, es wird nichts runtergeladen:

```
3 Highlights:
 #     Spike               Clip  Faktor  Msgs  Hype  Chat
 1   0:45:30    0:44:00-0:46:30    9.1x    87    58  KEKW (17), 💀💀💀 (13), ??? (12)
 2   1:55:30    1:54:00-1:56:30    8.2x   124    72  KEKW (14), LUL (13), hahah (12)
 3   3:15:00    3:13:30-3:16:00    7.2x   129    86  KEKW (19), gg (16), ICANT (16)
```

Passt die Liste, startest du ohne `--dry-run`. Dann landen die Clips unter `clips/<video-id>/`:

```
clips/123456789/
  chat.json            # gespeicherter Chat, beim nächsten Lauf kein neuer Download
  clip_01_0-45-30.mp4
  clip_02_1-55-30.mp4
  ...
  clips.json           # Zeitpunkte, Scores und häufigste Chat-Nachrichten pro Clip
```

## So funktioniert die Erkennung

1. **Nur der Chat wird geladen** (mit `chat-downloader`), nicht das Video.
2. **Nachrichten pro 30 Sekunden zählen.** Hype-Nachrichten wie `KEKW`, `LUL`, `OMEGALUL`, `💀`, `CLIP IT`, `?????` oder `NO WAY` zählen doppelt.
3. **Vergleich mit dem normalen Chat.** Jedes Fenster wird mit den 20 Minuten drumherum verglichen, nicht mit dem ganzen Stream. Der Chat ist am Anfang eines Streams oft ruhiger als später, so geht kein Highlight verloren. Ein Fenster gilt als Highlight, wenn der Chat dort mindestens `--threshold`-mal so aktiv ist wie normal (Standard 3,0). Der Ausschlag muss außerdem deutlich über dem Zufallsrauschen liegen.
4. **Rohclips schneiden.** Mit `yt-dlp --download-sections` werden nur diese Ausschnitte geladen. Jeder Clip beginnt 90 Sekunden vor dem Spike, weil der Chat immer verspätet reagiert, und endet 60 Sekunden danach.
5. **`clips.json`** enthält pro Clip die häufigsten Chat-Nachrichten. Die kannst du als Ideen für Titel nehmen.

## Wichtige Optionen

| Option | Standard | Wofür |
|---|---|---|
| `--top N` | 8 | maximal so viele Clips |
| `--threshold X` | 3.0 | tiefer (z.B. `2.5`) = mehr Clips, höher = nur die krassesten Momente |
| `--pre S` | 90 | Sekunden vor dem Spike |
| `--post S` | 30 | Sekunden nach dem Spike-Fenster |
| `--min-gap S` | 180 | Mindestabstand zwischen zwei Clips |
| `--chat-file F` | | Chat aus einer Datei statt Download. Versteht das JSON von chat-downloader und von [TwitchDownloaderCLI](https://github.com/lay295/TwitchDownloader) |
| `--precise` | aus | framegenau schneiden, dafür langsamer |
| `--out DIR` | `clips` | Ausgabeordner |

Alle Optionen siehst du mit `python gta_clipper.py --help`.

**Wenn der Chat-Download scheitert:** Twitch ändert seine Schnittstellen öfter. Dann lädst du den Chat mit TwitchDownloaderCLI und gibst ihn mit `--chat-file` rein:

```bash
TwitchDownloaderCLI chatdownload --id 123456789 -o chat.json
```

```bash
python gta_clipper.py https://www.twitch.tv/videos/123456789 --chat-file chat.json --dry-run
```

## Täglicher Check: neue VODs automatisch clippen

`watch_streamers.py` prüft die Kanäle deiner Streamer, findet VODs, die noch nicht bearbeitet wurden, und startet für jedes `gta_clipper.py`.

1. Liste anlegen: Kopier `streamers.example.txt` nach `streamers.txt` und trag einen Kanal pro Zeile ein. Erlaubt sind Twitch-Namen, Twitch-URLs, `yt:@handle` und YouTube-Kanal-URLs.
2. Testen, ohne etwas zu clippen:

   ```bash
   python watch_streamers.py --dry-run
   ```

3. Echt laufen lassen:

   ```bash
   python watch_streamers.py
   ```

   Optionen für den Clipper hängst du nach `--` an:

   ```bash
   python watch_streamers.py -- --top 5 --threshold 2.5
   ```

Die Clips landen unter `clips/<kanal>/<video-id>/`. In `clips/state.json` steht, welche VODs schon erledigt sind. So wird nichts doppelt geclippt.

- **Beim ersten Lauf eines Kanals** wird nur das neueste VOD geclippt, nicht das ganze Archiv. Das kannst du mit `--first-run 3` ändern.
- **Läuft ein Twitch-Stream gerade**, wird das neueste VOD übersprungen, weil es noch wächst. Es kommt beim nächsten Lauf dran.
- **Fehlgeschlagene VODs** werden bei den nächsten Läufen erneut versucht, höchstens 3-mal. Ein VOD ohne Highlight gilt als erledigt.
- **Kanal nicht erreichbar:** Die anderen Kanäle werden trotzdem geprüft. Das Skript endet dann mit Exit-Code 1, damit du es im Log siehst.

### Jeden Tag automatisch starten

Der Rechner muss zu der Zeit an sein. Am besten passt ein Zeitpunkt am Vormittag, dann sind die Streams vom Vorabend fertig.

**Linux/macOS (cron):** Öffne mit `crontab -e` deine Crontab und füge diese Zeile ein. Den Pfad passt du an:

```
0 10 * * * cd /pfad/zu/clipping && python3 watch_streamers.py >> clips/watch.log 2>&1
```

**Windows (Aufgabenplanung):** In der Eingabeaufforderung, Pfad anpassen:

```bat
schtasks /Create /SC DAILY /ST 10:00 /TN "GTA Clipper" /TR "cmd /c cd /d C:\pfad\zu\clipping && python watch_streamers.py >> clips\watch.log 2>&1"
```

## Weiter mit OpenShorts

1. **Installieren:** Du brauchst Docker und mindestens 8 GB RAM.

   ```bash
   git clone https://github.com/mutonby/openshorts.git
   ```

   ```bash
   cd openshorts && docker compose up --build
   ```

   Das Dashboard läuft auf `http://localhost:5175`. Unter **Settings** trägst du einen Gemini-Key ein, den gibt es gratis bei [Google AI Studio](https://aistudio.google.com/app/apikey).
2. **Shorts machen:** Lade die Rohclips aus `clips/<video-id>/` im Dashboard hoch. Für die Automatisierung geht es auch über den MCP-Server:

   ```bash
   claude mcp add --transport http openshorts http://localhost:8000/mcp
   ```

   Danach sagst du einem Agent einfach „verarbeite alle Clips im Ordner clips/“.
3. **Posten:** Das läuft über [Upload-Post](https://www.upload-post.com). Gratis sind 10 Uploads pro Monat über alle Plattformen zusammen. Für eine tägliche Page reicht das nicht, dafür brauchst du einen bezahlten Plan.

## Tests

Die Tests laufen mit simuliertem Chat und brauchen kein Netz:

```bash
python -m unittest discover -s tests -v
```

Sie prüfen unter anderem: In einem simulierten 4-Stunden-Stream werden alle 3 eingebauten Highlights gefunden. Reines Grundrauschen ergibt keinen Clip, weder bei `--threshold 3.0` noch bei `2.5`. Hype-Momente landen vor Phasen, in denen der Chat nur ohne Lachen und Clip-Rufe lebhafter wird.
