"""Narration for the demo video: one WAV per scene with Kokoro-82M (local, Apache-2.0), plus per-sentence
durations that record.py and build.py use for timing.

  pip install kokoro soundfile          # in a separate environment; not a project dependency
  python scripts/video/voice.py <out-dir>

Then: python scripts/video/record.py <out-dir> <clips-dir>   (web server running on :8000)
      python scripts/video/build.py <out-dir> <clips-dir> docs/video/lineage-demo.mp4
"""
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from kokoro import KPipeline

HERE = Path(__file__).parent
SPLIT = r"(?<=[.?!:])\s+"   # build.py splits the caption text the same way


def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    tts = KPipeline(lang_code="a")
    for scene in json.load(open(HERE / "script.json")):
        parts, timing = [], []
        for text, _, audio in tts(scene["say"], voice="af_heart", speed=1.08, split_pattern=SPLIT):
            a = audio.numpy() if hasattr(audio, "numpy") else audio
            parts.append(a)
            timing.append((text, len(a) / 24000))
        sf.write(out / f"vo_{scene['id']}.wav", np.concatenate(parts), 24000)
        json.dump(timing, open(out / f"vo_{scene['id']}.json", "w"))
        print(scene["id"], round(sum(d for _, d in timing), 2))


if __name__ == "__main__":
    main()
