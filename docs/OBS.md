# conffy with OBS (or vMix)

OBS can play two roles with conffy, together or separately:

1. **Send a stage's audio to conffy.** OBS streams to MediaMTX, which runs
   next to conffy, and a conffy worker reads that stream.
2. **Show the subtitles over any video.** conffy's overlay is a transparent
   web page that OBS adds as a *Browser* source on top of any camera, capture
   card, screen, media file or NDI feed. The subtitles are then part of
   whatever OBS streams or records.

The steps below were tested with OBS 30+ on macOS. vMix works the same way:
it has an RTMP output and a web browser input.

## 1. Start conffy with MediaMTX

On the machine running conffy, with the models running
(`./scripts/run-host.sh`):

```bash
make up-obs        # the stack + MediaMTX listening on rtmp://<host>:1935
make ps            # all containers "running", including mediamtx
docker compose logs mediamtx --tail 5   # "[RTMP] listener opened on :1935"
```

One Mac mini M4 Pro handles **2 live talks comfortably** (`docs/SCALING.md`).
Stop demo talks you don't need: `make stop ID=sala-a`.

## 2. Get the audio into conffy

### Option A: OBS sends the audio

Use this when this OBS already has the stage's audio (a mixer input, a
microphone, a capture card), or when you want to subtitle something playing
on the computer, like a YouTube talk in Chrome.

1. **Add the audio source.** OBS streams its **mix**: every audio source that
   is not muted. Mute everything you don't want transcribed, such as your own
   microphone under *Mic/Aux*. Some typical sources:
   - A mixer, an audio interface or a microphone: *Audio Input Capture*.
   - A browser tab or app (e.g. YouTube in Chrome) on macOS: *macOS Screen
     Capture* → Method *Application Capture* → Chrome, with **Capture audio**
     checked. macOS asks for the *Screen & System Audio Recording* permission
     once (System Settings → Privacy & Security); restart OBS after granting it.
   - Check that the level meter in the *Audio Mixer* moves.
2. **Point OBS at MediaMTX.** Settings → Stream:
   - Service: **Custom**
   - Server: `rtmp://<conffy host>:1935` (`rtmp://localhost:1935` if it is
     the same machine)
   - Stream key: **the talk id**, e.g. `sala-yt`
3. **Start the stream first** (*Start Streaming*). Check that MediaMTX
   receives it with `docker compose logs mediamtx --tail 5`: it should say
   `is publishing to path 'sala-yt'`.
4. **Then create the talk** in conffy:

   ```bash
   make room ID=sala-yt FROM=en TO=es TITLE="Main stage"
   ```

   It prints the viewer and overlay URLs. `FROM` is the language spoken on
   stage; `TO` is the subtitle language. The order matters: if the talk is
   created before the stream exists, it fails. In that case, start the stream
   and run `make start ID=sala-yt`.

**OBS streams to one destination.** If this same OBS must also stream the
event to YouTube or Twitch, there are two ways out:

- Feed conffy through Option B, so OBS only does the overlay.
- Add a second RTMP output to OBS, pointing to MediaMTX, with a
  multiple-output plugin such as *Multiple RTMP outputs*.

### Option B: the audio comes from somewhere else

The audio may already reach conffy another way: a laptop at the mixer
(`docs/WEB.md`), a hardware encoder, or another OBS. Then this OBS only adds
the overlay, and you can skip this step.

## 3. Add the subtitles on top of the video

1. In your scene, add **Sources → + → Browser**:
   - URL: `http://<conffy host>:8080/overlay.html?s=<talk id>&lang=<lang>`,
     for example `http://localhost:8080/overlay.html?s=sala-yt&lang=es`
   - Width **1920**, Height **1080**: the same as the canvas (Settings → Video)
   - Leave *Custom CSS* as it is; the page is already transparent
   - Uncheck *Shutdown source when not visible*, so it stays connected
2. Drag the Browser source **above** the video source in the *Sources* list.
3. The subtitles appear at the bottom center, in white with a black outline.

**Overlay options** (add them to the URL):

| Parameter | Default | Effect |
|---|---|---|
| `size` | `48` | font size in px (on a 1920 × 1080 canvas) |
| `lines` | `2` | how many recent sentences are kept |
| `rows` | `2` | maximum rows of text on screen. Long sentences show their newest words, so the video is never covered. |
| `partials` | `1` | `0` = show only finished sentences (steadier, a bit later) |

Example for a busy slide deck: `...overlay.html?s=sala-yt&lang=es&size=40&rows=2&partials=0`.

The screen clears itself after 8 seconds without speech.

It works over **any video source**: camera, capture card, *Display Capture*,
*Media Source* (a recorded video), NDI, or a whole nested scene. You can add
two Browser sources, one per language, stacked or in different scenes.

## 4. Delay and sync

Subtitles appear **1–3 s after the words** are spoken: ~1 s in the original
language, ~2–3 s translated. That is normal for live captioning, and viewers
accept it.

If you need tighter sync on the broadcast, delay the program video and audio
with OBS's delay filters (*Render Delay*, *Video Delay (Async)*, and *Sync
Offset* in *Advanced Audio Properties*). In that case conffy must keep getting
the **undelayed** audio, so use Option B for the audio. Check the maximum delay
each filter supports in your OBS version.

## 5. Record or stream

- **Start Recording** saves the video with the subtitles burned in. Useful
  for demos and archives.
- **Start Streaming** sends it to the audience. With Option A this output is
  already used by MediaMTX (see "OBS streams to one destination" above).
- For a clean, editable subtitle track instead of burned-in text, download
  `http://<conffy host>:8080/api/sessions/<id>/export.vtt?lang=<lang>` after
  the talk and upload it to YouTube.

## Several stages

Use one OBS (or one audio feed) per stage, each with its **own stream key =
talk id**. Create one talk per stage with `make room ID=<stage>`. The overlay
URL just changes its `s=` parameter.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| OBS: "Failed to connect to server" | MediaMTX isn't running, or the port isn't reachable | `make up-obs`, `make ps`; open port 1935 on the conffy host's firewall |
| The talk shows "Interrumpida" right away | It was created before OBS started streaming | Start streaming, then `make start ID=<id>` |
| The talk is live but no subtitles appear | OBS isn't sending audio (the meter doesn't move) or the source is muted | Check *Capture audio*, the macOS permission (restart OBS), and the mixer |
| The overlay shows nothing | Wrong URL, or the talk has no captions in that `lang` | Open the overlay URL in a normal browser; check `s=` and `lang=` |
| The overlay shows "Missing ?s=..." | The URL lacks the parameters | Add `?s=<id>&lang=<lang>` |
| Subtitles lag more and more | The models are saturated | Fewer live talks per machine (`make watch`); see `docs/SCALING.md` |
| The talk ended when I stopped streaming | Expected: the end of the stream is the end of the talk | Start streaming again, then `make start ID=<id>` |
| Your own voice appears in the subtitles | The microphone is in OBS's mix | Mute *Mic/Aux* |