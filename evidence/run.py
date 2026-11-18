#!/usr/bin/env python3
"""Re-run the three demo scenarios and the A/A null experiment.

Every number in README.md comes from this script.  It uses the public
``speedgap.compare`` API, so the evidence is produced by the same code path the
CLI uses.

Usage:
    python evidence/run.py                 # writes evidence/results.json
    python evidence/run.py --out /tmp/r.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

import impls  # noqa: E402
from speedgap import __version__, compare, environment_info  # noqa: E402
from speedgap.spec import load_spec  # noqa: E402


SCENARIOS = (
    ("genuine", "spec-genuine.json"),
    ("one-entry-cache", "spec-identity-cache.json"),
    ("drift", "spec-drift.json"),
)


def run_scenario(spec_name: str) -> dict:
    spec = load_spec(ROOT / "examples" / spec_name)
    report = compare(
        spec["baseline"],
        spec["candidate"],
        spec["synthetic"],
        spec["real"],
        warmup=spec.get("warmup", 3),
        reps=spec.get("reps", 5),
        repeat=spec.get("repeat", 1),
        serializer=spec["serializer"],
    )
    return report


def aa_null(reps_runs: int = 7) -> dict:
    """Baseline vs itself on the same synthetic/real inputs.

    A tool that calls this pair "robust" is producing a false positive.  The
    reported rate is the observed one for this machine and sample size, not a
    theoretical guarantee.
    """
    synthetic = [case for case in load_spec(
        ROOT / "examples" / "spec-genuine.json"
    )["synthetic"][:30]]
    real = load_spec(ROOT / "examples" / "spec-genuine.json")["real"]
    runs = []
    for _ in range(reps_runs):
        report = compare(
            impls.baseline_scan,
            impls.baseline_scan,
            synthetic,
            real,
            warmup=2,
            reps=5,
        )
        runs.append(
            {
                "real_median": report["real"]["median"],
                "real_p25": report["real"]["p25"],
                "real_p75": report["real"]["p75"],
                "synthetic_median": report["synthetic"]["median"],
                "verdict": report["verdict"],
            }
        )
    false_positives = sum(1 for run in runs if run["verdict"] == "robust")
    median_only = sum(1 for run in runs if run["real_median"] >= 1.15)
    return {
        "runs": runs,
        "run_count": len(runs),
        "false_positive_count": false_positives,
        "false_positive_rate": false_positives / len(runs),
        "median_only_robust_count": median_only,
        "median_only_robust_rate": median_only / len(runs),
        "real_median_min": min(run["real_median"] for run in runs),
        "real_median_max": max(run["real_median"] for run in runs),
        "verdicts": sorted({run["verdict"] for run in runs}),
    }


def summary(results: dict) -> str:
    lines = ["speedgap %s evidence" % results["version"], ""]
    for name, report in results["scenarios"].items():
        lines.append(
            "%-16s verdict=%-16s synth=%7.3fx real=%7.3fx gap=%s repeat_inflation=%s"
            % (
                name,
                report["verdict"],
                report["synthetic"]["median"] if report["synthetic"] else float("nan"),
                report["real"]["median"] if report["real"] else float("nan"),
                ("%.2fx" % report["gap"]) if report["gap"] is not None else "n/a",
                (
                    "%.2fx" % report["repeat_inflation"]
                    if report["repeat_inflation"] is not None
                    else "n/a"
                ),
            )
        )
    aa = results["aa_null"]
    lines.append("")
    lines.append(
        "A/A null: %d runs, robust verdicts=%d (%.1f%%), median-only rule would "
        "say %d (%.1f%%), real median range %.3f-%.3f"
        % (
            aa["run_count"],
            aa["false_positive_count"],
            100 * aa["false_positive_rate"],
            aa["median_only_robust_count"],
            100 * aa["median_only_robust_rate"],
            aa["real_median_min"],
            aa["real_median_max"],
        )
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out", default=str(ROOT / "evidence" / "results.json")
    )
    parser.add_argument("--aa-runs", type=int, default=7)
    args = parser.parse_args()

    scenarios = {}
    for name, spec_name in SCENARIOS:
        scenarios[name] = run_scenario(spec_name)
    results = {
        "tool": "speedgap",
        "version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "environment": environment_info(),
        "python": sys.version,
        "scenarios": scenarios,
        "aa_null": aa_null(args.aa_runs),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(summary(results))
    print("\nwrote %s (%d bytes)" % (out, out.stat().st_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
