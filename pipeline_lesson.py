#!/usr/bin/env python3
"""Capture matching serial/pipeline CPU + local socket runs and make offline reports."""
import argparse
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from lxtrace.workspace import workspace_environment

ROOT = Path(__file__).resolve().parent
COMPILER = "/work_ssd/software/HPCKit/25.1.0.SPC001/compiler/gcc/bin/g++"
RPATH = "/work_ssd/software/HPCKit/25.1.0.SPC001/compiler/gcc/lib64"


def verify_pair(runs):
    """Return observed lesson checks; unexpected overlap never suppresses a report."""
    serial, pipeline = runs["serial"], runs["pipeline"]
    checks = dict(capture_complete=True, expected_lesson_structure=True,
                  request_completion_evidence=True, socket_transport=True,
                  receive_bytes_validated=True)
    for mode, run in runs.items():
        data, result = run["session"], run["analysis"]
        quality = data["quality"]
        if quality.get("dropped_records") or quality.get("protocol_errors") or quality.get("incomplete_events"):
            checks["capture_complete"] = False
        requests = [e for e in data["events"] if e["kind"] == "request"]
        computes = [e for e in data["events"] if e["kind"] == "range" and e["category"] == "vec"]
        if len(requests) != 3 or len(computes) != 3 or not result["exact"]:
            checks["expected_lesson_structure"] = False
        for request in requests:
            if request["args"].get("transport") != "local_unix_socketpair":
                checks["socket_transport"] = False
            if not request.get("frame_id") or not request["args"].get("completion_tid"):
                checks["request_completion_evidence"] = False
        if run["business"]["bytes_received"] != 3 * 256 * 1024:
            checks["receive_bytes_validated"] = False
    checks["same_work"] = all(serial["business"][key] == pipeline["business"][key]
        for key in ("compute_checksum", "receive_checksum", "bytes_received", "iterations_per_batch", "batches"))
    checks["serial_overlap_zero"] = serial["analysis"]["overlap_ns"] == 0
    checks["pipeline_overlap_positive"] = pipeline["analysis"]["overlap_ns"] > 0
    return checks


def export_run_reports(runs, directory):
    """Every workload uses the same application renderer; only session data differs."""
    from lxtrace.core import write_report
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for mode, run in runs.items():
        write_report(run["session"], directory / (mode + ".html"))
    # Keep the previously published URL as an ordinary viewer entry point.
    write_report(runs["pipeline"]["session"], directory / "index.html")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true", help="build with the existing compiler")
    parser.add_argument("--compiler", default=COMPILER)
    parser.add_argument("--cxx-rpath", default=RPATH)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("this real socket/affinity lesson runs on the Linux server")
    output = ROOT / "artifacts" / "pipeline-lesson"
    output.mkdir(parents=True, exist_ok=True)
    executable = output / "overlap_first_steps"
    if args.build or not executable.exists():
        command = [args.compiler, "-std=c++11", "-O2", "-pthread", "-Isdk", "sdk/lxtrace.cpp",
                   "examples/overlap_first_steps.cpp", "-o", str(executable)]
        if args.cxx_rpath:
            command.append("-Wl,-rpath," + args.cxx_rpath)
        subprocess.run(command, cwd=str(ROOT), env=workspace_environment(), check=True, timeout=90)
    directory = output / ("run-" + uuid.uuid4().hex[:12])
    directory.mkdir()
    from lxtrace.core import analyze, finalize_session, write_json
    from lxtrace.importers import load_sdk
    runs = {}
    for mode in ("serial", "pipeline"):
        print("\n[Capture %s] Same work: 3 batches, 768 KiB local socket data." % mode, flush=True)
        raw = directory / (mode + ".raw.jsonl")
        env = workspace_environment(dict(LXTRACE_OUTPUT=str(raw), LXTRACE_DISABLED="0", LXTRACE_CAPACITY="32768"))
        process = subprocess.run([str(executable), mode], cwd=str(ROOT), env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                                 encoding="utf-8", check=True, timeout=30)
        print(process.stdout, end="", flush=True)
        (directory / (mode + ".stdout.txt")).write_text(process.stdout, encoding="utf-8")
        if process.stderr:
            print(process.stderr, file=sys.stderr)
        business = json.loads(next(line[7:] for line in process.stdout.splitlines() if line.startswith("RESULT ")))
        data = finalize_session(load_sdk(raw))
        bindings = [{"thread_id": e["thread_id"], **e["args"]} for e in data["events"] if e["name"] == "线程静态绑核设置"]
        data["app"]["workload_config"] = dict(mode=mode, batches=3, bytes_per_batch=262144,
            chunk_bytes=8192, teaching_pacing_ms_between_chunks=1, binding=bindings,
            transport="local_unix_socketpair", business_result=business)
        query = analyze(data, 0, max(1, data["time_range"][1]))
        runs[mode] = dict(session=data, analysis=query, business=business)
        write_json(data, directory / (mode + ".lxtrace"))
        write_json(query, directory / (mode + ".analysis.json"))
    export_run_reports(runs, directory)
    checks = verify_pair(runs)
    summary = {mode: dict(business=run["business"],
                         statistics={key: run["analysis"][key] for key in ("wall_ns", "compute_ns", "request_ns", "overlap_ns", "exact")},
                         quality=run["session"]["quality"]) for mode, run in runs.items()}
    summary["checks"] = dict(checks, scope="software wall annotations / request lifetime")
    write_json(summary, directory / "summary.json")
    latest = output / "latest"
    latest.mkdir(exist_ok=True)
    for source in directory.iterdir():
        if source.is_file():
            shutil.copy2(str(source), str(latest / source.name))
    if all(checks.values()):
        print("\nVERIFIED: identical compute/receive checksums; complete requests; no dropped records.")
    else:
        print("\nCHECK: reports saved with the observed data; inspect summary.json for failed lesson checks.")
    for mode, run in runs.items():
        print("  %-8s work_wall=%8.3f ms  compute/request_overlap=%8.3f ms" %
              (mode, run["business"]["elapsed_wall_ms"], run["analysis"]["overlap_ns"] / 1e6))
    print("\nOpen serial in LX2 Trace:   " + str(latest / "serial.html"))
    print("Open pipeline in LX2 Trace: " + str(latest / "pipeline.html"))
    print("Unique evidence: " + str(directory))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
