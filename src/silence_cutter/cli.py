"""Command-line entry point for project inspection and audio analysis."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .project import KdenliveProject
from .silence import detect_silence


def main() -> None:
    parser = argparse.ArgumentParser(prog="kdenlive-silence-cutter")
    parser.add_argument("project", type=Path, help=".kdenlive project")
    parser.add_argument("--audio", type=Path, help="audio media file to analyze")
    parser.add_argument("--threshold", type=float, default=-40.0, help="silence threshold in dB")
    parser.add_argument("--minimum", type=float, default=0.5, help="minimum silence duration in seconds")
    parser.add_argument("--padding", type=float, default=0.1,
                        help="speech-edge padding retained at each side of a silence (seconds)")
    parser.add_argument("--write", action="store_true",
                        help="add a silence-cut sequence to the project file in place")
    parser.add_argument("--output", type=Path,
                        help="write to a separate project file instead of modifying the input")
    args = parser.parse_args()
    try:
        project = KdenliveProject(args.project)
        report = {"project": str(args.project), "tracks": [t.__dict__ for t in project.tracks()],
                  "group_metadata": project.group_metadata()}
        if args.audio:
            if args.padding < 0:
                parser.error("--padding must be non-negative")
            silences = detect_silence(args.audio, noise_db=args.threshold,
                                      min_duration=args.minimum)
            report["silences"] = [i.__dict__ for i in silences]
            if args.write:
                cuts = [(i.start + args.padding, i.end - args.padding)
                        for i in silences if i.duration > 2 * args.padding]
                report["cut_intervals"] = [
                    {"start": start, "end": end} for start, end in cuts]
                output = args.output or args.project
                removed_frames = project.apply_timeline_cuts(
                    args.audio, cuts, output)
                report["output"] = str(output)
                report["removed_frames"] = removed_frames
        elif args.write:
            parser.error("--write requires --audio")
        print(json.dumps(report, indent=2))
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
