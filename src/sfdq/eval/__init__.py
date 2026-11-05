"""Eval harness: score the agent's proposals against labeled expected fixes."""

from sfdq.eval.labels import LabelSet, build_labels, load_labels
from sfdq.eval.metrics import Case, Metrics, compute_metrics, judge
from sfdq.eval.report import render_json, render_markdown
from sfdq.eval.runner import EvalError, EvalRun, run_eval

__all__ = [
    "Case",
    "EvalError",
    "EvalRun",
    "LabelSet",
    "Metrics",
    "build_labels",
    "compute_metrics",
    "judge",
    "load_labels",
    "render_json",
    "render_markdown",
    "run_eval",
]
