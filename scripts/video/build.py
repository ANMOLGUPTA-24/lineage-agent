"""Assemble the demo video: recorded clips + narration + burned-in captions.

  python scripts/video/build.py <voice-dir> <clips-dir> <out.mp4>

Each scene's clip (from record.py) is cut to the scene, scaled to 1080p and laid over its narration
(vo_<id>.wav, starting LEAD seconds in). Captions come from script.json's "show" text, split into the
same sentences the voice was generated from, so each caption is timed to its own sentence.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
LEAD = 0.5          # must match record.py
FADE = 0.25
# Captions as ASS with a 1920x1080 canvas, so sizes are real pixels (SRT styles scale from 384x288).
# Opaque rounded-ish box, raised above the app's docked question box.
ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,DejaVu Sans,40,&H00FFFFFF,&H00FFFFFF,&H28101820,&H28101820,0,0,0,0,100,100,0,0,3,14,0,2,240,240,118,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def run(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


def pieces(sentence: str, dur: float, limit: int = 84) -> list[tuple[str, float]]:
    """Split a long sentence at commas into caption-sized pieces, timed by length."""
    parts, cur = [], ""
    for chunk in re.split(r"(?<=,)\s+", sentence):
        if cur and len(cur) + len(chunk) + 1 > limit:
            parts.append(cur)
            cur = chunk
        else:
            cur = f"{cur} {chunk}".strip()
    parts.append(cur)
    total = sum(len(p) for p in parts)
    return [(p, dur * len(p) / total) for p in parts]


def stamp(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}".replace(".", ",")


def ass_time(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def main():
    voice, clips_dir, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    work = clips_dir / "build"
    work.mkdir(exist_ok=True)
    scenes = json.load(open(HERE / "script.json"))
    clips = json.load(open(clips_dir / "clips.json"))
    segs, srt, ass, t, n = [], [], [], 0.0, 1
    for k, sc in enumerate(scenes):
        c = clips[sc["id"]]
        dur = c["dur"]
        seg = work / f"{k:02d}_{sc['id']}.mp4"
        run("-ss", str(c["offset"]), "-t", str(dur), "-i", c["video"], "-i", str(voice / f"vo_{sc['id']}.wav"),
            "-filter_complex",
            f"[0:v]scale=1920:1080:flags=lanczos,fps=30,format=yuv420p,"
            f"fade=t=in:st=0:d={FADE},fade=t=out:st={dur - FADE:.3f}:d={FADE}[v];"
            f"[1:a]aresample=48000,adelay={int(LEAD * 1000)}:all=1,apad,atrim=0:{dur:.3f}[a]",
            "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-c:a", "pcm_s16le", "-t", str(dur), str(seg.with_suffix(".mkv")))
        segs.append(seg.with_suffix(".mkv"))
        # Captions: sentence k of the voice <-> sentence k of the "show" text.
        timing = json.load(open(voice / f"vo_{sc['id']}.json"))
        shown = [x for x in re.split(r"(?<=[.?!:])\s+", sc["show"]) if x.strip()]
        assert len(shown) == len(timing), sc["id"]
        at = t + LEAD
        for text, (_, d) in zip(shown, timing):
            for piece, pd in pieces(text, d):
                srt.append(f"{n}\n{stamp(at)} --> {stamp(at + pd - 0.05)}\n{piece}\n")
                ass.append(f"Dialogue: 0,{ass_time(at)},{ass_time(at + pd - 0.05)},Cap,,0,0,0,,{piece}")
                n += 1
                at += pd
        t += dur
    (work / "list.txt").write_text("".join(f"file '{s.resolve()}'\n" for s in segs))
    (work / "captions.srt").write_text("\n".join(srt))
    (work / "captions.ass").write_text(ASS_HEAD + "\n".join(ass) + "\n")
    joined = work / "joined.mkv"
    run("-f", "concat", "-safe", "0", "-i", str(work / "list.txt"), "-c", "copy", str(joined))
    run("-i", str(joined), "-vf", f"subtitles={work / 'captions.ass'}",
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:v", "libx264", "-preset", "slow", "-crf", "24",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out))
    (out.with_suffix(".srt")).write_text("\n".join(srt))
    print(f"{out}: {t:.1f} s, {out.stat().st_size / 1e6:.1f} MB, {n - 1} captions")


if __name__ == "__main__":
    main()
