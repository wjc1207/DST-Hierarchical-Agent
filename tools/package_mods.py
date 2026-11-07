"""Build independent, clean Don't Starve Together mod archives."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
MODS = {
    "jev_dst_agent": ("jev_bridge_config.lua", 8765),
    "jev_ai_companion": ("jev_companion_config.lua", 8766),
}
ICONS = ("modicon.tex", "modicon.xml", "modicon.png")


def build_archive(mod_id: str, archive: Path) -> None:
    source = ROOT / "mods" / mod_id
    config_name, port = MODS[mod_id]
    files = [source / "modinfo.lua", source / "modmain.lua"]
    files.extend(source / name for name in ICONS if (source / name).is_file())
    scripts = sorted(path for path in (source / "scripts").rglob("*.lua")
                     if not path.name.endswith(("_config.lua", ".example.lua")))
    if not scripts:
        raise FileNotFoundError(source / "scripts")
    files.extend(scripts)

    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
        for path in files:
            if not path.is_file() or path.resolve() != path:
                raise ValueError(f"Missing or linked mod file: {path}")
            name = path.relative_to(source).as_posix()
            bundle.write(path, f"{mod_id}/{name}")
        # Installed configuration can contain a local token. Generate a blank one.
        config = f'return {{enabled=false,url="http://127.0.0.1:{port}/v1/exchange",token=""}}\n'
        bundle.writestr(f"{mod_id}/scripts/{config_name}", config)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output) as staging:
        archives = [Path(staging) / f"{mod_id}.zip" for mod_id in MODS]
        for mod_id, archive in zip(MODS, archives):
            build_archive(mod_id, archive)
        for archive in archives:
            print(archive.replace(output / archive.name))


if __name__ == "__main__":
    main()
