"""Composite the approved mascot over DST's native portrait background.

Requires Pillow. Reads installed game assets; never modifies the game directory.
"""
from __future__ import annotations

import argparse
import io
import math
from pathlib import Path
import struct
import xml.etree.ElementTree as ET
import zipfile

from PIL import Image, ImageOps


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GAME = Path("C:/Program Files (x86)/Steam/steamapps/common/Don't Starve Together")


def portrait_background(game_dir: Path) -> Image.Image:
    """Decode only the native portrait_bg element, including its original frame."""
    with zipfile.ZipFile(game_dir / "data/databundles/images.zip") as bundle:
        atlas = ET.fromstring(bundle.read("images/ui.xml"))
        texture_name = atlas.find("Texture").get("filename")
        data = bundle.read("images/" + texture_name)
    if data[:4] != b"KTEX":
        raise ValueError("The game's UI texture is not KTEX")
    header = struct.unpack_from("<I", data, 4)[0]
    compression, mipmaps = (header >> 4) & 31, (header >> 13) & 31
    if compression not in (0, 1, 2) or not mipmaps:
        raise ValueError("Expected a DXT1/DXT3/DXT5 UI texture")
    width, height, _, length = struct.unpack_from("<HHHI", data, 8)
    offset = 8 + mipmaps * 10
    if len(data) < offset + length:
        raise ValueError("Incomplete UI texture")
    # Pillow's DDS decoder handles the same BC-compressed pixel payload.
    fourcc = (b"DXT1", b"DXT3", b"DXT5")[compression]
    dds = (b"DDS " + struct.pack("<7I", 124, 0x81007, height, width, length, 0, 1)
           + bytes(44) + struct.pack("<II4sI4I", 32, 4, fourcc, 0, 0, 0, 0, 0)
           + struct.pack("<5I", 4096, 0, 0, 0, 0))
    with Image.open(io.BytesIO(dds + data[offset:offset + length])) as decoded:
        ui = decoded.convert("RGBA").transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    element = next(e for e in atlas.findall("Elements/Element")
                   if e.get("name") == "portrait_bg.tex")
    box = (math.floor(float(element.get("u1")) * width),
           math.floor((1 - float(element.get("v2"))) * height),
           math.ceil(float(element.get("u2")) * width),
           math.ceil((1 - float(element.get("v1"))) * height))
    cropped = ui.crop(box)
    return Image.frombytes("RGBa", cropped.size, cropped.tobytes()).convert("RGBA")


def compose(character: Image.Image, background: Image.Image, size: int) -> Image.Image:
    character = character.convert("RGBA")
    if character.getchannel("A").getextrema() != (0, 255):
        raise ValueError("Use the approved transparent mascot, not a white or framed image")
    character = character.crop(character.getchannel("A").getbbox())
    result = background.resize((size, size), Image.Resampling.LANCZOS)
    margin = round(size * .11)
    character = ImageOps.contain(character, (size - margin * 2, size - margin * 2),
                                Image.Resampling.LANCZOS)
    result.alpha_composite(character, ((size - character.width) // 2,
                                      (size - character.height) // 2))
    return result


def write_icon(icon: Image.Image, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    size = icon.width
    levels = []
    for index in range(size.bit_length()):
        side = size >> index
        image = icon if index == 0 else icon.resize((side, side), Image.Resampling.LANCZOS)
        levels.append(image.convert("RGBa").transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes())
    # Default platform, RGBA, 2D, complete mip chain; Klei uses premultiplied alpha.
    header = (4 << 4) | (1 << 9) | (len(levels) << 13) | (3 << 18) | (0xfff << 20)
    metadata = b"".join(struct.pack("<HHHI", size >> i, size >> i,
                                    (size >> i) * 4, len(raw))
                        for i, raw in enumerate(levels))
    texture = b"KTEX" + struct.pack("<I", header) + metadata + b"".join(levels)
    lo, hi = 1 / size, 1 - 1 / size
    atlas = (f'<?xml version="1.0" encoding="utf-8"?>\n<Atlas>\n'
             f'    <Texture filename="modicon.tex" />\n    <Elements>\n'
             f'        <Element name="modicon.tex" u1="{lo}" u2="{hi}" v1="{lo}" v2="{hi}" />\n'
             f'    </Elements>\n</Atlas>\n')
    icon.save(output_dir / "modicon.png", optimize=True)
    (output_dir / "modicon.tex").write_bytes(texture)
    (output_dir / "modicon.xml").write_text(atlas, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", type=Path, default=DEFAULT_GAME)
    parser.add_argument("--character", type=Path, default=ROOT / "assets/logo-character.png")
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    parser.add_argument("--size", type=int, choices=(128, 256, 512), default=256)
    args = parser.parse_args()
    background = portrait_background(args.game_dir)
    with Image.open(args.character) as character:
        icon = compose(character, background, args.size)
    write_icon(icon, args.output_dir)
    print(f"Built {args.size}x{args.size} modicon.png/.tex/.xml in {args.output_dir}")


if __name__ == "__main__":
    main()
