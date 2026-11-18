import functools
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from speedgap import (  # noqa: E402
    Case,
    classify,
    compare,
    default_serializer,
    environment_info,
    measure_isolated,
    measure_workload,
    observe,
    quantile,
    summarize,
)


class SeqTimer:
    """Timer returning a fixed sequence, for deterministic duration checks."""

    def __init__(self, values):
        self.values = iter(values)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return next(self.values)


class ScheduleTimer:
    """Deterministic paired durations, one entry per timed sample."""

    def __init__(self, durations):
        self.durations = iter(durations)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return 0 if self.calls % 2 else next(self.durations)


class ConstTimer:
    """Every start/end pair spans exactly 1000 ns, for a deterministic A/A run."""

    def __init__(self, duration=1000):
        self.duration = duration
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return 0 if self.calls % 2 else self.duration


class QuantileTests(unittest.TestCase):
    def test_endpoints_and_median(self):
        xs = [1, 2, 3, 4]
        self.assertEqual(quantile(xs, 0.0), 1)
        self.assertEqual(quantile(xs, 1.0), 4)
        self.assertEqual(quantile(xs, 0.5), 2.5)

    def test_interpolation(self):
        self.assertAlmostEqual(quantile([0, 10], 0.25), 2.5)
        self.assertAlmostEqual(quantile([0, 10, 20], 0.75), 15.0)

    def test_rejects_bad_q(self):
        with self.assertRaises(ValueError):
            quantile([1], 1.1)
        with self.assertRaises(ValueError):
            quantile([], 0.5)

    def test_summarize(self):
        stats = summarize([1, 2, 3, 4, 5])
        self.assertEqual(stats["n"], 5)
        self.assertEqual(stats["median"], 3)
        self.assertEqual(stats["p25"], 2)
        self.assertEqual(stats["p75"], 4)
        self.assertEqual(stats["min"], 1)
        self.assertEqual(stats["max"], 5)
        self.assertGreater(stats["rel_stdev"], 0)


class SerializerTests(unittest.TestCase):
    def test_dict_order_is_ignored(self):
        self.assertEqual(
            default_serializer({"a": 1, "b": [2, 3]}),
            default_serializer({"b": [2, 3], "a": 1}),
        )

    def test_type_is_preserved(self):
        self.assertNotEqual(default_serializer(1), default_serializer(1.0))
        self.assertNotEqual(default_serializer(1), default_serializer("1"))

    def test_unicode_is_utf8(self):
        blob = default_serializer("中文\u00a0🧪")
        self.assertEqual(blob.decode("utf-8"), '"中文\xa0🧪"')

    def test_unknown_object_falls_back_to_repr(self):
        class Point:
            def __repr__(self):
                return "Point(1, 2)"

        self.assertEqual(default_serializer(Point()), b'"Point(1, 2)"')


class ObserveTests(unittest.TestCase):
    def test_value_observation(self):
        result = observe(lambda x: x * 2, 21)
        self.assertEqual(result["kind"], "value")
        self.assertEqual(len(result["sha256"]), 64)
        self.assertEqual(result["size"], len(b"42"))

    def test_same_value_same_bytes(self):
        first = observe(lambda x: {"b": x, "a": 1}, 2)
        second = observe(lambda x: {"a": 1, "b": x}, 2)
        self.assertEqual(first["data"], second["data"])

    def test_exception_is_part_of_the_contract(self):
        def boom(_):
            raise ValueError("nope")

        result = observe(boom, None)
        self.assertEqual(result["kind"], "raise")
        self.assertEqual(result["type"], "ValueError")
        self.assertEqual(result["message"], "nope")
        self.assertIn(b"nope", result["data"])

    def test_different_exception_messages_differ(self):
        first = observe(lambda _: (_ for _ in ()).throw(KeyError("a")), None)
        second = observe(lambda _: (_ for _ in ()).throw(KeyError("b")), None)
        self.assertNotEqual(first["data"], second["data"])

    def test_serializer_str_and_bytes(self):
        self.assertEqual(observe(lambda x: x, 5, lambda v: str(v))["size"], 1)
        self.assertEqual(observe(lambda x: x, 5, lambda v: b"raw")["size"], 3)

    def test_invalid_serializer(self):
        with self.assertRaises(TypeError):
            observe(lambda x: x, 5, lambda v: 42)


class WorkloadMeasurementTests(unittest.TestCase):
    def test_samples_are_divided_by_calls(self):
        cases = [Case("a", 1), Case("b", 2), Case("c", 3)]
        timer = SeqTimer([0, 300, 0, 600])
        measured = measure_workload(
            lambda x: x, cases, warmup=0, reps=2, repeat=1, timer=timer
        )
        self.assertEqual(measured["samples_ns"], [100.0, 200.0])
        self.assertEqual(measured["calls"], 3)
        self.assertEqual(measured["median_ns"], 150.0)

    def test_repeat_multiplies_calls(self):
        cases = [Case("a", 1), Case("b", 2)]
        timer = SeqTimer([0, 800])
        measured = measure_workload(
            lambda x: x, cases, warmup=0, reps=1, repeat=4, timer=timer
        )
        self.assertEqual(measured["calls"], 8)
        self.assertEqual(measured["samples_ns"], [100.0])

    def test_pass_order_rotates(self):
        seen = []

        def record(payload):
            seen.append(payload)

        measure_workload(
            record,
            [Case("a", 1), Case("b", 2), Case("c", 3)],
            warmup=0,
            reps=3,
            repeat=1,
            timer=SeqTimer([0, 1, 0, 1, 0, 1]),
        )
        self.assertEqual(seen[:3], [1, 2, 3])
        self.assertEqual(seen[3:6], [2, 3, 1])
        self.assertEqual(seen[6:9], [3, 1, 2])

    def test_zero_duration_is_rejected(self):
        cases = [Case("a", 1)]
        with self.assertRaises(ValueError):
            measure_workload(
                lambda x: x, cases, warmup=0, reps=1, timer=SeqTimer([5, 5])
            )

    def test_bad_reps(self):
        with self.assertRaises(ValueError):
            measure_workload(lambda x: x, [Case("a", 1)], reps=0)


class IsolatedMeasurementTests(unittest.TestCase):
    def test_one_row_per_input_keeps_samples(self):
        timer = SeqTimer([0, 10, 0, 30, 0, 20, 0, 40])
        measured = measure_isolated(
            lambda x: x, [Case("a", 1), Case("b", 2)], warmup=0, reps=2, timer=timer
        )
        rows = measured["rows"]
        self.assertEqual([row["median_ns"] for row in rows], [20.0, 30.0])
        self.assertEqual(rows[0]["samples_ns"], [10.0, 30.0])
        self.assertEqual(len(rows), 2)


class ClassifyTests(unittest.TestCase):
    def test_five_verdicts(self):
        self.assertEqual(classify(2.0, 1.5, 0), "robust")
        self.assertEqual(classify(2.0, 1.0, 0), "synthetic-only")
        self.assertEqual(classify(1.05, 1.0, 0), "no-gain")
        self.assertEqual(classify(1.0, 0.8, 0), "regression")
        self.assertEqual(classify(2.0, 1.5, 1), "semantic-mismatch")

    def test_unstable_when_median_passes_but_p25_does_not(self):
        self.assertEqual(classify(1.0, 1.30, 0, real_p25=0.94), "unstable")
        self.assertEqual(classify(1.0, 1.30, 0, real_p25=1.01), "robust")

    def test_custom_threshold(self):
        self.assertEqual(classify(3.0, 2.0, 0, threshold=2.5), "synthetic-only")
        self.assertEqual(classify(3.0, 2.0, 0, threshold=1.9), "robust")


class CompareTests(unittest.TestCase):
    def test_robust_quadratic_to_constant(self):
        synth = [Case("s-%d" % i, 400 + i, "synthetic") for i in range(10)]
        real = [Case("r-%d" % i, 420 + i, "real") for i in range(6)]
        report = compare(
            lambda n: sum(range(n)),
            lambda n: n * (n - 1) // 2,
            synth,
            real,
            warmup=1,
            reps=3,
        )
        self.assertEqual(report["verdict"], "robust")
        self.assertEqual(report["exit_code"], 0)
        self.assertGreater(report["real"]["median"], 1.15)
        self.assertIsNotNone(report["synthetic"])
        self.assertIsNotNone(report["isolated"])
        self.assertEqual(len(report["isolated"]["real"]["rows"]), 6)
        self.assertIn("python", report["environment"])

    def test_semantic_mismatch_short_circuits_timing(self):
        report = compare(
            lambda x: x,
            lambda x: str(x),
            [Case("s", 1, "synthetic")],
            [Case("r", 2, "real")],
            warmup=0,
            reps=2,
        )
        self.assertEqual(report["verdict"], "semantic-mismatch")
        self.assertEqual(report["exit_code"], 2)
        self.assertIsNone(report["synthetic"])
        self.assertIsNone(report["real"])
        self.assertEqual(report["mismatch_count"], 2)
        self.assertEqual(report["mismatches"][0]["name"], "s")

    def test_identical_functions_are_no_gain(self):
        def work(n):
            return sum(i * i for i in range(n))

        cases = [Case("a", 120), Case("b", 130), Case("c", 140)]
        report = compare(
            work, work, cases[:2], cases[2:], warmup=1, reps=4, timer=ConstTimer()
        )
        self.assertEqual(report["verdict"], "no-gain")
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(report["real"]["median"], 1.0)
        self.assertEqual(report["synthetic"]["median"], 1.0)

    def test_regression(self):
        cases = [Case("a", 100), Case("b", 110)]
        report = compare(
            lambda n: n * (n - 1) // 2,
            lambda n: sum(range(n)),
            cases,
            cases,
            warmup=0,
            reps=3,
        )
        self.assertEqual(report["verdict"], "regression")
        self.assertEqual(report["exit_code"], 1)
        self.assertLess(report["real"]["median"], 0.95)

    def test_one_entry_cache_repeat_inflation_is_caught(self):
        def work(n):
            return sum(i * i for i in range(n))

        synth = [Case("same-%02d" % i, 2000, "synthetic") for i in range(40)]
        real = [Case("real-%02d" % i, 2001 + i, "real") for i in range(12)]
        reps = 6
        n_synth, n_real = len(synth), len(real)
        # baseline: 1000 ns/call; candidate: 10 ns on repeated synthetic
        # inputs, 1000 ns on distinct real inputs (a maxsize=1 cache misses).
        plan = [n_synth * 1000] * reps + [n_synth * 10] * reps
        plan += [n_real * 1000] * reps + [n_real * 1000] * reps
        plan += [1000] * ((n_synth + n_real) * reps)
        plan += [10] * ((n_synth + n_real) * reps)
        report = compare(
            work,
            functools.lru_cache(maxsize=1)(work),
            synth,
            real,
            warmup=0,
            reps=reps,
            timer=ScheduleTimer(plan),
        )
        self.assertEqual(report["verdict"], "synthetic-only")
        self.assertEqual(report["synthetic"]["median"], 100.0)
        self.assertEqual(report["real"]["median"], 1.0)
        self.assertEqual(report["repeat_inflation"], 100.0)
        self.assertTrue(any("repeat inflation" in w for w in report["warnings"]))

    def test_unstable_verdict_and_exit_code(self):
        def work(n):
            return n

        synth = [Case("s-%d" % i, i, "synthetic") for i in range(3)]
        real = [Case("r-%d" % i, i, "real") for i in range(4)]
        reps = 6
        plan = [4000] * reps + [4000] * reps + [4000] * reps
        plan += [3200, 3200, 3200, 4800, 4800, 3200]
        plan += [1000] * (7 * reps) + [1000] * (7 * reps)
        report = compare(
            work, work, synth, real, warmup=0, reps=reps, timer=ScheduleTimer(plan)
        )
        self.assertEqual(report["verdict"], "unstable")
        self.assertEqual(report["exit_code"], 1)
        self.assertGreaterEqual(report["real"]["median"], 1.15)
        self.assertLess(report["real"]["p25"], 1.0)

    def test_no_inputs_is_an_error(self):
        with self.assertRaises(ValueError):
            compare(lambda x: x, lambda x: x, [], [])

    def test_threshold_must_be_positive(self):
        with self.assertRaises(ValueError):
            compare(
                lambda x: x,
                lambda x: x,
                [Case("a", 1)],
                [],
                threshold=0,
            )

    def test_gap_is_reported_when_real_is_slower(self):
        real = [Case("r", 500)]
        report = compare(
            lambda n: n,
            lambda n: n,
            [Case("s", 1)],
            real,
            warmup=0,
            reps=3,
        )
        self.assertIsNotNone(report["gap"])
        self.assertGreaterEqual(report["gap"], 0.0)

    def test_environment_has_runtime(self):
        info = environment_info()
        self.assertIn("python", info)
        self.assertIn("machine", info)
        self.assertEqual(info["implementation"], "CPython")


if __name__ == "__main__":
    unittest.main()
