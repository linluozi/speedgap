# speedgap

**Does the speedup survive real inputs?**

`speedgap` measures the same pair of callables on two input sets — synthetic
inputs and held-out real inputs — and reports one verdict: `robust`,
`synthetic-only`, `no-gain`, `regression`, `unstable`, or `semantic-mismatch`.  It refuses to
report a speedup at all when the two implementations disagree on a single byte
of output.

Upstream: none

This is an original tool, not a fork or a patch of another project.  It exists
because a benchmark that repeats one input will happily report 100x for an
optimisation that does nothing in production.  Two measurements are taken and
compared:

| measurement | how | used for |
| --- | --- | --- |
| **workload** | every input called once per pass, inputs interleaved, pass order rotated | the verdict |
| **isolated** | one input timed several times in a row | per-input attribution only |

The ratio between the two on real inputs is reported as **repeat inflation**.
A candidate that memoises the last document scores high on isolated timing, no
gain on workload timing, and triggers a warning instead of a trophy.

```
$ speedgap run --spec examples/spec-identity-cache.json
speedgap 1.0.0  2026-11-18T06:10:22+00:00
synthetic  inputs=200   passes=5   calls/pass=200   median=140.899x  p25=140.602  p75=141.277
real       inputs=8     passes=5   calls/pass=8     median=  0.978x  p25= 0.924  p75= 0.984
gap        144.10x  (synthetic median / real median)
repeat inflation 37.21x  (isolated real median / workload real median)
verdict    synthetic-only  (exit 1)
warning    repeat inflation 37.21x: the candidate looks 36.39x faster when one
           input is repeated, but only 0.98x faster on interleaved real inputs;
           the workload number is used for the verdict
```

## Install

```bash
git clone git@github.com:linluozi/speedgap.git
cd speedgap
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

Python 3.10+ and the standard library only.  No third-party runtime dependency.

## Quick start

```bash
# 1. run the three shipped scenarios
speedgap run --spec examples/spec-genuine.json          # exit 0, robust
speedgap run --spec examples/spec-identity-cache.json  # exit 1, synthetic-only
speedgap run --spec examples/spec-drift.json           # exit 2, semantic-mismatch

# 2. machine-readable report (every raw sample included)
speedgap run --spec examples/spec-genuine.json --json report.json

# 3. reproduce the numbers in this README
python evidence/run.py
```

A spec is JSON and points at a baseline callable and a candidate callable:

```json
{
  "baseline": "impls:baseline_scan",
  "candidate": "impls:candidate_scan",
  "synthetic": {"kind": "generated", "factory": "impls:make_docs", "count": 300, "seed": 20261118},
  "real": {"kind": "files", "paths": ["corpus"], "encoding": "utf-8"},
  "reps": 5,
  "warmup": 3
}
```

`module:function` and `path/to/file.py:function` are both accepted.  Real
inputs can also be inline (`{"kind": "inline", "values": [...]}`); synthetic
inputs can be a seeded factory or inline.  Functions take exactly one argument;
wrap multi-argument APIs in a one-argument lambda.

## What the evidence says

All numbers below are from `python evidence/run.py` on 2026-11-18, committed in
`evidence/results.json` with every raw sample.  The CLI transcript above is an
independent run of the same spec; run-to-run variation is expected and is
quantified by the A/A experiment below.

| scenario | synthetic speedup | real speedup | repeat inflation | verdict |
| --- | --- | --- | --- | --- |
| `genuine` — `str.count` vs a Python character loop | 13.522x | 19.767x | 0.478x | `robust` |
| `one-entry-cache` — `lru_cache(maxsize=1)` wrapper | 138.752x | 0.983x | 35.746x | `synthetic-only` |
| `drift` — NBSP normalisation | not measured | not measured | — | `semantic-mismatch` (1 of 8 real inputs) |

The `drift` candidate is byte-identical on all 50 synthetic documents and
changes exactly one real file (`unicode.txt`).  Timing never runs: the report
says `semantic-mismatch`, exit code 2.

**A/A null:** baseline against itself, 100 independent runs, 30 synthetic + 8
real inputs.  Two runs were called `robust` (**2/100 = 2.0%**); the real-input
median ranged **0.949x – 1.154x**.  The same two runs would also pass a
median-only rule, so the quartile guard does not explain them away: a 1.15x
threshold over five passes on this shared host has a measured ~2% false-positive
rate.  Use `--reps 9` or more, and treat a single run as evidence of a gap, not
as proof of a win.

The `genuine` gap is 0.68x (real inputs are *larger* than the synthetic ones, so
the real speedup is higher).  A gap below 1 is not a failure: `gap` is
informational, the verdict comes from the real-input workload speedup alone.

## Verdicts and exit codes

| verdict | condition (workload medians) | exit |
| --- | --- | --- |
| `semantic-mismatch` | any input's serialised output (or exception type/message) differs | 2 |
| `regression` | real < 0.95x | 1 |
| `robust` | real ≥ 1.15x (configurable with `--threshold`) | 0 |
| `unstable` | real ≥ 1.15x but its p25 < 1.0x | 1 |
| `synthetic-only` | synthetic ≥ 1.15x but real < 1.15x | 1 |
| `no-gain` | neither reaches the threshold | 1 |

`--threshold` changes the bar; `--reps`, `--warmup` and `--repeat` change the
sampling.  A configuration error (bad spec, unimportable target, non-positive
duration) exits **3** and never writes a verdict.

## Design notes

- **Semantics first.**  Every input is executed by both callables and the
  outputs are serialised before any timer starts.  The default serializer is
  sorted-key JSON (falling back to `repr`); supply `"serializer":
  "module:function"` when a result type needs a stable representation.  Raising
  a different exception message also counts as a mismatch.
- **Paired ratios, not totals.**  Per-pass ratios are paired sample by sample;
  the headline speedup is their median, with p25/p75 and all raw samples kept.
- **Quartile guard.**  `robust` requires the real-input p25 to be ≥ 1.0x as well
  as the median to clear the threshold; a wide distribution is reported as
  `unstable` instead of being rounded up.
- **Rotation.**  Each pass starts at a different offset, so no implementation
  gets a permanent "first input" advantage.
- **Honest warmup.**  Warmup passes are excluded from timing, but they do warm
  caches — including a candidate's memo cache.  That is deliberate: the workload
  pass then shows whether the cache helps when *different* inputs keep arriving.

## Limitations (measured, not guessed)

- Inputs must run in under 2 µs for the timer floor to matter; the report flags
  such inputs and excludes them from per-input regression warnings.
- The process is shared: a candidate that leaks state across inputs is measured
  with that state.  This is the behaviour production sees, but it is not a
  hermetic sandbox.
- A/A false-positive rate is measured for one machine and sample size (2/100
  here); it is not a universal guarantee.  Raise `--reps` on noisy hosts.
- Isolated timing intentionally rewards memoisation.  Do not quote isolated
  numbers as a speedup claim; they are attribution, and `repeat_inflation`
  exists to expose the difference.
- The tool measures a callable pair.  It does not import, patch, or install the
  upstream library; you supply both implementations.

## 本轮创新

Existing benchmark auditors in this account (for example `samplemeter`) check
whether the *inputs* are unique; `speedgap` checks whether the *speedup* is
transferable.  Its new, falsifiable claim is that **interleaved workload timing
and repeated-input timing diverge sharply for memoising candidates** — measured
here as 35.746x repeat inflation while the workload verdict is 0.983x — and that
this divergence can be turned into a gate instead of a footnote.  The 2/100 A/A
result and the 1-of-8 real-input semantic drift (invisible on all 50 synthetic
documents) are the two failure modes the gate is built around.

## Environment

- CPU: AMD EPYC 7K62 48-Core Processor (4 vCPU), 3.7 GiB RAM
- OS: Linux 6.8, CPython 3.12.3
- Seeds: 20261118 (genuine), 1 (one-entry-cache), 7 (drift)
- Clock: `time.perf_counter_ns()`

## License

MIT.  See `LICENSE`.  No upstream code is included or modified.
