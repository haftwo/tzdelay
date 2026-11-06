# tzdelay: timezone delay for radio streams

Takes a live audio feed, holds it, and plays it out again a set time later
(for example, the Sydney network feed delayed 3 hours for Perth).

No Python packages to install. You need Python 3.11+ and FFmpeg.

## Run it

```bash
cp config.example.json config.json    # set data_dir and port
python3 -m tzdelay --config config.json
```

Open http://localhost:8080. On that page you can:

- **Add a station:** enter a name, the HLS URL and the delay (hours,
  minutes, seconds, or a preset such as 30 min or 3 h). The URL is fetched
  and checked before it's saved, so a typo shows up straight away.
- **Watch status:** each station shows On air, Filling (with a countdown to
  on air), or Input error/stalled, plus buffer depth and gaps. It refreshes
  every 3 seconds.
- **Copy the output URL:** give `http://<host>:8080/channels/<id>/delayed.m3u8`
  to the player or playout system. Until the buffer holds a full delay's
  worth of audio it returns HTTP 503.
- **Listen:** plays the delayed output in the browser.
- **Edit:** change the name, delay or source URL. Switching to a mirror of
  the same stream (for example a backup CDN URL) is seamless.
- **Delete:** removes the station and its buffered audio.

Stations are saved to `<data_dir>/channels.json`. Channels listed in
`config.json` are only used to seed that file on the very first run.

The in-browser player loads hls.js from cdn.jsdelivr.net, so it needs
internet access (Safari plays HLS natively without it). Everything else
works offline.

## Docker

### Automatic builds from GitHub (recommended)

Every push to `main` builds the image on GitHub and pushes it, for both
Intel/AMD and ARM:

- to `ghcr.io/<github-user>/tzdelay`, with no setup needed;
- to Docker Hub as `<dockerhub-user>/tzdelay`, once you add two repository
  secrets (Settings > Secrets and variables > Actions): `DOCKERHUB_USERNAME`
  and `DOCKERHUB_TOKEN` (a Docker Hub access token with Read & Write).

Tags: `latest` from `main`, `0.3` from a git tag `v0.3`, plus a short commit
id. Progress is in the repo's Actions tab.

### Push to Docker Hub by hand

On a computer with Docker Desktop running (Mac, Windows or Linux):

```bash
./push.sh <your-dockerhub-username>        # tags :0.2 and :latest
./push.sh <your-dockerhub-username> 0.3    # a later version
```

It asks for your Docker Hub password. Use an access token (Docker Hub >
Account settings > Personal access tokens, with Read & Write), not your
account password.

It builds for both Intel/AMD and ARM. That matters if you build on an
Apple silicon Mac: a plain `docker build` there makes an ARM-only image
that won't start on Unraid.

The repository is created on the first push, and is public by default.
Make it private in Docker Hub if you prefer; Unraid then needs
`docker login` once before it can pull.

### Run on Unraid

1. Copy `unraid/my-tzdelay.xml` to the flash drive at
   `/boot/config/plugins/dockerMan/templates-user/`, and replace
   `YOUR_DOCKERHUB_USERNAME` in it.
2. Docker tab > Add Container > Template: pick **tzdelay** > Apply.
3. Click the container icon > WebUI to open the page.

Or from the Unraid terminal:

```bash
docker run -d --name tzdelay -p 8080:8080 \
  -v /mnt/user/appdata/tzdelay:/data \
  --restart unless-stopped <your-dockerhub-username>/tzdelay:latest
```

No config file is needed; stations live in `/data`. Keep `/data` on
persistent storage, because the buffer survives restarts, so the output
comes straight back instead of going silent for the full delay. Storage is
small, about 58 MB per hour of delay per station at 128 kbps.

To update: push a new version, then in Unraid click the container > Check
for updates (or `docker pull` and recreate). Stations and buffered audio
are kept.

Settings can also be given as environment variables: `TZDELAY_PORT`,
`TZDELAY_DATA_DIR`, `TZDELAY_LOG_LEVEL`. For sound card input add
`--device /dev/snd`; for Livewire/AES67 use `--network host`.

## Changing the delay on air

Changing the delay takes effect immediately and survives restarts. Shortening the
delay skips audio forward. Lengthening it repeats a section, or the output
waits for the buffer. Do it at a natural break such as the top of the hour.
That matters for daylight saving, where Sydney to Perth moves between 3 hours
and 2 hours.

## Inputs

| `ingest.type` | What it does |
|---|---|
| `hls` | Polls a live `.m3u8` (master or media playlist) and stores segments byte for byte, without re-encoding. Uses `EXT-X-PROGRAM-DATE-TIME` when present. |
| `capture` + `"input": "test"` | 1 kHz tone, for testing without hardware |
| `capture` + `"input": "alsa"` | Sound card (AES3 card or line input), e.g. `"device": "hw:1,0"`. In Docker add `--device /dev/snd`. List devices with `arecord -l`. |
| `capture` + `"input": "aes67"` | Livewire+/AES67 multicast stream described by an SDP file. The host must be on the audio network (`--network host` in Docker). |
| `capture` + `"input": "custom"` | Any FFmpeg input arguments in `"ffmpeg_input": [...]` |

Capture inputs are encoded to AAC (256 kbps by default) in short segments,
then handled exactly like HLS from that point on.

## How it works

```
input plug-in ──> delay buffer (segment files + SQLite index) ──> delayed playlist
 (hls / capture)     stamped on a continuous timeline              segments whose time
                                                                   <= now - delay
```

- `tzdelay/ingest/`: one file per input type. To add an input, subclass
  `Ingest` and register it in `ingest/__init__.py`.
- `tzdelay/buffer.py`: stores segments and gives each one a timeline
  timestamp. Gaps (dropouts, restarts) are flagged so players handle them
  with `EXT-X-DISCONTINUITY` rather than glitching.
- `tzdelay/server.py`: delayed playlist, API and the web page.
  `EXT-X-PROGRAM-DATE-TIME` in the output is the time the audio plays out at
  the delayed site.
- `tzdelay/web/index.html`: the interface, a single page with no build step.

## API (what the web page uses)

| Request | Body | Does |
|---|---|---|
| `GET /api/channels` | | Status of every station (also at `/api/status`) |
| `POST /api/channels` | `{"name", "url", "delay_seconds"}` | Add a station |
| `PUT /api/channels/<id>` | any of `{"name", "url", "delay_seconds"}` | Edit |
| `DELETE /api/channels/<id>` | | Remove a station and its buffer |

Errors come back as `{"error": "..."}` with HTTP 400.

For monitoring, alarm on `ingest_state != "ok"` or `last_segment_age > 30`.

## Known limits (v1)

- Encrypted HLS (`EXT-X-KEY`) is not supported yet.
- The output is HLS only. Feeding a sound card, AES3 or Livewire *output*
  needs a playout process (FFmpeg reading `delayed.m3u8`), and on-air use
  needs clock locking, covered in the next step.
- One ingest per channel, so there is no redundant second path yet.
- ID3 and SCTE-35 cue markers are not passed through.
- The web page and API have no login. Anyone who can reach port 8080 can
  add, change or delete stations, so keep it on a trusted network.
- Stations added in the web page are HLS only. Sound card and Livewire
  inputs are still set up in `config.json` (on first run) or
  `channels.json`; once running they show and can be edited like any other.
