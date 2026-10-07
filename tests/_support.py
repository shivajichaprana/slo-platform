"""Shared fixtures and import plumbing for the test suite.

This repository ships flat module directories rather than an installable
package -- every tool is run from a checkout, against the specifications in the
same checkout -- so the suite extends `sys.path` the same way the modules
themselves do, instead of depending on packaging metadata that does not exist.

The builders below return a document that is valid against the schema and
silent under `--strict`. A test states its subject by mutating one field of it,
which keeps the difference between a passing and a failing document visible in
the test rather than buried in a fixture file.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

for _directory in ("sources", "generator", "policy", "tools"):
    _path = str(ROOT / _directory)
    if _path not in sys.path:
        sys.path.insert(0, _path)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_script(relative: str, module_name: str) -> ModuleType:
    """Import a module whose filename is not a legal Python identifier.

    `tools/validate-specs.py` is spelled with a hyphen because it is run as a
    command far more often than it is imported. That is the right name for the
    file and the wrong name for a module, so it is loaded by path.
    """
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative)
    if spec is None or spec.loader is None:  # pragma: no cover - import plumbing
        raise ImportError(f"cannot load {relative}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def schema() -> dict[str, Any]:
    return json.loads((ROOT / "schema" / "slo.schema.json").read_text(encoding="utf-8"))


#: One tier that fires on a realistic budget, used wherever the tier itself is
#: not the subject of the test.
FAST_TIER: dict[str, Any] = {
    "name": "fast",
    "burn_rate": 14.4,
    "long_window": "1h",
    "short_window": "5m",
    "notify": "page",
}

SLOW_TIER: dict[str, Any] = {
    "name": "slow",
    "burn_rate": 1,
    "long_window": "3d",
    "short_window": "6h",
    "notify": "ticket",
}


def ratio_objective(**overrides: Any) -> dict[str, Any]:
    """A ratio objective that is valid, reachable and warning-free."""
    objective: dict[str, Any] = {
        "name": "availability",
        "title": "Requests are served successfully",
        "sli": {
            "kind": "ratio",
            "good_query": 'sum(rate(http_requests_total{code!~"5.."}[$window]))',
            "valid_query": "sum(rate(http_requests_total[$window]))",
            "blind_spots": [
                "A failure in front of the service removes traffic from the denominator.",
            ],
            "sampling": {"expected_events_per_hour": 120000},
        },
        "objective": 0.995,
        "window": {"kind": "rolling", "duration": "28d"},
        "alerting": {
            "policy": "multiwindow-burn-rate",
            "tiers": [copy.deepcopy(FAST_TIER), copy.deepcopy(SLOW_TIER)],
        },
    }
    objective.update(copy.deepcopy(overrides))
    return objective


def threshold_objective(**overrides: Any) -> dict[str, Any]:
    """A latency objective stated as a proportion, not as a percentile."""
    objective: dict[str, Any] = {
        "name": "latency",
        "title": "Requests complete within 300 milliseconds",
        "sli": {
            "kind": "threshold",
            "valid_query": "sum(rate(http_request_duration_seconds_count[$window]))",
            "metric": "http_request_duration_seconds",
            "threshold": 300,
            "unit": "milliseconds",
            "comparison": "less_than_or_equal",
            "blind_spots": ["Measured at the server, so the client's own latency is invisible."],
            "sampling": {"expected_events_per_hour": 120000},
        },
        "objective": 0.99,
        "window": {"kind": "rolling", "duration": "28d"},
        "alerting": {
            "policy": "multiwindow-burn-rate",
            "tiers": [copy.deepcopy(FAST_TIER)],
        },
    }
    objective.update(copy.deepcopy(overrides))
    return objective


def document(*objectives: dict[str, Any], **metadata: Any) -> dict[str, Any]:
    """A whole specification document around one or more objectives."""
    meta = {"service": "checkout", "owner": "payments-platform", "tier": "critical"}
    meta.update(metadata)
    return {
        "apiVersion": "slo.platform/v1",
        "kind": "ServiceLevelObjectives",
        "metadata": meta,
        "objectives": [copy.deepcopy(o) for o in (objectives or (ratio_objective(),))],
    }


def write_document(directory: Path, doc: Any, name: str = "spec.yaml") -> Path:
    """Serialise a document into a directory, for the tools that read files."""
    import yaml

    path = directory / name
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def codes(findings: Any) -> list[str]:
    return [f.code for f in findings]
