"""Spec loading and end-to-end CLI tests."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from speedgap.spec import SpecError, load_spec, resolve  # noqa: E402

IMPL = '''
import functools

def slow(text):
    total = 0
    for ch in text:
        if ch == "x":
            total += 1
    return total

def fast(text):
    return text.count("x")

def passthrough(text):
    return text

def normalize(text):
    return text.replace("\\u00a0", " ")

def _heavy(text):
    return sum(ord(ch) for ch in text) + len(text.split())

@functools.lru_cache(maxsize=1)
def cached_stats(text):
    return _heavy(text)

def plain_stats(text):
    return _heavy(text)

def make_docs(seed, count):
    return ["x" * (10 + (seed + i) % 7) + " alpha beta" for i in range(count)]

def make_repeated(seed, count):
    return ["x" * 400 + " alpha beta"] * count

def fixed_docs(seed, count):
    return ["one", "two", "three"]
'''


def run_cli(*args, cwd=ROOT):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "speedgap", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


class SpecTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "impl.py").write_text(IMPL, encoding="utf-8")
        (self.dir / "corpus").mkdir()
        (self.dir / "corpus" / "b.txt").write_text("box x", encoding="utf-8")
        (self.dir / "corpus" / "a.txt").write_text("alpha", encoding="utf-8")
        (self.dir / "corpus" / "skip.md").write_text("x" * 50, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def write_spec(self, payload, name="spec.json"):
        path = self.dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def base_spec(self, factory="impl:make_docs"):
        return {
            "baseline": "impl:slow",
            "candidate": "impl:fast",
            "synthetic": {
                "kind": "generated",
                "factory": factory,
                "count": 20,
                "seed": 3,
            },
            "real": {
                "kind": "files",
                "paths": ["corpus"],
                "encoding": "utf-8",
                "extensions": [".txt"],
            },
        }

    def test_resolve_module_and_file(self):
        self.assertTrue(callable(resolve("impl:fast", self.dir)))
        self.assertTrue(callable(resolve("impl.py:slow", self.dir)))

    def test_resolve_missing_attribute(self):
        with self.assertRaises(SpecError):
            resolve("impl:nope", self.dir)

    def test_resolve_bad_syntax(self):
        with self.assertRaises(SpecError):
            resolve("impl", self.dir)

    def test_load_generated_and_files(self):
        spec = load_spec(self.write_spec(self.base_spec()))
        self.assertEqual(len(spec["synthetic"]), 20)
        self.assertEqual([c.name for c in spec["real"]], ["a.txt", "b.txt"])
        self.assertEqual(spec["real"][0].payload, "alpha")
        self.assertEqual(spec["synthetic"][0].group, "synthetic")

    def test_file_order_is_sorted_and_binary_default(self):
        payload = self.base_spec()
        del payload["real"]["encoding"]
        spec = load_spec(self.write_spec(payload))
        self.assertEqual(spec["real"][0].payload, b"alpha")

    def test_factory_count_mismatch_is_rejected(self):
        payload = self.base_spec()
        payload["synthetic"]["factory"] = "impl:fixed_docs"
        payload["synthetic"]["count"] = 21
        with self.assertRaises(SpecError):
            load_spec(self.write_spec(payload))

    def test_inline_inputs(self):
        payload = {
            "baseline": "impl:slow",
            "candidate": "impl:fast",
            "synthetic": {"kind": "inline", "values": ["x", "xx"]},
            "real": {"kind": "inline", "values": ["box"]},
        }
        spec = load_spec(self.write_spec(payload))
        self.assertEqual([c.payload for c in spec["synthetic"]], ["x", "xx"])
        self.assertEqual(spec["real"][0].group, "real")

    def test_missing_fields(self):
        payload = {"baseline": "impl:slow"}
        with self.assertRaises(SpecError):
            load_spec(self.write_spec(payload))

    def test_invalid_json(self):
        path = self.dir / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(SpecError):
            load_spec(path)

    def test_empty_file_selection(self):
        payload = self.base_spec()
        payload["real"]["extensions"] = [".nope"]
        with self.assertRaises(SpecError):
            load_spec(self.write_spec(payload))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "impl.py").write_text(IMPL, encoding="utf-8")
        (self.dir / "corpus").mkdir()
        for i in range(12):
            name = chr(ord("a") + i) + ".txt"
            body = ("x box x alpha beta gamma delta %s " % name) * 60
            (self.dir / "corpus" / name).write_text(body.strip(), encoding="utf-8")
        (self.dir / "corpus" / "b.txt").write_text(
            ("alpha\u00a0beta gamma delta " * 60).strip(), encoding="utf-8"
        )
        self.report = self.dir / "report.json"

    def tearDown(self):
        self.tmp.cleanup()

    def spec(self, baseline, candidate, real_encoding="utf-8", count=20,
             factory="impl:make_docs"):
        payload = {
            "baseline": baseline,
            "candidate": candidate,
            "synthetic": {
                "kind": "generated",
                "factory": factory,
                "count": count,
                "seed": 11,
            },
            "real": {
                "kind": "files",
                "paths": ["corpus"],
                "encoding": real_encoding,
            },
            "reps": 3,
            "warmup": 1,
        }
        path = self.dir / "spec.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_version(self):
        result = run_cli("--version")
        self.assertEqual(result.returncode, 0)
        self.assertIn("speedgap 1.0.0", result.stdout)

    def test_genuine_run_exits_zero_and_writes_report(self):
        spec = self.spec("impl:slow", "impl:fast")
        result = run_cli("run", "--spec", str(spec), "--json", str(self.report))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verdict    robust", result.stdout)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(report["verdict"], "robust")
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["real"]["inputs"], 12)
        self.assertEqual(report["synthetic"]["inputs"], 20)
        self.assertIn("environment", report)

    def test_drift_exits_two(self):
        spec = self.spec("impl:passthrough", "impl:normalize")
        result = run_cli("run", "--spec", str(spec), "--json", str(self.report))
        self.assertEqual(result.returncode, 2)
        self.assertIn("semantic-mismatch", result.stdout)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(report["mismatch_count"], 1)
        self.assertEqual(report["mismatches"][0]["name"], "b.txt")

    def test_memoised_candidate_exits_one_with_warning(self):
        spec = self.spec(
            "impl:plain_stats", "impl:cached_stats", factory="impl:make_repeated"
        )
        result = run_cli("run", "--spec", str(spec), "--json", str(self.report))
        self.assertEqual(result.returncode, 1)
        self.assertIn("synthetic-only", result.stdout)
        self.assertIn("repeat inflation", result.stdout)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertLess(report["real"]["median"], 1.15)
        self.assertGreater(report["repeat_inflation"], 1.5)

    def test_quiet_suppresses_summary(self):
        spec = self.spec("impl:slow", "impl:fast")
        result = run_cli("run", "--spec", str(spec), "--quiet")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_missing_spec_is_config_error(self):
        result = run_cli("run", "--spec", str(self.dir / "nope.json"))
        self.assertEqual(result.returncode, 3)
        self.assertIn("speedgap:", result.stderr)

    def test_bad_threshold_is_config_error(self):
        spec = self.spec("impl:slow", "impl:fast")
        result = run_cli("run", "--spec", str(spec), "--threshold", "0")
        self.assertEqual(result.returncode, 3)


if __name__ == "__main__":
    unittest.main()
