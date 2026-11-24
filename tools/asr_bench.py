"""ASR benchmark on synthesized speech with known ground truth.

Measures what actually matters for the demo: word error rate on Russian and the
latency of a single transcription pass. Kept dependency-free apart from
faster-whisper and numpy so it can run on any machine with the venv installed.

Run:
    .venv\\Scripts\\python.exe tools\\asr_bench.py --wav .artifacts\\speech\\ru-1.wav --text "Коллеги, ..." --model large-v3-turbo
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import wave
from pathlib import Path

import numpy as np

PUNCT = re.compile(r"[^\w\s]|_", re.UNICODE)


def normalize(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = PUNCT.sub(" ", text)
    return " ".join(text.split())


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref = normalize(reference).split()
    hyp = normalize(hypothesis).split()
    if not ref:
        return 0.0 if not hyp else 1.0
    # Classic Levenshtein over words, memory O(len(hyp)).
    previous = list(range(len(hyp) + 1))
    for i, ref_word in enumerate(ref, start=1):
        current = [i]
        for j, hyp_word in enumerate(hyp, start=1):
            cost = 0 if ref_word == hyp_word else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1] / len(ref)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        rate = handle.getframerate()
        width = handle.getsampwidth()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise SystemExit(f"expected 16-bit PCM, got {width * 8}-bit")
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio, rate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", required=True)
    parser.add_argument("--text", required=True, help="reference transcript")
    parser.add_argument("--model", default="large-v3-turbo")
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    audio, rate = read_wav(Path(args.wav))
    duration = len(audio) / rate

    # Reuse the project's runtime setup (cuBLAS/cuDNN directories from wheels).
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    try:
        from app.asr import add_cuda_dll_dirs

        add_cuda_dll_dirs()
    except Exception:  # noqa: BLE001 - bench must work even without the app package
        pass

    from faster_whisper import WhisperModel

    device = args.device
    if device == "auto":
        try:
            import ctranslate2

            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:  # noqa: BLE001
            device = "cpu"
    compute_type = "int8_float16" if device == "cuda" else "int8"

    load_started = time.perf_counter()
    try:
        model = WhisperModel(args.model, device=device, compute_type=compute_type)
    except Exception as error:  # noqa: BLE001 - GPU often lacks cuDNN in a fresh env
        print(f"falling back to cpu: {error}", file=sys.stderr)
        device, compute_type = "cpu", "int8"
        model = WhisperModel(args.model, device=device, compute_type=compute_type)
    load_seconds = time.perf_counter() - load_started

    started = time.perf_counter()
    segments, info = model.transcribe(
        audio,
        language="ru",
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    hypothesis = " ".join(segment.text.strip() for segment in segments)
    elapsed = time.perf_counter() - started

    result = {
        "model": args.model,
        "device": device,
        "compute_type": compute_type,
        "audio_seconds": round(duration, 2),
        "load_seconds": round(load_seconds, 2),
        "transcribe_seconds": round(elapsed, 2),
        "realtime_factor": round(elapsed / duration, 3) if duration else None,
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "wer": round(word_error_rate(args.text, hypothesis), 4),
        "reference": args.text,
        "hypothesis": hypothesis.strip(),
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"model       : {result['model']} ({device}, {compute_type})")
        print(f"audio       : {result['audio_seconds']} s, load {result['load_seconds']} s")
        print(f"transcribe  : {result['transcribe_seconds']} s  (RTF {result['realtime_factor']})")
        print(f"language    : {result['language']} p={result['language_probability']}")
        print(f"WER         : {result['wer'] * 100:.1f}%")
        print(f"reference   : {args.text}")
        print(f"hypothesis  : {result['hypothesis']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
