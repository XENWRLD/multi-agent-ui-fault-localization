# =========================================================
# log_parser.py — Unified log.json ingestion
#
# Handles 5 distinct log shapes encountered across apps and returns a
# single ParsedLogs struct that the graph consumes uniformly.
#
#   Pre-assigned modes (step ownership known at parse time):
#     - flat_indexed     — array of flat entries with `step: <int>`
#     - flat_assertions  — array of flat entries; boundaries marked by
#                          StepRunner messages ("Step N passed/failed: ...")
#     - nested           — array of {step: {...}, status, logs?} where
#                          array index IS the step_idx and `logs` is a
#                          pipe-delimited string (or missing entirely)
#
#   Dynamic mode (baseline behavior preserved):
#     - raw              — flat, unstepped, heterogeneous entries; step
#                          assignment is performed at query time by
#                          models.extract_step_logs (keyword + positional)
# =========================================================

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Literal


@dataclass
class ParsedLogs:
    """Result of ingesting a log.json file.

    mode == "preassigned" → step_map is authoritative; the graph looks up
                            entries via step_map.get(step_idx, []).
    mode == "dynamic"     → raw_entries holds the full list; the graph
                            calls extract_step_logs() at query time.
    mode == "empty"       → no log file / unparseable / empty array.
    """
    mode: Literal["preassigned", "dynamic", "empty"]
    format_name: str
    step_map: Dict[int, List[dict]] = field(default_factory=dict)
    raw_entries: List[dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def parse_log_file(log_path: str, total_steps: int) -> ParsedLogs:
    """Load and classify a log.json file. `total_steps` enables 1-indexed shift detection."""
    if not log_path or not os.path.exists(log_path):
        return ParsedLogs(mode="empty", format_name="none")

    try:
        with open(log_path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return ParsedLogs(mode="empty", format_name="error",
                          warnings=[f"failed to load {log_path}: {e}"])

    if not isinstance(raw, list) or not raw:
        return ParsedLogs(mode="empty", format_name="none")

    fmt = _detect_format(raw)

    if fmt == "nested":
        return _parse_step_nested(raw)
    if fmt == "flat_indexed":
        return _parse_flat_indexed(raw, total_steps)
    if fmt == "flat_assertions":
        return _parse_flat_assertions(raw, total_steps)
    # fmt == "raw" — keep flat, let the graph do dynamic assignment
    return ParsedLogs(mode="dynamic", format_name="raw", raw_entries=raw)


# =========================================================
# FORMAT DETECTION
# =========================================================

_ASSERTION_RE = re.compile(r"\bStep\s+\d+\s+(passed|failed)", re.IGNORECASE)


def _detect_format(raw: list) -> str:
    first = raw[0]
    if not isinstance(first, dict):
        return "raw"

    step_val = first.get("step")
    if isinstance(step_val, dict):
        return "nested"
    if isinstance(step_val, int):
        return "flat_indexed"

    # Scan for StepRunner assertion markers (first ~20 entries is enough)
    for entry in raw[:20]:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("source", "")).lower() != "steprunner":
            continue
        if _ASSERTION_RE.search(str(entry.get("message", ""))):
            return "flat_assertions"

    return "raw"


# =========================================================
# FORMAT A2 — flat with explicit step:int
# =========================================================

def _parse_flat_indexed(raw: list, total_steps: int) -> ParsedLogs:
    step_map: Dict[int, List[dict]] = {}
    seen: set = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        s = entry.get("step")
        if not isinstance(s, int):
            continue
        seen.add(s)
        step_map.setdefault(s, []).append(entry)

    warnings: List[str] = []
    if seen and total_steps > 0:
        min_v, max_v = min(seen), max(seen)
        # Shift condition: minimum observed step is 1 and no step 0 exists.
        # This is the true semantic signal for a 1-indexed log. We deliberately
        # do NOT constrain max_v == total_steps: trailing skipped/unlogged steps
        # (e.g. a final `wait` action in QNB) make max_v < total_steps even
        # when the log is genuinely 1-indexed, causing the old check to miss.
        if min_v == 1 and 0 not in seen:
            step_map = {k - 1: v for k, v in step_map.items()}
            warnings.append(
                f"log appeared 1-indexed (steps {min_v}..{max_v}); "
                f"shifted to 0..{max_v - 1} to align with steps.json"
            )
        # Warn on out-of-range residue after any shift (e.g. clock logs step
        # 7/8 but steps.json only goes to 0..6).
        out_of_range = [s for s in step_map if s < 0 or s >= total_steps]
        if out_of_range:
            warnings.append(
                f"{len(out_of_range)} log step value(s) outside steps.json range "
                f"[0, {total_steps - 1}]: {sorted(out_of_range)}"
            )

    return ParsedLogs(
        mode="preassigned",
        format_name="flat_indexed",
        step_map=step_map,
        warnings=warnings,
    )


# =========================================================
# FORMAT B — flat with StepRunner "Step N passed/failed" markers
# =========================================================

def _parse_flat_assertions(raw: list, total_steps: int = 0) -> ParsedLogs:
    """Partition entries using assertion markers as right-boundaries.

    Everything accumulated up to and including a `Step N passed/failed` row
    from `source: StepRunner` is assigned to step N. Entries after the
    last marker (scenario summaries, etc.) are orphaned with a warning.
    """
    step_map: Dict[int, List[dict]] = {}
    bucket: List[dict] = []

    for entry in raw:
        bucket.append(entry)
        if not isinstance(entry, dict):
            continue
        if str(entry.get("source", "")).lower() != "steprunner":
            continue
        m = _ASSERTION_RE.search(str(entry.get("message", "")))
        if not m:
            continue
        step_num = _extract_step_number(entry.get("message", ""))
        if step_num is None:
            continue
        step_map.setdefault(step_num, []).extend(bucket)
        bucket = []

    warnings: List[str] = []
    if bucket:
        warnings.append(
            f"{len(bucket)} trailing entries after final StepRunner marker "
            f"were not assigned (likely scenario summary)"
        )
    # Detect data-quality mismatch: more assertion markers than steps.json steps
    # suggests the log was generated from a different test run (e.g. bitaksi).
    marker_count = len(step_map)
    if total_steps > 0 and marker_count > total_steps:
        warnings.append(
            f"log contains {marker_count} StepRunner markers but steps.json has "
            f"{total_steps} steps — log may be from a different test run; "
            f"step-to-log mapping may be semantically misaligned"
        )

    return ParsedLogs(
        mode="preassigned",
        format_name="flat_assertions",
        step_map=step_map,
        warnings=warnings,
    )


_STEP_NUM_RE = re.compile(r"\bStep\s+(\d+)\b", re.IGNORECASE)


def _extract_step_number(msg: str) -> int | None:
    m = _STEP_NUM_RE.search(msg or "")
    return int(m.group(1)) if m else None


# =========================================================
# FORMAT C — step-nested; array index is step_idx; `logs` is pipe-delimited
# =========================================================

_PIPE_SPLIT_RE = re.compile(r"\s*\|\s*")


def _parse_step_nested(raw: list) -> ParsedLogs:
    step_map: Dict[int, List[dict]] = {}
    missing = 0
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            step_map[idx] = []
            continue
        logs_val = item.get("logs")
        if not logs_val:
            step_map[idx] = []
            missing += 1
            continue
        step_map[idx] = _parse_pipe_delimited(logs_val)

    warnings: List[str] = []
    if missing:
        warnings.append(f"{missing}/{len(raw)} steps had no 'logs' field")
    return ParsedLogs(
        mode="preassigned",
        format_name="nested",
        step_map=step_map,
        warnings=warnings,
    )


def _parse_pipe_delimited(text: str) -> List[dict]:
    """Parse `k: v | k: v | ...` rows; multiple rows are joined by `\\n`.

    Preserves the original keys so downstream keyword-scoring / log-observer
    behavior is identical to the flat formats (timestamp, level, source,
    message, event_type, screen, thread, trace_id, ...).
    """
    entries: List[dict] = []
    for row in (text or "").splitlines():
        row = row.strip()
        if not row:
            continue
        d: Dict[str, str] = {}
        for field_str in _PIPE_SPLIT_RE.split(row):
            if ":" not in field_str:
                continue
            k, _, v = field_str.partition(":")
            k, v = k.strip(), v.strip()
            if k:
                d[k] = v
        if d:
            entries.append(d)
    return entries
