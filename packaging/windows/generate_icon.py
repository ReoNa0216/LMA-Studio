#!/usr/bin/env python3
"""Generate the deterministic Windows icon used by the LMA Studio build."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


SIZE = 1024
OUTPUT = Path(__file__).with_name("LMAStudio.ico")


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path(r"C:\Windows\Fonts\arialbd.ttf"),
        Path(r"C:\Windows\Fonts\segoeuib.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def main() -> None:
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

    image.save(
        OUTPUT,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


if __name__ == "__main__":
    main()
