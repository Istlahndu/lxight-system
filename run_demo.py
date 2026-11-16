#!/usr/bin/env python3
"""Reproducible, local-only build and capture; never changes account settings."""
import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from lxtrace.core import analyze, doctor, finalize_session, sha256, write_json, write_report
from lxtrace.workspace import workspace_environment


def run(command, **kwargs):
    kwargs["env"] = workspace_environment(kwargs.get("env"))
    p = subprocess.run(command, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                       encoding="utf-8", errors="replace", **kwargs)
    if p.returncode:
        raise RuntimeError("command failed: " + repr(command) + "\n" + p.stdout)
    return p.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="artifacts")
    parser.add_argument("--compiler", default=os.environ.get("CXX", "g++"))
    parser.add_argument("--cxx-rpath", help="optional runtime library path for an existing compiler toolchain")
    parser.add_argument("--benchmark-runs", type=int, default=5)
    args = parser.parse_args()
    out = (ROOT / args.output).resolve()
    if ROOT not in out.parents:
        raise ValueError("output must be a child of demo directory")
    out.mkdir(parents=True, exist_ok=True)
    exe = out / ("pipeline.exe" if os.name == "nt" else "pipeline")
    command = [args.compiler, "-O2", "-std=c++11", "-pthread", "-Isdk", "sdk/lxtrace.cpp", "examples/pipeline.cpp", "-o", str(exe)]
    if args.cxx_rpath:
        command.append("-Wl,-rpath," + args.cxx_rpath)
    build_started = time.perf_counter()
    output = run(command)
    (out / "build.log").write_text(repr(command) + "\n" + output, encoding="utf-8")
    build_seconds = time.perf_counter() - build_started
    from lxtrace.importers import load_sdk, load_gftrace
    snapshot = doctor()
    write_json(snapshot, out / "capability.json")
    runs = {}
    for mode, extra, capacity in [("demo", [], "32768"), ("incomplete", ["--open"], "32768"), ("loss", ["--stress"], "64")]:
        raw = out / (mode + ".raw.jsonl")
        env = dict(os.environ, LXTRACE_OUTPUT=str(raw), LXTRACE_DISABLED="0", LXTRACE_CAPACITY=capacity)
        business = run([str(exe)] + extra, env=env)
        (out / (mode + ".business.log")).write_text(business, encoding="utf-8")
        session = load_sdk(raw)
        for key in ("capability_manifest", "topology", "host_id", "authorized_uid", "boot_id"):
            session[key] = snapshot[key]
        session.setdefault("app", {})["executable_sha256"] = sha256(exe)
        session["capture"] = {"command": [str(exe)] + extra, "mode": mode, "compiler_command": command}
        session = finalize_session(session)
        write_json(session, out / (mode + ".lxtrace"))
        write_report(session, out / (mode + ".html"))
        runs[mode] = {"business": business.strip(), "events": len(session["events"]),
                      "quality": session["quality"], "analysis": analyze(session, 0, max(1, session["time_range"][1]))}
        runs[mode]["analysis"].pop("events", None)
        runs[mode]["analysis"].pop("selected_events", None)
    gf = finalize_session(load_gftrace(ROOT / "fixtures/gftrace-demo.bin", ROOT / "fixtures/gftrace-demo.txt"))
    write_json(gf, out / "gftrace.lxtrace")
    write_report(gf, out / "gftrace.html")
    bench = {}
    for mode in ("disabled", "enabled"):
        elapsed, checksum = [], []
        for i in range(max(1, min(args.benchmark_runs, 20))):
            env = dict(os.environ, LXTRACE_OUTPUT=str(out / "benchmark.raw.jsonl"),
                       LXTRACE_DISABLED="1" if mode == "disabled" else "0", LXTRACE_CAPACITY="32768")
            started = time.perf_counter()
            checksum.append(run([str(exe)], env=env).strip())
            elapsed.append(time.perf_counter() - started)
        bench[mode] = {"wall_seconds": elapsed, "median_seconds": statistics.median(elapsed), "business_outputs": checksum}
    bench["business_outputs_match"] = len(set(bench["disabled"]["business_outputs"] + bench["enabled"]["business_outputs"])) == 1
    bench["median_overhead_ratio"] = bench["enabled"]["median_seconds"] / bench["disabled"]["median_seconds"] - 1
    bench["interpretation"] = "Short sample process wall time includes startup, sleeps, instrumentation and JSON flush. No production budget acceptance."
    evidence = {"host": platform.node(), "platform": platform.platform(), "python": platform.python_version(),
                "build_seconds": build_seconds, "compiler": run([args.compiler, "--version"]).splitlines()[0],
                "runs": runs, "benchmark": bench, "compute_node_validation": False,
                "capability_sha256": sha256(out / "capability.json")}
    write_json(evidence, out / "validation.json")
    run([sys.executable, "-m", "lxtrace", "bundle-viewer", str(out / "demo.lxtrace"), "--output", str(out / "viewer")])
    print(json.dumps({"output": str(out), "demo_events": runs["demo"]["events"], "business_outputs_match": bench["business_outputs_match"],
                      "incomplete_events": runs["incomplete"]["quality"].get("incomplete_events"),
                      "loss_dropped_records": runs["loss"]["quality"].get("dropped_records")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
