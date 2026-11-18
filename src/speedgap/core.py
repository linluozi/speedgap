"""Paired synthetic-vs-real speedup measurement.

Two measurements are made, and they answer different questions:

``workload``
    Every input is called once per pass, inputs are interleaved and the pass is
    rotated.  The verdict is the median of paired per-pass ratios.  This is the
    number that should be quoted: a candidate that only wins because the same
    input is fed to it repeatedly does not win here.

``isolated``
    Each input is timed on its own, several times in a row.  This is useful for
    attributing cost, but it *rewards* memoisation: the second call with the
    same input is a cache hit.  speedgap reports the ratio between the two
    real-input measurements as ``repeat inflation`` and warns when it is large.

Before either timing pass, every output is serialised and compared
byte-for-byte.  One disagreement makes the run ``semantic-mismatch`` and no
speedup number is produced at all.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

__version__ = "1.0.0"

THRESHOLD = 1.15
REGRESSION = 0.95
REPEAT_INFLATION_WARN = 1.50
SCHEMA_VERSION = 1

EXIT_ROBUST = 0
EXIT_FAILED = 1
EXIT_MISMATCH = 2

Timer = Callable[[], int]


@dataclass(frozen=True)
class Case:
    """One benchmark input."""

    name: str
    payload: Any = None
    group: str = "real"

    def __post_init__(self) -> None:
        if self.group not in ("synthetic", "real"):
            raise ValueError("group must be 'synthetic' or 'real'")


def quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile (numpy's default convention)."""
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be in [0, 1]")
    xs = sorted(float(v) for v in values)
    if not xs:
        raise ValueError("quantile() of an empty sequence")
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac


def summarize(values: Iterable[float]) -> Dict[str, float]:
    xs = [float(v) for v in values]
    if not xs:
        raise ValueError("summarize() of an empty sequence")
    mean = statistics.fmean(xs)
    stdev = statistics.stdev(xs) if len(xs) > 1 else 0.0
    return {
        "n": len(xs),
        "median": statistics.median(xs),
        "p25": quantile(xs, 0.25),
        "p75": quantile(xs, 0.75),
        "min": min(xs),
        "max": max(xs),
        "mean": mean,
        "rel_stdev": (stdev / mean) if mean else 0.0,
    }


def default_serializer(value: Any) -> bytes:
    """Stable-enough bytes for equality checks; custom serializers are allowed."""
    try:
        return json.dumps(
            value, sort_keys=True, ensure_ascii=False, default=repr, allow_nan=True
        ).encode("utf-8")
    except (TypeError, ValueError):
        return repr(value).encode("utf-8")


def observe(
    fn: Callable[[Any], Any],
    payload: Any,
    serializer: Optional[Callable[[Any], Any]] = None,
) -> Dict[str, Any]:
    """Run one callable and record a comparable outcome.

    Exceptions count as behaviour: type name and message are both part of the
    signature, so a candidate that starts raising a different message fails the
    equality gate just like one that returns a different value.
    """
    try:
        value = fn(payload)
    except Exception as exc:  # noqa: BLE001 - the failure *is* the observation
        blob = json.dumps(
            ["raise", type(exc).__name__, str(exc)], ensure_ascii=False
        ).encode("utf-8")
        return {
            "kind": "raise",
            "type": type(exc).__name__,
            "message": str(exc),
            "data": blob,
        }
    data = (serializer or default_serializer)(value)
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("serializer must return str or bytes")
    data = bytes(data)
    return {
        "kind": "value",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "data": data,
    }


def _check_args(warmup: int, reps: int, repeat: int) -> None:
    if reps < 1:
        raise ValueError("reps must be >= 1")
    if warmup < 0:
        raise ValueError("warmup must be >= 0")
    if repeat < 1:
        raise ValueError("repeat must be >= 1")


def measure_workload(
    fn: Callable[[Any], Any],
    cases: Sequence[Case],
    *,
    warmup: int = 3,
    reps: int = 5,
    repeat: int = 1,
    timer: Timer = time.perf_counter_ns,
) -> Dict[str, Any]:
    """Time full interleaved passes over ``cases``.

    Each sample is one pass over every input, divided by the number of calls, so
    caching across *different* inputs is measured where it actually happens.
    Pass order is rotated by one position per repetition to avoid a fixed-order
    advantage.  Every raw sample is returned.
    """
    _check_args(warmup, reps, repeat)
    if not cases:
        raise ValueError("measure_workload() needs at least one case")
    for _ in range(warmup):
        for case in cases:
            fn(case.payload)
    samples: List[float] = []
    calls = len(cases) * repeat
    for rep in range(reps):
        order = list(cases[rep % len(cases):]) + list(cases[: rep % len(cases)])
        start = timer()
        for _ in range(repeat):
            for case in order:
                fn(case.payload)
        elapsed = timer() - start
        if elapsed < 0:
            raise ValueError("timer moved backwards")
        samples.append(elapsed / calls)
    if min(samples) <= 0.0:
        raise ValueError(
            "timer produced a non-positive duration; increase --repeat or use a "
            "finer clock"
        )
    return {
        "mode": "workload",
        "calls": calls,
        "passes": reps,
        "samples_ns": samples,
        "median_ns": statistics.median(samples),
    }


def measure_isolated(
    fn: Callable[[Any], Any],
    cases: Sequence[Case],
    *,
    warmup: int = 3,
    reps: int = 5,
    repeat: int = 1,
    timer: Timer = time.perf_counter_ns,
) -> Dict[str, Any]:
    """Time each input on its own; diagnostic only (rewards memoisation)."""
    _check_args(warmup, reps, repeat)
    rows: List[Dict[str, Any]] = []
    for case in cases:
        for _ in range(warmup):
            fn(case.payload)
        samples: List[float] = []
        for _ in range(reps):
            start = timer()
            for _ in range(repeat):
                fn(case.payload)
            elapsed = timer() - start
            if elapsed < 0:
                raise ValueError("timer moved backwards")
            samples.append(elapsed / repeat)
        if min(samples) <= 0.0:
            raise ValueError("timer produced a non-positive duration for %r" % case.name)
        rows.append(
            {
                "name": case.name,
                "group": case.group,
                "samples_ns": samples,
                "median_ns": statistics.median(samples),
            }
        )
    return {"mode": "isolated", "rows": rows}


def _segment_workload(base: Dict[str, Any], cand: Dict[str, Any]) -> Dict[str, Any]:
    ratios = [b / c for b, c in zip(base["samples_ns"], cand["samples_ns"])]
    agg = summarize(ratios)
    agg.update(
        {
            "baseline_ns_per_call": base["median_ns"],
            "candidate_ns_per_call": cand["median_ns"],
            "calls_per_pass": base["calls"],
            "samples_ns": ratios,
        }
    )
    return agg


def _segment_isolated(
    base: Dict[str, Any], cand: Dict[str, Any]
) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for b, c in zip(base["rows"], cand["rows"]):
        rows.append(
            {
                "name": b["name"],
                "baseline_ns": b["median_ns"],
                "candidate_ns": c["median_ns"],
                "speedup": b["median_ns"] / c["median_ns"],
                "group": b["group"],
            }
        )
    agg = summarize([row["speedup"] for row in rows])
    worst = min(rows, key=lambda row: row["speedup"])
    best = max(rows, key=lambda row: row["speedup"])
    total_base = sum(row["baseline_ns"] for row in rows)
    agg.update(
        {
            "total_ratio": total_base / sum(row["candidate_ns"] for row in rows),
            "worst": {"name": worst["name"], "speedup": worst["speedup"]},
            "best": {"name": best["name"], "speedup": best["speedup"]},
            "rows": rows,
        }
    )
    return agg


def classify(
    synthetic_speedup: float,
    real_speedup: float,
    mismatches: int,
    *,
    threshold: float = THRESHOLD,
    regression: float = REGRESSION,
    real_p25: Optional[float] = None,
) -> str:
    """Map the workload speedups to a verdict.

    ``robust`` also requires the real-input p25 to be at least 1.0: a median
    above the threshold with a quarter of passes slower than baseline is not a
    transferable speedup, it is a wide distribution.  That case is reported as
    ``unstable`` instead of being rounded up.
    """
    if mismatches:
        return "semantic-mismatch"
    if real_speedup < regression:
        return "regression"
    if real_speedup >= threshold:
        if real_p25 is not None and real_p25 < 1.0:
            return "unstable"
        return "robust"
    if synthetic_speedup >= threshold:
        return "synthetic-only"
    return "no-gain"


EXIT_CODES = {
    "robust": EXIT_ROBUST,
    "no-gain": EXIT_FAILED,
    "synthetic-only": EXIT_FAILED,
    "regression": EXIT_FAILED,
    "unstable": EXIT_FAILED,
    "semantic-mismatch": EXIT_MISMATCH,
}


def _report_base(
    *,
    warmup: int,
    reps: int,
    repeat: int,
    threshold: float,
    serializer: Optional[Callable[[Any], Any]],
    environment: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "tool": "speedgap",
        "version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": {
            "warmup": warmup,
            "reps": reps,
            "repeat": repeat,
            "threshold": threshold,
            "timer": "time.perf_counter_ns",
            "custom_serializer": serializer is not None,
        },
        "environment": environment or environment_info(),
    }


def compare(
    baseline: Callable[[Any], Any],
    candidate: Callable[[Any], Any],
    synthetic: Sequence[Case],
    real: Sequence[Case],
    *,
    warmup: int = 3,
    reps: int = 5,
    repeat: int = 1,
    threshold: float = THRESHOLD,
    serializer: Optional[Callable[[Any], Any]] = None,
    timer: Timer = time.perf_counter_ns,
    environment: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run semantics, workload timing, isolated timing, then classify."""
    _check_args(warmup, reps, repeat)
    if not synthetic and not real:
        raise ValueError("at least one synthetic or real input is required")
    if threshold <= 0:
        raise ValueError("threshold must be positive")

    started = time.perf_counter()
    cases = list(synthetic) + list(real)
    mismatches: List[Dict[str, Any]] = []
    for case in cases:
        expected = observe(baseline, case.payload, serializer)
        actual = observe(candidate, case.payload, serializer)
        if expected["data"] != actual["data"]:
            mismatches.append(
                {
                    "name": case.name,
                    "group": case.group,
                    "baseline": {
                        k: expected[k] for k in ("kind", "sha256", "size") if k in expected
                    },
                    "candidate": {
                        k: actual[k] for k in ("kind", "sha256", "size") if k in actual
                    },
                }
            )
            if len(mismatches) > 200:
                raise RuntimeError(
                    "more than 200 semantic mismatches; is the candidate wired to "
                    "the right function?"
                )

    report = _report_base(
        warmup=warmup,
        reps=reps,
        repeat=repeat,
        threshold=threshold,
        serializer=serializer,
        environment=environment,
    )
    if mismatches:
        report.update(
            {
                "synthetic": None,
                "real": None,
                "isolated": None,
                "gap": None,
                "repeat_inflation": None,
                "verdict": "semantic-mismatch",
                "exit_code": EXIT_MISMATCH,
                "mismatches": mismatches,
                "mismatch_count": len(mismatches),
                "warnings": [],
                "total_seconds": round(time.perf_counter() - started, 3),
            }
        )
        return report

    n_synth = len(synthetic)

    def workload_group(
        fn: Callable[[Any], Any], group: Sequence[Case]
    ) -> Dict[str, Any]:
        if not group:
            return {"mode": "workload", "calls": 0, "passes": 0, "samples_ns": []}
        return measure_workload(
            fn, group, warmup=warmup, reps=reps, repeat=repeat, timer=timer
        )

    synth_wl = (
        _segment_workload(
            workload_group(baseline, synthetic),
            workload_group(candidate, synthetic),
        )
        if synthetic
        else None
    )
    real_wl = (
        _segment_workload(
            workload_group(baseline, real),
            workload_group(candidate, real),
        )
        if real
        else None
    )
    iso_base = measure_isolated(
        baseline, cases, warmup=warmup, reps=reps, repeat=repeat, timer=timer
    )
    iso_cand = measure_isolated(
        candidate, cases, warmup=warmup, reps=reps, repeat=repeat, timer=timer
    )
    synth_iso = (
        _segment_isolated(
            {"rows": iso_base["rows"][:n_synth]},
            {"rows": iso_cand["rows"][:n_synth]},
        )
        if synthetic
        else None
    )
    real_iso = (
        _segment_isolated(
            {"rows": iso_base["rows"][n_synth:]},
            {"rows": iso_cand["rows"][n_synth:]},
        )
        if real
        else None
    )

    if synth_wl is not None:
        synth_wl["inputs"] = len(synthetic)
    if real_wl is not None:
        real_wl["inputs"] = len(real)
    synth_speedup = synth_wl["median"] if synth_wl else 1.0
    real_speedup = real_wl["median"] if real_wl else 1.0
    verdict = classify(
        synth_speedup,
        real_speedup,
        mismatches=0,
        threshold=threshold,
        real_p25=real_wl["p25"] if real_wl else None,
    )
    gap = (synth_speedup / real_speedup) if synth_wl and real_wl else None
    repeat_inflation = None
    if real_iso and real_wl and real_wl["median"] > 0:
        repeat_inflation = real_iso["median"] / real_wl["median"]

    warnings: List[str] = []
    if repeat_inflation is not None and repeat_inflation >= REPEAT_INFLATION_WARN:
        warnings.append(
            "repeat inflation %.2fx: the candidate looks %.2fx faster when one "
            "input is repeated, but only %.2fx faster on interleaved real inputs; "
            "the workload number is used for the verdict"
            % (repeat_inflation, real_iso["median"], real_wl["median"])
        )
    if real_wl and real_wl["p25"] < 1.0:
        warnings.append(
            "at least 25%% of real passes were slower than baseline (p25 %.3fx)"
            % real_wl["p25"]
        )
    if verdict == "unstable":
        warnings.append(
            "real median %.3fx clears the %.2fx threshold but the p25 is %.3fx; "
            "the speedup is not stable across passes"
            % (real_speedup, threshold, real_wl["p25"])
        )
    if real_iso:
        noisy = 0
        for row in real_iso["rows"]:
            if row["baseline_ns"] < 2000:
                noisy += 1
                continue
            if row["speedup"] < REGRESSION:
                warnings.append(
                    "real input %r is %.3fx in isolated timing"
                    % (row["name"], row["speedup"])
                )
        if noisy:
            warnings.append(
                "%d real input(s) run in under 2 us; their isolated ratios are "
                "timer-noise dominated and were excluded from regression warnings"
                % noisy
            )

    report.update(
        {
            "synthetic": synth_wl,
            "real": real_wl,
            "isolated": {"synthetic": synth_iso, "real": real_iso},
            "gap": gap,
            "repeat_inflation": repeat_inflation,
            "verdict": verdict,
            "exit_code": EXIT_CODES[verdict],
            "mismatches": [],
            "mismatch_count": 0,
            "warnings": warnings,
            "total_seconds": round(time.perf_counter() - started, 3),
        }
    )
    return report


def environment_info() -> Dict[str, Any]:
    """Facts recorded next to every report so two runs are comparable."""
    info: Dict[str, Any] = {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
    }
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("model name"):
                    info["cpu_model"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    info["mem_total_kb"] = int(line.split()[1])
                    break
    except (OSError, ValueError):
        pass
    return info
