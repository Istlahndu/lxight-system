#!/usr/bin/env python3
"""Run a real two-layer CPU MLP in serial and microbatch pipeline modes."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from lxtrace.workspace import workspace_environment

ROOT = Path(__file__).resolve().parent
SERVER_GCC = "/work_ssd/software/HPCKit/25.1.0.SPC001/compiler/gcc/bin/g++"
SERVER_LIB = "/work_ssd/software/HPCKit/25.1.0.SPC001/compiler/gcc/lib64"


def summarize_categories(data):
    """Recorded software evidence, explicitly distinct from hardware attribution."""
    from lxtrace.core import length, union
    specifications = [
        ("mat", "算·矩阵", lambda e: e["kind"] == "range" and e["category"] == "mat"),
        ("vec", "算·向量/标量", lambda e: e["kind"] == "range" and e["category"] == "vec"),
        ("memory_exposed", "访存（暴露）", None),
        ("cc", "通信·计算内跨核", lambda e: e["kind"] == "range" and e["category"] == "cc"),
        ("cx", "通信·显式交换", lambda e: e["kind"] == "exchange"),
        ("frontend_branch", "其他·前端/分支", None),
        ("wait", "等待", lambda e: e["kind"] == "wait"),
        ("drv", "驱动线程调用外", lambda e: e["kind"] == "driver_outside"),
    ]
    result = []
    for key, name, predicate in specifications:
        events = [e for e in data["events"] if predicate and predicate(e)]
        closed = [e for e in events if e.get("end_ts") is not None and e.get("status") == "complete"]
        result.append(dict(category=key, name=name,
            status="unavailable" if predicate is None else "recorded" if events else "not_recorded",
            recorded_events=len(events), closed_events=len(closed),
            annotated_wall_union_ns=None if predicate is None else length(union(
                [(e["begin_ts"], e["end_ts"]) for e in closed])),
            scope="hardware attribution unavailable" if predicate is None else
                  "software frame complement" if key == "drv" else "software wall annotation"))
    return result


def summarize_run(data, business):
    from lxtrace.core import analyze, intersection, length, union
    overall = analyze(data, 0, max(1, data["time_range"][1]))
    layers = {}
    for layer in (1, 2):
        computations = [e for e in data["events"] if e["kind"] == "range" and
                        e["category"] in ("mat", "vec") and e.get("args", {}).get("layer") == layer
                        and e.get("end_ts") is not None]
        intervals = union([(e["begin_ts"], e["end_ts"]) for e in computations])
        layers[layer] = intervals
    layer_overlap = intersection(layers[1], layers[2])
    frames = []
    for frame in data["events"]:
        if frame.get("kind") != "frame" or frame.get("args", {}).get("frame_kind") != "layer":
            continue
        frame_id = frame["event_id"]
        members = [e for e in data["events"] if e.get("frame_id") == frame_id and
                   e.get("end_ts") is not None and e.get("status") == "complete"]
        by_category = {}
        for category in ("mat", "vec", "cc", "cx", "wait", "drv"):
            selected = [e for e in members if e.get("kind") != "request" and e.get("category") == category]
            by_category[category] = dict(status="recorded" if selected else "not_recorded", events=len(selected), wall_ns=length(union(
                [(e["begin_ts"], e["end_ts"]) for e in selected])), intervals=[
                    dict(event_id=e["event_id"], name=e.get("name"), begin_ts=e["begin_ts"], end_ts=e["end_ts"],
                         thread_id=e["thread_id"], evidence=e.get("evidence", "observed")) for e in selected])
        for category in ("memory_exposed", "frontend_branch"):
            by_category[category] = dict(status="unavailable", events=0, wall_ns=None, intervals=[])
        frame_requests = union([(e["begin_ts"], e["end_ts"]) for e in members if e.get("kind") == "request"])
        frame_compute = union([(e["begin_ts"], e["end_ts"]) for e in members if
                                e.get("kind") == "range" and e.get("category") in ("mat", "vec", "cc")])
        frames.append(dict(frame_id=frame_id, name=frame.get("name"), begin_ts=frame["begin_ts"],
                           end_ts=frame.get("end_ts"), layer=frame["args"].get("layer"),
                           microbatch=frame["args"].get("microbatch"), operator_id=frame["args"].get("operator_id"),
                           categories=by_category, compute_ns=length(frame_compute), request_ns=length(frame_requests),
                           request_lifecycle_scope="producer frame attribution; completion may follow frame end",
                           compute_request_overlap_ns=length(intersection(frame_compute, frame_requests))))
    return dict(business=business,
        statistics={k: overall[k] for k in ("wall_ns", "compute_ns", "compute_parallel_ns", "request_ns", "overlap_ns", "wait_ns", "exact")},
        layers={"layer1_annotated_compute_ns": length(layers[1]),
                "layer2_annotated_compute_ns": length(layers[2]),
                "layer_compute_overlap_ns": length(layer_overlap), "overlap_intervals": layer_overlap},
        frames=sorted(frames, key=lambda f: (f.get("microbatch", -1), f.get("layer", -1))),
        categories=summarize_categories(data), quality=data["quality"])


def main():
    compiler_default = SERVER_GCC if Path(SERVER_GCC).exists() else shutil.which("g++") or "g++"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--compiler", default=compiler_default)
    parser.add_argument("--cxx-rpath", default=SERVER_LIB if Path(SERVER_LIB).exists() else "")
    for option, default in (("microbatches", 8), ("batch", 16), ("input", 256), ("hidden", 512), ("output", 256)):
        parser.add_argument("--" + option, type=int, default=default)
    args = parser.parse_args()
    output = ROOT / "artifacts" / "mlp-demo"
    output.mkdir(parents=True, exist_ok=True)
    executable = output / ("mlp_pipeline.exe" if os.name == "nt" else "mlp_pipeline")
    sources = [ROOT / "examples/mlp_pipeline.cpp", ROOT / "sdk/lxtrace.cpp", ROOT / "sdk/lxtrace.h",
               ROOT / "sdk/lxtrace_adapter.h"]
    rebuild = args.build or not executable.exists() or any(p.stat().st_mtime > executable.stat().st_mtime for p in sources)
    command = [args.compiler, "-std=c++11", "-O2", "-ffp-contract=off", "-pthread", "-Isdk",
               "sdk/lxtrace.cpp", "examples/mlp_pipeline.cpp", "-o", str(executable)]
    if args.cxx_rpath:
        command.append("-Wl,-rpath," + args.cxx_rpath)
    if rebuild:
        subprocess.run(command, cwd=str(ROOT), env=workspace_environment(), check=True, timeout=90)
    directory = output / ("run-" + uuid.uuid4().hex[:12])
    directory.mkdir()
    model_arguments = [str(value) for pair in (("--" + option, getattr(args, option))
        for option in ("microbatches", "batch", "input", "hidden", "output")) for value in pair]
    from lxtrace.core import analyze, finalize_session, sha256, write_json, write_report
    from lxtrace.importers import load_sdk
    summaries = {}
    for mode in ("serial", "pipeline"):
        print("\n[MLP %s] FP32: Linear -> bias + ReLU -> Linear -> bias. No pacing sleeps." % mode, flush=True)
        raw = directory / (mode + ".raw.jsonl")
        process_command = [str(executable), mode] + model_arguments
        env = workspace_environment(dict(LXTRACE_OUTPUT=str(raw), LXTRACE_DISABLED="0", LXTRACE_CAPACITY="32768"))
        process = subprocess.run(process_command, cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", timeout=60)
        (directory / (mode + ".stdout.txt")).write_text(process.stdout, encoding="utf-8")
        (directory / (mode + ".stderr.txt")).write_text(process.stderr, encoding="utf-8")
        print(process.stdout, end="", flush=True)
        if process.stderr:
            print(process.stderr, file=sys.stderr)
        if not raw.exists():
            raise RuntimeError("MLP produced no capture: " + str(process.returncode))
        business = json.loads(next(line[7:] for line in process.stdout.splitlines() if line.startswith("RESULT ")))
        data = finalize_session(load_sdk(raw))
        binding = [{"thread_id": e["thread_id"], **e["args"]} for e in data["events"]
                   if e["name"] == "thread.static_affinity"]
        data["app"]["workload_config"].update(mode=mode, model_result=business, binding=binding,
            transport="shared_memory_copy_queue", stages=2, activation_queue_capacity=2,
            reference_validation="independent long-double inference outside captured frame",
            frame_definition="one microbatch layer-forward per frame; inference driver is enclosing context")
        data["app"]["executable_sha256"] = sha256(executable)
        data["capture"] = dict(command=process_command, exit_code=process.returncode,
                               measured_scope="per-layer/microbatch frames plus mlp.inference driver context; reference and initialization excluded")
        write_json(data, directory / (mode + ".lxtrace"))
        write_json(analyze(data, 0, max(1, data["time_range"][1])), directory / (mode + ".analysis.json"))
        write_report(data, directory / (mode + ".html"))
        if mode == "pipeline":
            write_report(data, directory / "index.html")
        summaries[mode] = summarize_run(data, business)
    same_fields = ("output_checksum", "microbatches", "microbatch_rows", "input_features", "hidden_features", "output_features", "macs", "activation_bytes_total")
    checks = dict(same_outputs_and_work=all(summaries["serial"]["business"][key] ==
        summaries["pipeline"]["business"][key] for key in same_fields),
        reference_valid=all(s["business"]["reference_valid"] for s in summaries.values()),
        complete_capture=all(not any(s["quality"].get(key, 0) for key in
            ("dropped_records", "protocol_errors", "incomplete_events")) for s in summaries.values()),
        serial_parallel_ns=summaries["serial"]["statistics"]["compute_parallel_ns"],
        pipeline_parallel_ns=summaries["pipeline"]["statistics"]["compute_parallel_ns"])
    write_json(dict(runs=summaries, checks=checks, build_command=command,
                   metrics_scope="software wall time; eight categories are not an exclusive hardware decomposition"), directory / "summary.json")
    latest = output / "latest"
    latest.mkdir(exist_ok=True)
    old_audit = latest / "audit.json"
    if old_audit.is_file() and not (directory / "audit.json").exists():
        old_audit.unlink()
    for source in directory.iterdir():
        if source.is_file():
            shutil.copy2(str(source), str(latest / source.name))
    print("\nRESULT CHECK: same outputs/work=%s, long-double reference=%s, complete capture=%s" %
          (checks["same_outputs_and_work"], checks["reference_valid"], checks["complete_capture"]))
    for mode, summary in summaries.items():
        print("  %-8s inference=%8.3f ms  two-compute-thread parallel=%8.3f ms  compute/request overlap=%8.3f ms" %
            (mode, summary["business"]["elapsed_wall_ms"], summary["statistics"]["compute_parallel_ns"] / 1e6,
             summary["statistics"]["overlap_ns"] / 1e6))
    print("  mat/vec/exchange/wait/driver-outside: software evidence only")
    print("  exposed-memory/frontend-branch: unavailable; intra-layer cross-core: not present in this model")
    print("\nOpen in the SAME LX2 Trace viewer: " + str(latest / "pipeline.html"))
    print("Serial control: " + str(latest / "serial.html"))
    print("Saved evidence: " + str(directory))
    return 0 if all(checks[key] for key in ("same_outputs_and_work", "reference_valid", "complete_capture")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
