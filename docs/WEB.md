# From the stage's sound to subtitles on phones (no OBS)

The simplest real deployment: the audience reads the subtitles on their
phones, and the only thing each stage has to provide is **its audio**. No
video, no streaming software.

```
microphones → mixing desk → [ audio feed ] → MediaMTX → conffy → web on phones
                              (this guide)
```

## 1. Where to take the audio from

Ask the sound engineer for a **speech feed from the mixer**:

- **An aux, matrix or record output**, with the stage microphones only. Room
  ambience and music hurt recognition; playback videos inside a talk are fine
  (they get transcribed too).
- **Mono is enough.** conffy converts everything to mono 16 kHz.
- **Line level**, peaking around −12 dBFS and never clipping. Distorted
  audio is the #1 cause of bad subtitles.

Avoid an extra room microphone pointed at the speakers: reverb and applause
make it far worse than the desk feed.

## 2. Ways to bring that audio into conffy

| Option | What you need at the stage | Works today | Notes |
|---|---|---|---|
| **A. A laptop with an audio input** | laptop + USB audio interface (or a mixer with a USB audio output, which shows up as an audio device) | yes | Cheapest and most flexible. `make mic` or one ffmpeg command. |
| **B. A hardware streaming encoder** | a box that pushes RTMP or SRT (common in venues) | yes | No computer at the stage: point it at MediaMTX. |
| **C. The stage's existing live stream** | nothing; the event already streams the room (e.g. YouTube Live) | yes | Zero extra hardware, but adds the stream's own delay (often 5–20 s). |
| **D. Network audio (Dante, AES67)** | a computer on the audio network with a virtual sound card | yes | Then it is option A with a network input. |

In every option the audio ends up in **MediaMTX**, the small media server that
runs next to conffy (`make up-obs`). The talk's source is
`rtmp://mediamtx:1935/<talk id>`.

## 3. Step by step (option A: laptop at the mixer)

### On the conffy server

```bash
./scripts/run-host.sh    # models (keep this terminal open)
make up-obs              # conffy + MediaMTX on port 1935
```

The stage laptop must reach the server on **port 1935**. Being on the same
network is enough; open the port in the server's firewall if needed. Find the
server's IP with `ipconfig getifaddr en0` (macOS).

### On the stage laptop

1. Connect the mixer output to the audio interface, and the interface to the
   laptop.
2. Find the input's device number:

   ```bash
   make mic-devices
   # [AVFoundation indev @ ...] AVFoundation audio devices:
   # [AVFoundation indev @ ...] [0] MacBook Pro Microphone
   # [AVFoundation indev @ ...] [1] Scarlett 2i2 USB
   ```

3. Start sending it. Choose the talk id now; it becomes the URL of the room.

   If this laptop **is** the conffy server:

   ```bash
   make mic DEVICE=1 ID=sala-2
   ```

   Otherwise, send it to the server's IP with ffmpeg:

   **macOS:**

   ```bash
   ffmpeg -f avfoundation -i ":1" -ac 1 -ar 48000 -c:a aac -b:a 96k \
     -f flv rtmp://<server-ip>:1935/sala-2
   ```

   **Linux** (list devices with `arecord -l`):

   ```bash
   ffmpeg -f alsa -i hw:1 -ac 1 -ar 48000 -c:a aac -b:a 96k \
     -f flv rtmp://<server-ip>:1935/sala-2
   ```

   **Windows** (list devices with
   `ffmpeg -list_devices true -f dshow -i dummy`):

   ```bash
   ffmpeg -f dshow -i audio="Line In (Scarlett 2i2 USB)" -ac 1 -ar 48000 -c:a aac -b:a 96k -f flv rtmp://<server-ip>:1935/sala-2
   ```

   Leave it running for the whole session.

### Back on the server: create the talk

Create it **after** the audio is flowing:

```bash
make room ID=sala-2 FROM=es TO=en TITLE="Sala 2: Arquitecturas serverless"
```

- `FROM`: the language spoken on stage.
- `TO`: the subtitle language.
- The talk's glossary defaults to `nerdearla` (`GLOSSARY=...` to change it).

### For the audience

Put a **QR code** at the door and on the screen, pointing to the room's viewer:

```
http://<server>:8080/?s=sala-2&lang=en
```

Anyone can switch languages on the page. For the QR code, any generator
works, e.g. `brew install qrencode` and then
`qrencode -o sala-2.png "http://<server>:8080/?s=sala-2&lang=en"`. In a real
event, the server should have a proper domain name and HTTPS in front of nginx.

### During the event

- `make watch` shows each room's latency; subtitles should stay about 1 s
  behind in the original language.
- **Between talks, keep the feed running.** The same talk id can carry a
  whole day of talks in one room: silence produces no subtitles.
- **If the laptop or the feed stops**, conffy marks the talk as finished.
  Start the feed again, then `make start ID=sala-2`. Viewers' pages recover by
  themselves.
- **At the end:** `/api/sessions/sala-2/export.srt?lang=es` gives the whole
  transcript.

## 4. Option B: a hardware encoder

Configure the encoder's RTMP destination:

- **Server:** `rtmp://<server-ip>:1935`
- **Stream key:** the talk id

Some encoders want the full URL: `rtmp://<server-ip>:1935/sala-2`. Start it,
then run `make room ID=sala-2 ...`. Audio-only streams are fine; so is a video
stream (conffy ignores the image).

For unreliable networks, **SRT** is more robust than RTMP. MediaMTX supports
it, but the compose file only exposes RTMP (1935). Publish MediaMTX's SRT port
(8890/udp) in `docker-compose.yml` to use it.

## 5. Option C: the stage's existing live stream

If the room is already streamed live (for example on YouTube), conffy can read
that stream directly. Get the real stream URL with yt-dlp and use it as the
talk's source:

```bash
URL=$(yt-dlp -g -f best "https://www.youtube.com/watch?v=<live id>")
curl -X POST localhost:8080/api/sessions -H 'Content-Type: application/json' \
  -d '{"id":"sala-3","title":"Sala 3","source":{"kind":"url","uri":"'"$URL"'"},"source_lang":"en","target_langs":["es"]}'
```

- **This works for live streams only.** For a recorded video, see
  `docs/OBS.md` or download it as a file.
- The URL YouTube returns expires after some hours. For a full day, refresh
  it: stop the talk, recreate it with a new URL.
- Subtitles lag behind the room by the stream's own delay. They are still in
  sync for people *watching the stream*, which makes this a good option for
  remote audiences.

## How many rooms per server

Each live room needs transcription and translation capacity. One Mac mini M4
Pro handles 2 rooms comfortably. With more rooms, add machines, use GPU
servers, or use hosted models such as Gemini (`docs/MODELS.md`,
`docs/SCALING.md`). Viewers are almost free: 1,500 phones were measured at
~1% CPU per api replica.