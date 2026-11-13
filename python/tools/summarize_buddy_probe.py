"""Read client-side probe measurements; never combine clocks across machines."""
import argparse
import json
from pathlib import Path

MARKER = "[JEV_BUDDY_PROBE]"


def summaries(lines):
    result = []
    for line in lines:
        marker = line.find(MARKER)
        if marker < 0:
            continue
        try:
            row = json.loads(line[marker + len(MARKER):].strip())
        except (ValueError, TypeError):
            continue
        if isinstance(row, dict) and row.get("side") == "client" and row.get("event") == "summary":
            result.append(row)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", nargs="?", type=Path,
                        default=Path.home() / "Documents/Klei/DoNotStarveTogether/client_log.txt")
    parser.add_argument("--all", action="store_true", help="Print every summary instead of the latest")
    args = parser.parse_args()
    rows = summaries(args.log.read_text(encoding="utf-8-sig", errors="replace").splitlines())
    if not rows:
        parser.exit(1, "No client probe summary found. Run /buddyprobe run in game first.\n")
    if rows[-1].get("clock_validated") is not True or (rows[-1].get("counts") or {}).get("clock_invalid", 0):
        parser.exit(2, "Latest probe run has an unvalidated/invalid clock; its numbers are not usable latency evidence. "
                       "Reload probe 0.1.1+ and run /buddyprobe run again.\n")
    print(json.dumps(rows if args.all else rows[-1], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
