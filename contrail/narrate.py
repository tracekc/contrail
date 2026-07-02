"""Narrator: turn a script line into spoken audio.

Provider switch via TTS_PROVIDER: openai (default, cheap) | elevenlabs (nicer
voice) | local (stub) | edge (Microsoft Neural). Produces an audio file; the
orchestrator queues it for the stream, or plays it locally during development.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile

log = logging.getLogger(__name__)


def estimate_speech_seconds(text: str, wpm: float = 150.0, gap: float = 0.8) -> float:
    """Rough spoken duration, used to pace the loop before audio metadata exists."""
    words = max(len(text.split()), 1)
    return words / wpm * 60.0 + gap


class Narrator:
    def __init__(self) -> None:
        self.provider = os.getenv("TTS_PROVIDER", "openai").strip().lower()
        self._client = None

    def synth(self, text: str, out_path: str | None = None) -> str:
        """Synthesize `text` to an audio file, returning its path."""
        if out_path is None:
            # Local TTS emits WAV; the cloud providers emit MP3. ffmpeg/afplay
            # both sniff content, so the extension is cosmetic, but keep it right.
            suffix = ".wav" if self.provider == "local" else ".mp3"
            fd, out_path = tempfile.mkstemp(suffix=suffix, prefix="contrail_")
            os.close(fd)
        if self.provider == "openai":
            self._synth_openai(text, out_path)
        elif self.provider == "elevenlabs":
            self._synth_elevenlabs(text, out_path)
        elif self.provider == "local":
            self._synth_local(text, out_path)
        elif self.provider == "edge":
            self._synth_edge(text, out_path)
        else:
            raise ValueError(f"Unknown TTS_PROVIDER: {self.provider!r}")
        return out_path

    # ── providers ─────────────────────────────────────────────
    def _synth_openai(self, text: str, out_path: str) -> None:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI()  # reads OPENAI_API_KEY
        voice = os.getenv("OPENAI_TTS_VOICE", "onyx")
        with self._client.audio.speech.with_streaming_response.create(
            model="tts-1", voice=voice, input=text, response_format="mp3"
        ) as resp:
            resp.stream_to_file(out_path)

    def _synth_local(self, text: str, out_path: str) -> None:
        """Offline, zero-cost TTS. Prefers Piper (neural, good quality) when a
        voice model is configured; otherwise falls back to macOS `say`, which is
        always available. Both write WAV to out_path."""
        piper_voice = os.getenv("PIPER_VOICE")  # path to a .onnx voice model
        piper_bin = os.getenv("PIPER_BIN", "piper")
        if piper_voice and shutil.which(piper_bin):
            subprocess.run(
                [piper_bin, "-m", piper_voice, "-f", out_path],
                input=text.encode(), check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return

        say_bin = shutil.which("say")
        if say_bin:
            # Daniel = British English male; fits a calm aviation-desk anchor.
            voice = os.getenv("MACOS_SAY_VOICE", "Daniel")
            subprocess.run(
                [say_bin, "-v", voice, "-o", out_path,
                 "--data-format=LEI16@22050", text],
                check=True,
            )
            return

        raise RuntimeError(
            "local TTS needs Piper (set PIPER_VOICE to a .onnx model) or macOS `say`"
        )

    def _synth_elevenlabs(self, text: str, out_path: str) -> None:
        if self._client is None:
            from elevenlabs.client import ElevenLabs

            self._client = ElevenLabs()  # reads ELEVENLABS_API_KEY
        voice_id = os.getenv("ELEVENLABS_VOICE_ID")
        if not voice_id:
            raise ValueError("ELEVENLABS_VOICE_ID is required for elevenlabs TTS")
        audio = self._client.text_to_speech.convert(
            voice_id=voice_id,
            model_id="eleven_flash_v2_5",
            text=text,
            output_format="mp3_44100_128",
        )
        with open(out_path, "wb") as f:
            for chunk in audio:
                if chunk:
                    f.write(chunk)

    def _synth_edge(self, text: str, out_path: str) -> None:
        import asyncio
        import edge_tts

        voice = os.getenv("EDGE_TTS_VOICE", "en-GB-RyanNeural")
        asyncio.run(edge_tts.Communicate(text, voice).save(out_path))


def play(path: str) -> None:
    """Play an audio file locally (dev preview). macOS afplay, else ffplay."""
    player = shutil.which("afplay") or shutil.which("ffplay")
    if not player:
        log.warning("no local audio player found (afplay/ffplay); skipping playback")
        return
    args = [player, path]
    if player.endswith("ffplay"):
        args = [player, "-autoexit", "-nodisp", "-loglevel", "quiet", path]
    subprocess.run(args, check=False)
