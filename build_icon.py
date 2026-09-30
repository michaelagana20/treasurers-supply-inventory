from pathlib import Path
from PIL import Image, ImageDraw


def make_icon(size=256):
    scale = size / 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    box = lambda values: tuple(round(value * scale) for value in values)
    draw.ellipse(box((3, 3, 61, 61)), fill="#075ca8", outline="#ffd43b", width=round(4 * scale))
    draw.polygon(tuple((round(x * scale), round(y * scale)) for x, y in ((14, 26), (32, 13), (50, 26))), fill="#ffd43b")
    draw.rectangle(box((16, 27, 48, 32)), fill="#ffffff")
    for left in (19, 29, 39):
        draw.rectangle(box((left, 32, left + 6, 47)), fill="#ffffff")
    draw.rectangle(box((14, 47, 50, 52)), fill="#ffd43b")
    return image


make_icon().save(Path(__file__).with_name("app_icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
