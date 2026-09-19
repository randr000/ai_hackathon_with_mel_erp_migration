"""One-off verification of the live ElevenLabs round-trip.

Synthesize a short phrase, feed it to Scribe, and print the transcript. This
proves the API key, TTS and STT all work before wiring a real microphone.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import voice  # noqa: E402

audio, cached = voice.synthesize("Approve the top match.")
print(f"TTS bytes: {len(audio)}  cached: {cached}")
text = voice.transcribe(audio, "test.mp3")
print(f"STT transcript: {text!r}")
