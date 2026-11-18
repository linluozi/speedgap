"""Load a benchmark spec and turn it into callables + :class:`Case` objects."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .core import Case


class SpecError(ValueError):
    """The spec file is missing a field or points at something unimportable."""


def _load_file_module(path: Path):
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
    module_name = "_speedgap_target_" + digest
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise SpecError("cannot load module from %s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def resolve(target: str, base_dir: Path) -> Any:
    """Resolve ``module:attr`` or ``path/to/file.py:attr``.

    ``module`` is imported after adding the spec's directory to ``sys.path``;
    relative file paths are resolved against that directory.
    """
    if not isinstance(target, str) or ":" not in target:
        raise SpecError("expected 'module:function', got %r" % (target,))
    module_ref, attr = target.rsplit(":", 1)
    if not module_ref or not attr:
        raise SpecError("expected 'module:function', got %r" % (target,))
    if module_ref.endswith(".py") or "/" in module_ref:
        path = Path(module_ref)
        if not path.is_absolute():
            path = base_dir / path
        if not path.is_file():
            raise SpecError("file not found: %s" % path)
        module = _load_file_module(path)
    else:
        base = str(base_dir.resolve())
        if base not in sys.path:
            sys.path.insert(0, base)
        try:
            module = importlib.import_module(module_ref)
        except ImportError as exc:
            raise SpecError("cannot import %r: %s" % (module_ref, exc)) from exc
    obj: Any = module
    for part in attr.split("."):
        if not hasattr(obj, part):
            raise SpecError("%r has no attribute %r" % (module_ref, attr))
        obj = getattr(obj, part)
    if not callable(obj):
        raise SpecError("%r is not callable" % target)
    return obj


def _generated(cfg: Dict[str, Any], base_dir: Path, prefix: str) -> List[Case]:
    factory = resolve(cfg["factory"], base_dir)
    count = int(cfg.get("count", 100))
    seed = int(cfg.get("seed", 0))
    if count < 1:
        raise SpecError("count must be >= 1")
    values = factory(seed, count)
    values = list(values)
    if len(values) != count:
        raise SpecError(
            "factory %r returned %d values, spec asked for %d"
            % (cfg["factory"], len(values), count)
        )
    return [Case("%s-%04d" % (prefix, i), value, prefix) for i, value in enumerate(values)]


def _inline(cfg: Dict[str, Any], prefix: str) -> List[Case]:
    values = list(cfg.get("values", []))
    if not values:
        raise SpecError("inline input list is empty")
    return [Case("%s-%04d" % (prefix, i), value, prefix) for i, value in enumerate(values)]


def _files(cfg: Dict[str, Any], base_dir: Path) -> List[Case]:
    extensions = cfg.get("extensions")
    if extensions:
        extensions = tuple(
            ext if str(ext).startswith(".") else "." + str(ext) for ext in extensions
        )
    encoding: Optional[str] = cfg.get("encoding")
    max_bytes = int(cfg.get("max_bytes", 5_000_000))
    limit = cfg.get("limit")
    cases: List[Case] = []
    roots = [Path(p) if Path(p).is_absolute() else base_dir / p for p in cfg.get("paths", [])]
    if not roots:
        raise SpecError("files input needs at least one path")
    for root in roots:
        if root.is_file():
            files = [root]
            names = [root.name]
        elif root.is_dir():
            files = [
                p
                for p in sorted(root.rglob("*"))
                if p.is_file() and (not extensions or p.suffix.lower() in extensions)
            ]
            names = [str(p.relative_to(root)) for p in files]
        else:
            raise SpecError("path not found: %s" % root)
        for path, name in zip(files, names):
            if path.stat().st_size > max_bytes:
                continue
            data = path.read_bytes()
            payload = data.decode(encoding) if encoding else data
            cases.append(Case(name, payload, "real"))
    if limit is not None:
        cases = cases[: int(limit)]
    if not cases:
        raise SpecError("no input files matched %r" % (cfg.get("paths"),))
    return cases


def load_spec(path: Path | str) -> Dict[str, Any]:
    """Read a JSON spec and return resolved callables and cases.

    Recognised fields: ``baseline``, ``candidate``, ``serializer``,
    ``synthetic``, ``real``, plus optional ``warmup`` / ``reps`` / ``repeat`` /
    ``threshold`` overrides.
    """
    import json

    path = Path(path)
    if not path.is_file():
        raise SpecError("spec not found: %s" % path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SpecError("cannot parse spec %s: %s" % (path, exc)) from exc
    if not isinstance(raw, dict):
        raise SpecError("spec must be a JSON object")
    for key in ("baseline", "candidate"):
        if key not in raw:
            raise SpecError("spec is missing %r" % key)
    base_dir = path.resolve().parent

    synthetic: List[Case] = []
    real: List[Case] = []
    if "synthetic" in raw:
        cfg = raw["synthetic"]
        kind = cfg.get("kind", "generated")
        if kind == "generated":
            synthetic = _generated(cfg, base_dir, "synthetic")
        elif kind == "inline":
            synthetic = _inline(cfg, "synthetic")
        else:
            raise SpecError("unknown synthetic kind %r" % kind)
    if "real" in raw:
        cfg = raw["real"]
        kind = cfg.get("kind", "files")
        if kind == "files":
            real = _files(cfg, base_dir)
        elif kind == "inline":
            real = _inline(cfg, "real")
        else:
            raise SpecError("unknown real kind %r" % kind)
    if not synthetic and not real:
        raise SpecError("spec has neither synthetic nor real inputs")

    serializer: Optional[Callable[[Any], Any]] = None
    if raw.get("serializer"):
        serializer = resolve(raw["serializer"], base_dir)
    result: Dict[str, Any] = {
        "baseline": resolve(raw["baseline"], base_dir),
        "candidate": resolve(raw["candidate"], base_dir),
        "synthetic": synthetic,
        "real": real,
        "serializer": serializer,
        "spec_path": str(path),
    }
    for key in ("warmup", "reps", "repeat"):
        if key in raw:
            result[key] = int(raw[key])
    if "threshold" in raw:
        result["threshold"] = float(raw["threshold"])
    return result
