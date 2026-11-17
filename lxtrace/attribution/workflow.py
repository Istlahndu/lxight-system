"""Explicit offline production attribution workflow; existing report stays intact."""
import hashlib
import json
import math
import os
from pathlib import Path
from urllib.parse import quote


def parse_count_inputs(values):
    """Preserve supplied order: the first counting run owns duplicate event codes."""
    result = {}
    for value in values:
        if "=" not in value:
            raise ValueError("--count requires RUN=PATH")
        name, path = value.split("=", 1)
        if not name or not path or name in result:
            raise ValueError("counting run labels must be unique and nonempty")
        result[name] = Path(path)
    if not result:
        raise ValueError("at least one counting run is required")
    return result


def read_json(path):
    def invalid(value):
        raise ValueError("non-finite JSON number: " + value)
    def finite(value):
        number = float(value)
        return number if math.isfinite(number) else invalid(value)
    return json.loads(Path(path).read_text(encoding="utf-8-sig"), parse_constant=invalid, parse_float=finite)


def analysis_link(output, analysis_output):
    """Keep moved sidecars navigable, including names with URL punctuation."""
    try:
        relative = os.path.relpath(Path(analysis_output).resolve(), Path(output).resolve().parent)
    except ValueError:  # Different Windows volumes cannot have a relative URL.
        return None
    return quote(Path(relative).as_posix(), safe="/.")


def output_paths(output, analysis_output, inputs, protected=()):
    """Do not let optional sidecars overwrite inputs, profiles or one another."""
    paths = [Path(output), Path(analysis_output)]
    paths.extend(Path(p) for p in protected if p is not None)
    resolved = [p.resolve() for p in paths]
    originals = {Path(p).resolve() for p in inputs if p is not None}
    for path in list(originals):
        if path.suffix == ".bin" and path.with_suffix(".txt").is_file():
            originals.add(path.with_suffix(".txt"))
    if len(set(resolved)) != len(resolved):
        raise ValueError("report, analysis and evidence output paths must be distinct")
    for path in resolved:
        if path in originals or any(path in p.parents or p in path.parents for p in originals):
            raise ValueError("production outputs must be separate from all inputs")
        if path.exists():
            raise ValueError("output already exists; choose a new path: " + str(path))
    for i, path in enumerate(resolved):
        if any(path in p.parents or p in path.parents for p in resolved[i + 1:]):
            raise ValueError("production output paths must not contain one another")
    return paths


def attach_reference(analysis, path):
    """An external comparison is never a source of computed attribution values."""
    raw = Path(path).read_bytes()
    analysis["external_reference"] = {
        "status": "external_reference_not_recomputed",
        "note": "External reference only; its configuration and coverage must be checked separately.",
        "path": str(Path(path)), "sha256": hashlib.sha256(raw).hexdigest(),
        "data": read_json(path),
    }


def bin_nanoseconds(milliseconds):
    if not math.isfinite(milliseconds) or milliseconds <= 0:
        raise ValueError("--bin-ms must be finite and positive")
    scaled = milliseconds * 1_000_000
    if not math.isfinite(scaled):
        raise ValueError("--bin-ms is too large")
    value = int(round(scaled))
    if not value:
        raise ValueError("--bin-ms is smaller than one nanosecond")
    return value


def run_report(args):
    """Generate the same product report for new inputs, with auditable sidecars."""
    from .align import align_runs
    from .model import analyze_attribution, analyze_mfu_counts, load_hardware, load_model
    from .report import build_timeline, write_report

    hardware, model = load_hardware(args.hardware), load_model(args.model)
    count_inputs = parse_count_inputs(args.count) if args.timing else {}
    if not args.timing and (args.count or args.strict_alignment or args.evidence_output):
        raise ValueError("standalone MFU summary mode does not accept --count, --strict-alignment or --evidence-output")
    output = Path(args.output)
    analysis_output = Path(args.analysis_output) if args.analysis_output else output.with_suffix(".analysis.json")
    inputs = ([args.timing] + list(count_inputs.values()) if args.timing else [args.mfu_counts])
    inputs.extend(p for p in (args.hardware, args.model, args.reference) if p and Path(p).is_file())
    output_paths(output, analysis_output, inputs, (args.evidence_output,))
    bin_ns = bin_nanoseconds(args.bin_ms)
    if args.timing:
        aligned = align_runs(args.timing, count_inputs, hardware["facts"]["frequency_hz"],
                             strict_alignment=args.strict_alignment,
                             expected_pool_threads=model.get("expected_pool_threads"))
        analysis = analyze_attribution(aligned, hardware=hardware, model=model)
        analysis["config"] = {"kind": "timing_and_counter_runs", "timing_run": aligned["base"],
                              "counting_runs": aligned["counts"], "workload": aligned["metadata"]["configuration"],
                              "hardware_profile": hardware["profile_id"], "model_profile": model["profile_id"]}
        analysis["coverage"] = {k: aligned[k] for k in
                                ("observed_pool_threads", "expected_pool_threads", "pool_coverage_ratio",
                                 "full_pool_coverage", "threads", "pool_windows")}
        analysis["provenance"] = aligned["provenance"]
        analysis["quality"] = {"warnings": analysis["warnings"], "semantics": "calibrated_aggregate_estimates",
                               "complete_pool_coverage": aligned["full_pool_coverage"]}
    else:
        counts = read_json(args.mfu_counts)
        mfu = analyze_mfu_counts(counts, hardware=hardware)
        raw = Path(args.mfu_counts).read_bytes()
        analysis = {
            "schema_version": "lxtrace.production-analysis.v1", "analysis_rules_version": "mfu-summary-v1",
            "evidence": "model_calibrated:mfu_chain_v1", "representation": "aggregate", "hardware_interval": False,
            "hardware": hardware, "model": model, "config": {"kind": "standalone_mfu_summary",
                "label": counts.get("label"), "source": counts.get("source"), "hardware_profile": hardware["profile_id"]},
            "coverage": {"scope": "declared_complete_summary", "verification": "application_supplied_summary",
                         "observed_pool_threads": counts["frame"]["threads"]},
            "totals": {}, "dies": {}, "stages": {}, "stage_modes": {}, "segments": [], "drivers": [],
            "dense": {"status": "unavailable", "reason": "no_timing_or_dense_counters_supplied"}, "mfu": mfu,
            "alignment": {"absolute_clocks_merged": False},
            "provenance": [{"kind": "complete_mfu_summary", "path": str(Path(args.mfu_counts)),
                            "sha256": hashlib.sha256(raw).hexdigest(), "label": counts.get("label"), "source": counts.get("source")}],
            "warnings": ["MFU derives from the supplied complete-window summary; no timing trace or hardware boundaries were supplied."],
        }
    analysis["timeline"] = build_timeline(analysis, bin_ns=bin_ns)
    analysis["analysis_file"] = analysis_link(output, analysis_output)
    analysis["analysis_location"] = str(analysis_output)
    if args.reference:
        attach_reference(analysis, args.reference)
    # Serialise before creating outputs, so invalid numeric data leaves no report.
    encoded = json.dumps(analysis, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    if args.evidence_output:
        from .evidence import to_resource_session
        from ..resource import write_session
        evidence = to_resource_session(analysis)
    write_report(analysis, output, overwrite=False)
    analysis_output.parent.mkdir(parents=True, exist_ok=True)
    with analysis_output.open("x", encoding="utf-8") as stream:
        stream.write(encoded)
    if args.evidence_output:
        write_session(evidence, args.evidence_output)
    return analysis
