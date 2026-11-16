#!/usr/bin/env python3
"""Run a tiny, one-thread CPU lesson and create its offline profile report."""
import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path
from lxtrace.workspace import workspace_environment

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true", help="build with the already installed server GCC")
    args = parser.parse_args()
    output = ROOT / "artifacts" / "cpu-lesson"
    output.mkdir(parents=True, exist_ok=True)
    executable = output / "cpu_first_steps"
    if args.build:
        compiler = "/work_ssd/software/HPCKit/25.1.0.SPC001/compiler/gcc/bin/g++"
        command = [compiler, "-std=c++11", "-O2", "-pthread", "-Isdk", "sdk/lxtrace.cpp",
                   "examples/cpu_first_steps.cpp", "-o", str(executable),
                   "-Wl,-rpath,/work_ssd/software/HPCKit/25.1.0.SPC001/compiler/gcc/lib64"]
        subprocess.run(command, cwd=str(ROOT), env=workspace_environment(), check=True)
    if not executable.exists():
        parser.error("sample not built; run python3 cpu_lesson.py --build once")
    run_directory = output / ("run-" + uuid.uuid4().hex[:12])
    run_directory.mkdir()
    session = run_directory / "session.lxtrace"
    subprocess.run([sys.executable, "-m", "lxtrace", "record", "--profile", "basic",
                    "--output", str(session), "--", str(executable)], cwd=str(ROOT),
                   env=workspace_environment(), check=True)
    from lxtrace.core import analyze, write_json, write_report
    data = json.loads(session.read_text(encoding="utf-8"))
    query = analyze(data, 0, max(1, data["time_range"][1]))
    write_json(query, run_directory / "analysis.json")
    write_report(data, run_directory / "report.html")
    # Stable entry points make copying the user's own newest run easy.
    write_json(data, output / "latest.lxtrace")
    write_report(data, output / "latest.html")
    print("\nPROFILE: actual annotated ranges (wall time, not CPU occupancy)")
    for event in data["events"]:
        if event["kind"] in ("range", "wait") and event.get("end_ts") is not None:
            print("  %-24s %8.3f ms" % (event["name"], (event["end_ts"]-event["begin_ts"])/1e6))
    print("  compute ranges: %.3f ms; marked wait: %.3f ms" % (query["compute_ns"]/1e6, query["wait_ns"]/1e6))
    print("  request overlap: N/A (this lesson has no exchange requests)")
    print("\nReport: " + str(output / "latest.html"))
    print("Saved unique run: " + str(run_directory))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
