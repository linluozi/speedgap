"""Command line interface for speedgap."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .core import EXIT_ROBUST, __version__, compare
from .spec import SpecError, load_spec

EXIT_CONFIG = 3


def _fmt_group(label: str, segment: Optional[Dict[str, Any]]) -> str:
    if not segment:
        return "%-10s (no inputs)" % label
    return (
        "%-10s inputs=%-5d passes=%-3d calls/pass=%-5d median=%7.3fx  "
        "p25=%6.3f  p75=%6.3f"
        % (
            label,
            segment.get("inputs", 0),
            segment["n"],
            segment["calls_per_pass"],
            segment["median"],
            segment["p25"],
            segment["p75"],
        )
    )


def format_report(report: Dict[str, Any]) -> str:
    """Human-readable summary built only from numbers in ``report``."""
    lines: List[str] = [
        "speedgap %s  %s" % (report["version"], report["generated_at"]),
    ]
    if report.get("synthetic") is None and report.get("mismatches"):
        lines.append("semantics failed before timing; no speedup is reported")
    else:
        lines.append(_fmt_group("synthetic", report.get("synthetic")))
        lines.append(_fmt_group("real", report.get("real")))
    if report.get("gap") is not None:
        lines.append(
            "gap        %6.2fx  (synthetic median / real median)" % report["gap"]
        )
    if report.get("repeat_inflation") is not None:
        lines.append(
            "repeat inflation %.2fx  (isolated real median / workload real median)"
            % report["repeat_inflation"]
        )
    lines.append(
        "verdict    %s  (exit %d)" % (report["verdict"], report["exit_code"])
    )
    for warning in report.get("warnings") or []:
        lines.append("warning    " + warning)
    mismatches = report.get("mismatches") or []
    if mismatches:
        lines.append("mismatches %d (showing up to 5)" % len(mismatches))
        for item in mismatches[:5]:
            base = item["baseline"]
            cand = item["candidate"]
            lines.append(
                "  %-24s baseline=%s candidate=%s"
                % (
                    item["name"],
                    base.get("sha256", base.get("type", "?"))[:16],
                    cand.get("sha256", cand.get("type", "?"))[:16],
                )
            )
        if len(mismatches) > 5:
            lines.append("  ... and %d more" % (len(mismatches) - 5))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="speedgap",
        description=(
            "Measure whether a speedup that shows up on synthetic inputs also "
            "shows up on held-out real inputs."
        ),
    )
    parser.add_argument("--version", action="version", version="speedgap " + __version__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run a benchmark spec")
    run.add_argument("--spec", required=True, help="path to the JSON spec")
    run.add_argument("--warmup", type=int, default=None, help="warmup calls per input")
    run.add_argument("--reps", type=int, default=None, help="timed samples per input")
    run.add_argument("--repeat", type=int, default=None, help="calls per timed sample")
    run.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="minimum real-input speedup for a 'robust' verdict (default 1.15)",
    )
    run.add_argument("--json", dest="json_path", default=None, help="write the full report")
    run.add_argument("--quiet", action="store_true", help="do not print the summary")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        spec = load_spec(Path(args.spec))
        kwargs: Dict[str, Any] = {}
        for key in ("warmup", "reps", "repeat"):
            value = getattr(args, key)
            if value is not None:
                kwargs[key] = value
            elif key in spec:
                kwargs[key] = spec[key]
        threshold = args.threshold if args.threshold is not None else spec.get("threshold")
        if threshold is not None:
            kwargs["threshold"] = float(threshold)
        report = compare(
            spec["baseline"],
            spec["candidate"],
            spec["synthetic"],
            spec["real"],
            serializer=spec["serializer"],
            **kwargs,
        )
    except (SpecError, ValueError, OSError, ImportError) as exc:
        print("speedgap: %s" % exc, file=sys.stderr)
        return EXIT_CONFIG

    if args.json_path:
        target = Path(args.json_path)
        if target.parent and str(target.parent) != ".":
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    if not args.quiet:
        print(format_report(report))
    return int(report["exit_code"])


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
