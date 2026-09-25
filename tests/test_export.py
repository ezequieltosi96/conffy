from conffy.contracts import Caption, CaptionKind
from conffy.export import to_srt, to_txt, to_vtt


def cap(seq, text, t0, t1):
    return Caption(session_id="s", lang="es", kind=CaptionKind.FINAL, seq=seq, text=text, t0=t0, t1=t1)


def test_srt_and_vtt_timestamps():
    caps = [cap(0, "Hola a todos.", 1.5, 3.25), cap(1, "Bienvenidos.", 3661.0, 3662.0)]
    srt = to_srt(caps)
    assert srt.startswith("1\n00:00:01,500 --> 00:00:03,250\nHola a todos.\n\n2\n01:01:01,000 --> 01:01:02,000")
    assert to_vtt(caps).startswith("WEBVTT\n\n00:00:01.500 --> 00:00:03.250\nHola a todos.")
    assert to_txt(caps) == "Hola a todos.\nBienvenidos.\n"


def test_long_caption_splits_into_two_line_cues_covering_the_same_time():
    text = " ".join(["palabra"] * 40)  # ~320 chars
    srt = to_srt([cap(0, text, 10.0, 20.0)])
    cues = srt.strip().split("\n\n")
    assert len(cues) > 1
    assert all(len(c.split("\n")) <= 4 for c in cues)  # index + time + max 2 lines
    assert all(len(line) <= 42 for c in cues for line in c.split("\n")[2:])
    assert "00:00:10,000 -->" in cues[0] and cues[-1].split("\n")[1].endswith("00:00:20,000")


def test_empty():
    assert to_srt([]) == "" and to_vtt([]) == "WEBVTT\n\n" and to_txt([]) == ""
