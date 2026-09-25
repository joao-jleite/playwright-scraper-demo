"""Stitch the recorded .webm segments into docs/demo.gif and docs/demo.mp4 (imageio + imageio-ffmpeg).

Reads media_build/timeline.json written by record_demo.py. Each segment has a plan of
(t0, t1, speed, badge) intervals. Only real waits are sped up: the crawl (page loads and polite
delays 4x, page processing 2x) and the replay of the full-run log (3x). Every frame of a sped-up
interval carries a speed badge; everything else plays at 1x. A sped-up interval without a badge
is refused, so the media can never compress time silently.

    python scripts/build_media.py --out docs
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import imageio_ffmpeg
import matplotlib
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "media_build"
W, H = 1280, 800
FONT_PATH = str(Path(matplotlib.get_data_path()) / "fonts" / "ttf" / "DejaVuSans-Bold.ttf")


_FONTS: dict[int, ImageFont.FreeTypeFont] = {}


def badge(img: Image.Image, text: str) -> Image.Image:
    """Pill in the top-right corner, '▶▶ 4× speed', sized relative to a 1280 px frame."""
    k = img.width / W
    size = round(22 * k)
    font = _FONTS.setdefault(size, ImageFont.truetype(FONT_PATH, size))
    speed = text.replace("x", "×")
    label = f"▶▶ {speed} speed"
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    tw = d.textlength(label, font=font)
    x1, y0 = img.width - 22 * k, 22 * k
    x0, y1 = x1 - tw - 34 * k, y0 + 42 * k
    d.rounded_rectangle((x0, y0, x1, y1), radius=21 * k, fill=(42, 120, 214, 235))
    d.text((x0 + 17 * k, y0 + 8 * k), label, font=font, fill=(255, 255, 255, 255))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def _coarse(a: np.ndarray, f: int = 4) -> np.ndarray:
    """4x4 box-averaged copy: blur/sharpen differences from the codec mostly cancel out here."""
    h, w = a.shape[0] // f, a.shape[1] // f
    return a[: h * f, : w * f].reshape(h, f, w, f, 3).mean(axis=(1, 3))


def _change_map(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.abs(_coarse(a) - _coarse(b)).max(axis=2)


def settle(frames: list[np.ndarray], block: int = 16, real_change: float = 45.0) -> list[np.ndarray]:
    """Clean Playwright's VP8 video before GIF/MP4 encoding.

    Measured on this recording, codec refinement/shimmer never exceeds ~36 levels on a 4x4-averaged
    frame, while real changes (typing, cursor, highlights, scrolling) are above ~70. `real_change`=45
    separates them. Decisions are taken per FRAME first, so moments are never mixed (no ghosting):

    1. Backward pass: VP8 at ~1 Mbit/s shows a soft frame after every jump and sharpens it over the
       next second. A frame that differs from the next output frame only by refinement is replaced by
       that later, sharper frame.
    2. Forward pass: when nothing really changed the previous frame is repeated; scroll/jump/tab-switch
       frames are kept as they are; for local changes every block that differs even slightly is
       refreshed and only same-content blocks keep the previous pixels.
    Static stretches end up byte-identical, which keeps the GIF small.
    """
    if len(frames) < 2:
        return frames
    arr = [f.astype(np.int16) for f in frames]
    out = arr[:]
    for k in range(len(arr) - 2, -1, -1):
        if _change_map(arr[k], out[k + 1]).max() < real_change:
            out[k] = out[k + 1]
    cell = block // 4  # one 16 px block = 4x4 cells of the coarse map
    for k in range(1, len(out)):
        cm = _change_map(out[k], out[k - 1])
        h, w = cm.shape[0] // cell, cm.shape[1] // cell
        block_max = cm[: h * cell, : w * cell].reshape(h, cell, w, cell).max(axis=(1, 3))
        strong = block_max >= real_change
        if not strong.any():
            out[k] = out[k - 1]  # shimmer only
            continue
        if strong.mean() > 0.15:
            continue  # scroll, page jump, tab switch: keep the real frame untouched
        # Local change (typing, cursor, HUD): refresh every block that differs even a little, keep the
        # previous pixels only where the content is the same, so two moments are never mixed.
        update = block_max >= 10
        g = update.copy()  # 1-block halo so glyph edges update with their core
        g[1:] |= update[:-1]
        g[:-1] |= update[1:]
        g[:, 1:] |= update[:, :-1]
        g[:, :-1] |= update[:, 1:]
        mask = np.zeros(out[k].shape[:2], dtype=bool)
        mask[: h * block, : w * block] = np.repeat(np.repeat(g, block, axis=0), block, axis=1)
        out[k] = np.where(mask[..., None], out[k], out[k - 1])
    return [o.astype(np.uint8) for o in out]


def schedule(plan: list, fps: float, duration: float) -> list[tuple[float, str | None]]:
    """Output frames -> (source time, badge) for one segment."""
    out = []
    for t0, t1, speed, tag in plan:
        if speed > 1.001 and not tag:
            raise ValueError(f"interval {t0:.2f}-{t1:.2f} s is sped up {speed}x without a badge")
        t0, t1 = max(0.0, t0), min(t1, duration - 0.05)
        if t1 <= t0:
            continue
        n = max(1, round((t1 - t0) / speed * fps))
        out += [(t0 + k * speed / fps, tag) for k in range(n)]
    return out


def hud_plan(video: Path, fast: float = 4.0, normal: float = 2.0) -> list:
    """Crawl segment plan taken from the video itself: the demo HUD (dark box, bottom-right) is on
    screen while a page is being processed and disappears while the next page loads. Loading gaps are
    fast-forwarded (`fast`), processing is shown at `normal` speed; both carry their speed badge."""
    reader = imageio_ffmpeg.read_frames(str(video))
    meta = next(reader)
    fps, (w, h) = meta["fps"], meta["size"]
    on, events, n = False, [], 0
    for n, raw in enumerate(reader):
        a = np.frombuffer(raw, np.uint8).reshape(h, w, 3)
        now = a[int(h * 0.81):int(h * 0.96), int(w * 0.74):int(w * 0.96)].mean() < 70
        if now != on:
            events.append((n / fps, now))
            on = now
    end = n / fps
    ons = [t for t, state in events if state]
    offs = [t for t, state in events if not state]
    plan, cursor = [], 0.3
    for i, t_on in enumerate(ons):
        plan.append((cursor, t_on, fast, "4x"))
        t_off = next((t for t in offs if t > t_on), end - 0.1)
        plan.append((t_on, t_off, normal, f"{normal:g}x"))
        cursor = t_off
    return plan


def iter_segment(video: Path, wanted: list[tuple[float, str | None]]):
    """Stream the webm once and yield (frame RGB image, badge) for each wanted source time."""
    reader = imageio_ffmpeg.read_frames(str(video), pix_fmt="rgb24")
    meta = next(reader)
    fps = meta["fps"]
    size = meta["size"]
    idx, frame, want_i = -1, None, 0
    for raw in reader:
        idx += 1
        frame = raw
        while want_i < len(wanted) and round(wanted[want_i][0] * fps) <= idx:
            img = Image.frombuffer("RGB", size, frame, "raw", "RGB", 0, 1)
            if img.size != (W, H):
                img = img.resize((W, H), Image.LANCZOS)
            yield img, wanted[want_i][1]
            want_i += 1
    while want_i < len(wanted) and frame is not None:  # plan slightly past the end: hold the last frame
        yield Image.frombuffer("RGB", size, frame, "raw", "RGB", 0, 1).resize((W, H)), wanted[want_i][1]
        want_i += 1


def video_duration(video: Path) -> float:
    reader = imageio_ffmpeg.read_frames(str(video))
    meta = next(reader)
    reader.close()
    return float(meta["duration"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "docs")
    ap.add_argument("--gif-fps", type=float, default=10)
    ap.add_argument("--gif-width", type=int, default=960)
    ap.add_argument("--gif-colors", type=int, default=112)
    ap.add_argument("--mp4-fps", type=float, default=20)
    ap.add_argument("--crf", type=int, default=28)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    timeline = json.loads((BUILD / "timeline.json").read_text(encoding="utf-8"))

    frames_dir = BUILD / "gif_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("f_*.png"):  # frames from a previous build
        old.unlink()

    # ---- MP4 (H.264, yuv420p, no metadata) at mp4-fps; GIF frames written as PNGs at gif-fps
    mp4 = a.out / "demo.mp4"
    writer = imageio_ffmpeg.write_frames(
        str(mp4), (W, H), fps=a.mp4_fps, codec="libx264", pix_fmt_out="yuv420p", quality=None, macro_block_size=16,
        output_params=["-crf", str(a.crf), "-preset", "slow", "-movflags", "+faststart", "-map_metadata", "-1"])
    writer.send(None)
    gif_h = round(a.gif_width * H / W)
    n_mp4 = n_gif = 0
    for seg in timeline:
        video = ROOT / seg["video"]
        dur = video_duration(video)
        if seg["name"] == "crawl":
            seg["plan"] = hud_plan(video)
        elif seg.get("lifetime"):
            # recorder marks are wall-clock; stretch them onto the video clock
            k = dur / seg["lifetime"]
            seg["plan"] = [(t0 * k, t1 * k, sp, tag) for t0, t1, sp, tag in seg["plan"]]
        for fps, sink in ((a.mp4_fps, "mp4"), (a.gif_fps, "gif")):
            frames, tags = [], []
            for img, tag in iter_segment(video, schedule(seg["plan"], fps, dur)):
                if sink == "gif":
                    img = img.resize((a.gif_width, gif_h), Image.LANCZOS)
                frames.append(np.asarray(img))
                tags.append(tag)
            while len(frames) > 1 and frames[0].mean() > 248:  # blank page before the first paint
                frames.pop(0)
                tags.pop(0)
            for arr, tag in zip(settle(frames), tags):
                img = Image.fromarray(arr)
                if tag:
                    img = badge(img, tag)
                if sink == "mp4":
                    writer.send(np.asarray(img, dtype=np.uint8).tobytes())
                    n_mp4 += 1
                else:
                    n_gif += 1
                    img.save(frames_dir / f"f_{n_gif:04d}.png")
        print(f"{seg['name']:<6} video {dur:5.1f} s -> gif frames so far {n_gif}")
    writer.close()

    # ---- GIF: one global palette for the whole clip, no dithering (UI content), only changed rectangles
    gif = a.out / "demo.gif"
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run([
        ffmpeg, "-y", "-loglevel", "error", "-framerate", str(a.gif_fps), "-i", str(frames_dir / "f_%04d.png"),
        "-filter_complex",
        f"[0:v]split[a][b];[a]palettegen=max_colors={a.gif_colors}:stats_mode=full[p];"
        "[b][p]paletteuse=dither=none:diff_mode=rectangle",
        "-loop", "0", str(gif)], check=True)

    print(f"mp4: {mp4.stat().st_size / 1e6:.2f} MB, {n_mp4 / a.mp4_fps:.1f} s @ {a.mp4_fps} fps, {W}x{H}")
    print(f"gif: {gif.stat().st_size / 1e6:.2f} MB, {n_gif / a.gif_fps:.1f} s @ {a.gif_fps} fps, "
          f"{a.gif_width}x{gif_h}, {n_gif} frames")


if __name__ == "__main__":
    main()
