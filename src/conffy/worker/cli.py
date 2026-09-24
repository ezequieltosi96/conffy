"""
Terminal captions: the first vertical slices, and a handy debugging tool.

    uv run python -m conffy.worker.cli samples/jfk.wav
    uv run python -m conffy.worker.cli samples/talk.mp3 --to es pt --glossary nerdearla
    uv run python -m conffy.worker.cli samples/charla.mp3 --lang es --to en
    uv run python -m conffy.worker.cli samples/talk.mp3 --to none      # transcription only
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys

from pysilero_vad import SileroVoiceActivityDetector

from conffy.contracts import Caption, CaptionKind, Source, SourceKind
from conffy.glossary import EMPTY, load_glossary
from conffy.providers.asr_whispercpp import WhisperCppTranscriber
from conffy.providers.mt_openai_compat import OpenAICompatTranslator
from conffy.worker.audio import ffmpeg_pcm
from conffy.worker.pipeline import AsrPipeline, PipelineConfig
from conffy.worker.segmenter import Segmenter
from conffy.worker.translate import TranslationStage

DIM, CYAN, RESET, CLEAR = "\033[90m", "\033[36m", "\033[0m", "\r\033[2K"


def fmt_ts(seconds: float) -> str:
    m, s = divmod(seconds, 60)
    return f"{int(m):02d}:{s:04.1f}"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="conffy terminal captions")
    p.add_argument("source", help="audio/video file path or URL (anything ffmpeg reads)")
    p.add_argument("--lang", default="en", help="source language (ISO 639-1)")
    p.add_argument("--to", nargs="+", default=["es"], help="target languages, or 'none'")
    p.add_argument("--asr-url", default=os.environ.get("ASR_URL", "http://127.0.0.1:8081"))
    p.add_argument("--mt-url", default=os.environ.get("MT_URL", "http://127.0.0.1:11434/v1"))
    p.add_argument("--mt-model", default=os.environ.get("MT_MODEL", "gemma4:e4b"))
    p.add_argument("--glossary", default=None, help="glossary name in --glossary-file")
    p.add_argument("--glossary-file", default=os.environ.get("GLOSSARY_FILE", "config/glossary.yml"))
    p.add_argument("--fast", action="store_true", help="files: read as fast as possible, not at 1x")
    p.add_argument("--no-partials", action="store_true")
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    is_url = "://" in args.source
    if not is_url and not os.path.exists(args.source):
        print(f"file not found: {args.source}", file=sys.stderr)
        return 2
    targets = tuple(t for t in args.to if t != "none" and t != args.lang)
    glossary = load_glossary(args.glossary_file, args.glossary) if args.glossary else EMPTY

    source = Source(
        kind=SourceKind.URL if is_url else SourceKind.FILE, uri=args.source, realtime=not args.fast
    )
    transcriber = WhisperCppTranscriber(args.asr_url)
    translator = OpenAICompatTranslator(args.mt_url, args.mt_model, api_key=os.environ.get("MT_API_KEY"))
    width = shutil.get_terminal_size((100, 20)).columns

    async def show(c: Caption) -> None:
        if c.kind is CaptionKind.PARTIAL:
            text = c.text if len(c.text) < width - 4 else "…" + c.text[-(width - 5):]
            sys.stdout.write(f"{CLEAR}{DIM}{text}{RESET}")
        elif c.lang == args.lang:
            s = pipeline.stats
            meta = f"{DIM}(asr {s.last_asr_ms:.0f} ms · lag {s.last_lag_s:.1f} s){RESET}"
            sys.stdout.write(f"{CLEAR}[{fmt_ts(c.t0)} → {fmt_ts(c.t1)}] #{c.seq} {c.text} {meta}\n")
        else:
            meta = f"{DIM}(mt {stage.stats.last_mt_ms:.0f} ms · backlog {stage.backlog}){RESET}"
            sys.stdout.write(f"{CLEAR}{' ' * 18}{CYAN}↳ {c.lang}: {c.text}{RESET} {meta}\n")
        sys.stdout.flush()

    stage = TranslationStage(
        translator,
        show,
        source_lang=args.lang,
        target_langs=targets,
        glossaries={lang: glossary.for_lang(lang) for lang in targets},
    )

    async def emit(c: Caption) -> None:
        await show(c)
        stage.submit(c)

    pipeline = AsrPipeline(
        transcriber,
        Segmenter(SileroVoiceActivityDetector()),
        emit,
        PipelineConfig(
            session_id="cli",
            language=args.lang,
            partial_every_s=0 if args.no_partials else 1.0,
            base_prompt=glossary.asr_prompt(),
        ),
    )
    mt_task = asyncio.create_task(stage.run())
    try:
        await pipeline.run(ffmpeg_pcm(source))
    finally:
        stage.close()
        await mt_task
        await transcriber.aclose()
        await translator.aclose()

    s, m = pipeline.stats, stage.stats
    print(
        f"\n{DIM}asr: finals={s.finals} partials={s.partials} dropped={s.dropped} errors={s.errors} "
        f"avg={s.avg_asr_ms:.0f} ms max_lag={s.max_lag_s:.1f} s\n"
        f"mt:  done={m.done} errors={m.errors} avg={m.avg_mt_ms:.0f} ms{RESET}"
    )
    for err in [*s.recent, *([m.last_error] if m.last_error else [])]:
        print(f"  error: {err}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
