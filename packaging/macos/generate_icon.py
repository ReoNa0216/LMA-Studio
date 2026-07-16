#!/usr/bin/env python3
"""Generate the deterministic macOS icon set used by LMA Studio."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


SIZE = 1024
OUTPUT_DIR = Path(__file__).with_name("LMAStudio.iconset")


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        Path("/System/Library/Fonts/SFNS.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def render_master() -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((56, 56, 968, 968), radius=176, fill="#101828")

    baseline = 690
    colors = ("#2A7D67", "#7A52C7", "#C65F12")
    x_positions = (220, 512, 804)
    heights = (250, 390, 300)
    for x, height, color in zip(x_positions, heights, colors):
        draw.line((x - 108, baseline, x - 30, baseline), fill=color, width=30)
        draw.line((x - 30, baseline, x, baseline - height), fill=color, width=30)
        draw.line((x, baseline - height, x + 30, baseline), fill=color, width=30)
        draw.line((x + 30, baseline, x + 108, baseline), fill=color, width=30)

    font = load_font(184)
    text = "LMA"
    box = draw.textbbox((0, 0), text, font=font)
    text_width = box[2] - box[0]
    draw.rounded_rectangle((150, 718, 874, 914), radius=56, fill="#FFFFFF")
    draw.text(((SIZE - text_width) / 2, 718), text, font=font, fill="#101828")
    return image


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    master = render_master()
    icon_files = {
        "icon_16x16.png": 16,
        "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32,
        "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128,
        "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256,
        "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512,
        "icon_512x512@2x.png": 1024,
    }
    for filename, size in icon_files.items():
        resized = master if size == SIZE else master.resize((size, size), Image.Resampling.LANCZOS)
        resized.save(OUTPUT_DIR / filename, format="PNG")


if __name__ == "__main__":
    main()
