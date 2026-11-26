"""Build clean Don't Starve Together mod archives, self-contained Steam Workshop folders, and the GitHub runner bundle."""
from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[2]
MODS = {
    "dst_buddy_agent": {
        "config_name": "jev_companion_config.lua",
        "port": 8766,
        "mode": "buddy",
        "run_stem": "2_run_buddy",
    },
}
REQUIRED_ROOT_FILES = ("modinfo.lua", "modmain.lua", "modicon.tex", "modicon.xml", "preview.jpg")
OPTIONAL_ARCHIVE_FILES = ("modicon.png",)
MAX_PREVIEW_BYTES = 1_000_000  # Steam Workshop preview image limit (~1 MB)

RUNNER_RUNTIME_FILES = (
    "setup_and_run.bat",
    "setup_and_run.sh",
    "scripts/launcher.ps1",
    "scripts/workshop_install.ps1",
    ".env.example",
    "data/DSTKnowledge.txt",
    "data/worldgen_resources.json",
    "python/requirements-mcp.txt",
)
RUNNER_DOC_FILES = (
    "README.md",
    "docs/WORKSHOP.md",
    "docs/AGENT_MEMORY_AND_BACKENDS.md",
)
RUNNER_PY_PACKAGES = ("common", "transport", "executor", "mcp_server", "mcp_agent")


def blank_config_lua(port: int) -> str:
    return f'return {{enabled=false,url="http://127.0.0.1:{port}/v1/exchange",token=""}}\n'


def extract_mod_version(modinfo_text: str) -> str:
    match = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', modinfo_text)
    if not match:
        raise ValueError("Cannot find version in modinfo.lua")
    return match.group(1)


def workshop_install_bat(mode: str) -> str:
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "setlocal",
        'set "MOD_DIR=%~dp0"',
        "where pwsh >nul 2>&1",
        "if %ERRORLEVEL%==0 (",
        f'    pwsh -NoProfile -ExecutionPolicy Bypass -File "%MOD_DIR%runner\\scripts\\workshop_install.ps1" -SourceDir "%MOD_DIR%runner" -DefaultMode {mode} %*',
        ") else (",
        f'    powershell -NoProfile -ExecutionPolicy Bypass -File "%MOD_DIR%runner\\scripts\\workshop_install.ps1" -SourceDir "%MOD_DIR%runner" -DefaultMode {mode} %*',
        ")",
        "if %ERRORLEVEL% NEQ 0 (",
        "    echo.",
        "    echo [Press any key to close...]",
        "    pause >nul",
        ")",
    ]
    return "\r\n".join(lines) + "\r\n"


def workshop_run_bat(mode: str) -> str:
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "setlocal",
        'set "MOD_DIR=%~dp0"',
        "where pwsh >nul 2>&1",
        "if %ERRORLEVEL%==0 (",
        f'    pwsh -NoProfile -ExecutionPolicy Bypass -File "%MOD_DIR%runner\\scripts\\workshop_install.ps1" -SourceDir "%MOD_DIR%runner" -DefaultMode {mode} -Launch %*',
        ") else (",
        f'    powershell -NoProfile -ExecutionPolicy Bypass -File "%MOD_DIR%runner\\scripts\\workshop_install.ps1" -SourceDir "%MOD_DIR%runner" -DefaultMode {mode} -Launch %*',
        ")",
        "if %ERRORLEVEL% NEQ 0 (",
        "    echo.",
        "    echo [Press any key to close...]",
        "    pause >nul",
        ")",
    ]
    return "\r\n".join(lines) + "\r\n"


def workshop_install_sh(mode: str) -> str:
    label = "DST_Buddy_Agent" if mode == "buddy" else "DST_Player_Agent"
    run_file = f"2_run_{mode}.sh"
    return f"""#!/usr/bin/env bash
set -euo pipefail

MOD_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
SRC_DIR="$MOD_DIR/runner"
INSTALL_DIR="${{DST_AGENT_HOME:-$HOME/.dst-hierarchical-agent}}"

sync_runner() {{
    mkdir -p "$INSTALL_DIR/data"
    for item in setup_and_run.sh scripts .env.example python; do
        if [ -e "$SRC_DIR/$item" ]; then
            rm -rf "$INSTALL_DIR/$item"
            cp -R "$SRC_DIR/$item" "$INSTALL_DIR/$item"
        fi
    done
    for df in "$SRC_DIR"/data/*; do
        [ -e "$df" ] || continue
        base="$(basename "$df")"
        [ "$base" = "agent_memory" ] && continue
        cp -R "$df" "$INSTALL_DIR/data/$base"
    done
    chmod +x "$INSTALL_DIR/setup_and_run.sh"
    cp "$SRC_DIR/version.txt" "$INSTALL_DIR/.workshop_runner_version"
}}

echo "============================================================"
echo "  DST Hierarchical Agent Workshop Installer (macOS / Linux)"
echo "============================================================"
echo "Install directory: $INSTALL_DIR"
sync_runner
"$INSTALL_DIR/setup_and_run.sh" --check-only

if [ -d "$HOME/Desktop" ]; then
    read -r -p "Create a Desktop launcher for {label}? [Y/n]: " ans
    if [ -z "$ans" ] || [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
        if [ "$(uname -s)" = "Darwin" ]; then
            shortcut="$HOME/Desktop/{label}.command"
            printf '#!/usr/bin/env bash\\nexec "%s/setup_and_run.sh" --mode {mode}\\n' "$INSTALL_DIR" > "$shortcut"
            chmod +x "$shortcut"
            echo "Created Desktop launcher: $shortcut"
        else
            shortcut="$HOME/Desktop/{label}.sh"
            printf '#!/usr/bin/env bash\\nexec "%s/setup_and_run.sh" --mode {mode}\\n' "$INSTALL_DIR" > "$shortcut"
            chmod +x "$shortcut"
            echo "Created Desktop launcher: $shortcut"
        fi
    fi
fi

echo "Done! Next time you can run ./{run_file} or your Desktop launcher."
read -r -p "Start now? [Y/n]: " start_now
if [ -z "$start_now" ] || [ "$start_now" = "y" ] || [ "$start_now" = "Y" ]; then
    exec "$INSTALL_DIR/setup_and_run.sh" --mode {mode}
fi
"""


def workshop_run_sh(mode: str) -> str:
    return f"""#!/usr/bin/env bash
set -euo pipefail

MOD_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
SRC_DIR="$MOD_DIR/runner"
INSTALL_DIR="${{DST_AGENT_HOME:-$HOME/.dst-hierarchical-agent}}"

if [ ! -x "$INSTALL_DIR/setup_and_run.sh" ] || [ ! -f "$INSTALL_DIR/.workshop_runner_version" ] \\
   || [ "$(cat "$SRC_DIR/version.txt")" != "$(cat "$INSTALL_DIR/.workshop_runner_version")" ]; then
    mkdir -p "$INSTALL_DIR/data"
    for item in setup_and_run.sh scripts .env.example python; do
        if [ -e "$SRC_DIR/$item" ]; then
            rm -rf "$INSTALL_DIR/$item"
            cp -R "$SRC_DIR/$item" "$INSTALL_DIR/$item"
        fi
    done
    for df in "$SRC_DIR"/data/*; do
        [ -e "$df" ] || continue
        base="$(basename "$df")"
        [ "$base" = "agent_memory" ] && continue
        cp -R "$df" "$INSTALL_DIR/data/$base"
    done
    chmod +x "$INSTALL_DIR/setup_and_run.sh"
    cp "$SRC_DIR/version.txt" "$INSTALL_DIR/.workshop_runner_version"
fi

exec "$INSTALL_DIR/setup_and_run.sh" --mode {mode}
"""


def collect_mod_files(mod_id: str, *, include_optional: bool = True) -> list[tuple[Path, str]]:
    source = ROOT / "mods" / mod_id
    files: list[Path] = [source / name for name in REQUIRED_ROOT_FILES]
    if include_optional:
        files.extend(source / name for name in OPTIONAL_ARCHIVE_FILES if (source / name).is_file())
    scripts = sorted(
        path for path in (source / "scripts").rglob("*.lua")
        if not path.name.endswith(("_config.lua", ".example.lua"))
    )
    if not scripts:
        raise FileNotFoundError(source / "scripts")
    files.extend(scripts)
    anim_dir = source / "anim"
    if anim_dir.is_dir():
        files.extend(sorted(anim_dir.glob("*.zip")))
    result: list[tuple[Path, str]] = []
    for path in files:
        if not path.is_file() or path.resolve() != path:
            raise ValueError(f"Missing or linked mod file: {path}")
        result.append((path, path.relative_to(source).as_posix()))
    return result


def collect_runner_files(*, include_docs: bool = True) -> list[tuple[Path, str]]:
    entries: list[tuple[Path, str]] = []
    file_list = RUNNER_RUNTIME_FILES + (RUNNER_DOC_FILES if include_docs else ())
    for rel in file_list:
        path = ROOT / rel
        if not path.is_file() or path.resolve() != path:
            raise ValueError(f"Missing or linked runner file: {path}")
        entries.append((path, rel))
    for pkg in RUNNER_PY_PACKAGES:
        pkg_dir = ROOT / "python" / pkg
        if not pkg_dir.is_dir():
            raise FileNotFoundError(pkg_dir)
        for path in sorted(pkg_dir.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if not path.is_file() or path.resolve() != path:
                raise ValueError(f"Missing or linked python file: {path}")
            entries.append((path, path.relative_to(ROOT).as_posix()))
    return entries


def verify_workshop_dir(stage_dir: Path, mod_id: str) -> None:
    meta = MODS[mod_id]
    config_name = meta["config_name"]
    port = meta["port"]
    run_stem = meta["run_stem"]

    for name in REQUIRED_ROOT_FILES:
        path = stage_dir / name
        if not path.is_file():
            raise ValueError(f"[{mod_id}] Missing required Workshop file: {name}")
    preview = stage_dir / "preview.jpg"
    preview_bytes = preview.read_bytes()
    if not preview_bytes.startswith(b"\xff\xd8\xff"):
        raise ValueError(f"[{mod_id}] preview.jpg is not a valid JPEG file")
    if len(preview_bytes) > MAX_PREVIEW_BYTES:
        raise ValueError(f"[{mod_id}] preview.jpg exceeds Steam Workshop 1 MB limit: {len(preview_bytes)} bytes")

    pairing_lua = stage_dir / "scripts" / "jev_bridge_pairing.lua"
    if not pairing_lua.is_file():
        raise ValueError(f"[{mod_id}] Missing Workshop pairing module: scripts/jev_bridge_pairing.lua")

    src_anim = ROOT / "mods" / mod_id / "anim"
    if src_anim.is_dir():
        for anim_src in sorted(src_anim.glob("*.zip")):
            staged_anim = stage_dir / "anim" / anim_src.name
            if not staged_anim.is_file():
                raise ValueError(f"[{mod_id}] Missing staged anim archive: anim/{anim_src.name}")
            with ZipFile(staged_anim, "r") as zf:
                names = set(zf.namelist())
                if "build.bin" not in names or "atlas-0.tex" not in names:
                    raise ValueError(f"[{mod_id}] Invalid anim archive anim/{anim_src.name}")

    config_path = stage_dir / "scripts" / config_name
    expected = blank_config_lua(port)
    actual = config_path.read_text(encoding="utf-8")
    if actual != expected:
        raise ValueError(f"[{mod_id}] Config file is not clean/blank: {config_path}")

    for script_name in ("1_install_agent.bat", f"{run_stem}.bat", "1_install_agent.sh", f"{run_stem}.sh"):
        script_path = stage_dir / script_name
        if not script_path.is_file():
            raise ValueError(f"[{mod_id}] Missing Workshop launcher script: {script_name}")
        raw = script_path.read_bytes()
        if script_name.endswith(".bat") and raw.startswith(b"\xef\xbb\xbf"):
            raise ValueError(f"[{mod_id}] BAT file must not have UTF-8 BOM: {script_name}")
        if script_name.endswith(".sh") and b"\r\n" in raw:
            raise ValueError(f"[{mod_id}] Shell script must use LF line endings: {script_name}")

    for req_runner in (
        "runner/version.txt",
        "runner/scripts/workshop_install.ps1",
        "runner/scripts/launcher.ps1",
        "runner/setup_and_run.sh",
        "runner/.env.example",
        "runner/data/DSTKnowledge.txt",
        "runner/data/worldgen_resources.json",
        "runner/python/requirements-mcp.txt",
        "runner/python/executor/team_runner.py",
        "runner/python/executor/companion_tasks.py",
        "runner/python/mcp_server/companion_server.py",
        "runner/python/mcp_agent/companion_agent.py",
    ):
        if not (stage_dir / req_runner).is_file():
            raise ValueError(f"[{mod_id}] Missing embedded offline runner file: {req_runner}")

    for path in stage_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(stage_dir).as_posix()
        if path.name.endswith(".example.lua") or path.name.upper() == "README.MD":
            raise ValueError(f"[{mod_id}] Development file leaked into Workshop stage: {rel}")
        if path.name == ".env" or "/logs/" in f"/{rel}" or "/agent_memory/" in f"/{rel}" or "__pycache__" in path.parts:
            raise ValueError(f"[{mod_id}] Sensitive/runtime file leaked into Workshop stage: {rel}")
        if "/tests/" in f"/{rel}":
            raise ValueError(f"[{mod_id}] Test file leaked into Workshop stage: {rel}")

    modinfo = (stage_dir / "modinfo.lua").read_text(encoding="utf-8")
    for field in ("name", "version", "author", "api_version", "dst_compatible", "icon_atlas", "icon"):
        if not re.search(rf"(?m)^\s*{field}\s*=", modinfo):
            raise ValueError(f"[{mod_id}] modinfo.lua missing required field: {field}")


def build_archive(mod_id: str, archive: Path) -> None:
    meta = MODS[mod_id]
    config_name = meta["config_name"]
    port = meta["port"]
    entries = collect_mod_files(mod_id, include_optional=True)
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
        for path, rel in entries:
            bundle.write(path, f"{mod_id}/{rel}")
        # Installed configuration can contain a local token. Generate a blank one.
        bundle.writestr(f"{mod_id}/scripts/{config_name}", blank_config_lua(port))


def build_runner_bundle(archive: Path) -> None:
    entries = collect_runner_files(include_docs=True)
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
        for path, rel in entries:
            bundle.write(path, f"dst_agent_runner/{rel}")


def build_workshop_dir(mod_id: str, target_dir: Path) -> Path:
    meta = MODS[mod_id]
    config_name = meta["config_name"]
    port = meta["port"]
    mode = meta["mode"]
    run_stem = meta["run_stem"]

    entries = collect_mod_files(mod_id, include_optional=False)
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    for path, rel in entries:
        dest = target_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
    config_dest = target_dir / "scripts" / config_name
    config_dest.parent.mkdir(parents=True, exist_ok=True)
    config_dest.write_text(blank_config_lua(port), encoding="utf-8")

    # Embed self-contained offline Python runner + pure-English one-click scripts.
    mod_version = extract_mod_version((target_dir / "modinfo.lua").read_text(encoding="utf-8"))
    runner_dir = target_dir / "runner"
    for path, rel in collect_runner_files(include_docs=False):
        dest = runner_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
    (runner_dir / "version.txt").write_text(f"{mod_id}:{mod_version}\n", encoding="utf-8")

    (target_dir / "1_install_agent.bat").write_bytes(workshop_install_bat(mode).encode("utf-8"))
    (target_dir / f"{run_stem}.bat").write_bytes(workshop_run_bat(mode).encode("utf-8"))
    (target_dir / "1_install_agent.sh").write_bytes(workshop_install_sh(mode).encode("utf-8"))
    (target_dir / f"{run_stem}.sh").write_bytes(workshop_run_sh(mode).encode("utf-8"))

    verify_workshop_dir(target_dir, mod_id)
    return target_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--skip-zip", action="store_true", help="Skip .zip mod release archives")
    parser.add_argument("--skip-workshop", action="store_true", help="Skip Steam Workshop staging folders")
    parser.add_argument("--skip-runner", action="store_true", help="Skip dst_agent_runner.zip GitHub bundle")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not args.skip_zip:
        with TemporaryDirectory(dir=output) as staging:
            archives = [Path(staging) / f"{mod_id}.zip" for mod_id in MODS]
            for mod_id, archive in zip(MODS, archives):
                build_archive(mod_id, archive)
            for archive in archives:
                print(archive.replace(output / archive.name))
    if not args.skip_runner:
        with TemporaryDirectory(dir=output) as staging:
            runner = Path(staging) / "dst_agent_runner.zip"
            build_runner_bundle(runner)
            print(runner.replace(output / runner.name))
    if not args.skip_workshop:
        workshop_root = output / "workshop"
        workshop_root.mkdir(parents=True, exist_ok=True)
        for mod_id in MODS:
            staged = build_workshop_dir(mod_id, workshop_root / mod_id)
            print(staged)


if __name__ == "__main__":
    main()
