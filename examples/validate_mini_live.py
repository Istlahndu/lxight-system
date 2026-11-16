#!/usr/bin/env python3
"""Check one small uninstrumented workload through the two limited live profiles.

Run from the project with ``python3 examples/validate_mini_live.py``.  This is a
small behavior/integrity check, not H1/H2, an overhead benchmark, or proof of
continuous residency.  No existing process is attached and no privilege or
system policy is changed.
"""
import datetime
import hashlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lxtrace.core import write_json
from lxtrace.resource import analyze_resources, load_session, write_report
from lxtrace.workspace import workspace_environment


def require(condition, message):
    if not condition:
        raise ValueError(message)


def checksum(stdout):
    values = re.findall(r"^checksum=([0-9a-f]{16})$", stdout, re.MULTILINE)
    require(len(values) == 1, "expected exactly one deterministic checksum line")
    return values[0]


def invoke(command, log, env, timeout=30):
    before = time.monotonic_ns()
    result = subprocess.run(command, cwd=str(ROOT), env=env, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=timeout)
    elapsed = time.monotonic_ns() - before
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    write_json(dict(command=command, returncode=result.returncode, elapsed_ns=elapsed,
                    stdout=result.stdout, stderr=result.stderr), str(log) + ".json")
    return result, elapsed


def raw_integrity(archive, session):
    """load_session already validates every manifest hash; independently list raw evidence."""
    require(session.get("archive", {}).get("integrity") == "verified", "native archive integrity not verified")
    files = session["archive"].get("files", {})
    require(bool(files), "archive manifest has no files")
    raw_names = sorted(name for name in files if name.startswith("raw/"))
    require(bool(raw_names), "raw evidence is missing")
    details = []
    for name in raw_names:
        path = (archive / name).resolve()
        path.relative_to(archive.resolve())
        require(path.is_file(), "raw evidence file missing: " + name)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        require(digest == files[name], "raw evidence hash differs: " + name)
        details.append(dict(path=name, bytes=path.stat().st_size, sha256=digest))
    for record in session["records"]:
        require(bool(record.get("raw_refs")), "record has no raw reference")
        for reference in record["raw_refs"]:
            name = reference.split("#", 1)[0]
            require(name in raw_names, "raw reference does not name a hashed archive member: " + reference)
    return details


def validate_analysis(session):
    result = analyze_resources(session)
    # These limited profiles deliberately have no verified job allocation or scheduler stream.
    for key in ("target_cpu_ns", "other_cpu_ns", "idle_cpu_ns", "unknown_cpu_ns",
                "capacity_cpu_ns", "effective_capacity_cpu_ns", "coverage_ratio",
                "target_wall_ns", "peak_target_cpus", "mean_target_cpus", "utilization"):
        require(result.get(key) is None, "unavailable residency/capacity became a number: " + key)
    require(result["hardware_overlap"]["status"] == "unavailable", "hardware overlap must be unavailable")
    require(not any(record.get("category") in ("schedule", "sched", "residency", "on_cpu")
                    for record in session["records"]), "continuous residency was invented")
    return result


def validate_samples(session, archive):
    records = session["records"]
    require(bool(records), "no proc samples collected")
    require(all(record["representation"] == "sample" for record in records), "proc profile created a non-sample record")
    start, end = session["time_range"]
    raw = [json.loads(line) for line in (archive / "raw/proc-sample/samples.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    require(len(raw) == len(records), "sample and raw record counts differ")
    grouped = {}
    ids_by_identity = {}
    for record, evidence in zip(records, raw):
        timestamp = record["timestamp_ns"]
        require(start <= timestamp < end, "sample timestamp outside session")
        require(record.get("begin_ns") is None and record.get("end_ns") is None, "sample invented interval boundaries")
        identity = (record["tid"], evidence["start_ticks"])
        expected_id = "thread:%d:%d" % identity
        require(record["thread_instance_id"] == expected_id, "unstable thread instance ID")
        require(ids_by_identity.setdefault(identity, expected_id) == record["thread_instance_id"], "same thread identity changed ID")
        require(record["pid"] == session["capture"]["pid"], "sample not associated with own launched PID")
        require(record["cpu"] == evidence["cpu"], "sample CPU differs from raw evidence")
        require(record["utime_ticks"] == evidence["utime_ticks"] and record["stime_ticks"] == evidence["stime_ticks"], "sample ticks differ from raw evidence")
        grouped.setdefault(expected_id, []).append(record)
    require(len(grouped) >= 3, "expected main thread and two worker thread instances")
    for thread_records in grouped.values():
        for earlier, later in zip(thread_records, thread_records[1:]):
            require(earlier["timestamp_ns"] <= later["timestamp_ns"], "sample timestamps go backwards")
            require(earlier["utime_ticks"] <= later["utime_ticks"] and earlier["stime_ticks"] <= later["stime_ticks"], "cumulative ticks go backwards for same thread instance")
    return dict(sample_count=len(records), thread_instance_count=len(grouped),
                thread_instance_ids=sorted(grouped), point_timestamps_in_range=True,
                raw_records_match=True, stable_thread_instances=True)


def validate_pmu(session, archive):
    records = session["records"]
    require(len(records) == 2, "expected two task PMU event windows")
    require(all(record["representation"] == "counter_window" for record in records), "PMU profile created non-window activity")
    require({record["event"] for record in records} == {"cycles:u", "instructions:u"}, "unexpected PMU event set")
    evidence = json.loads((archive / "raw/task-pmu/events.json").read_text(encoding="utf-8"))
    require(len(evidence) == len(records), "PMU and raw event counts differ")
    results = []
    for record, raw in zip(records, evidence):
        require(record["raw"] > 0, "PMU raw counter is not positive")
        require(record["enabled_ns"] >= record["running_ns"] > 0, "invalid PMU enabled/running times")
        expected = record["raw"] * record["enabled_ns"] / record["running_ns"]
        require(math.isclose(record["scaled"], expected, rel_tol=1e-12, abs_tol=1e-6), "PMU scaled count differs from raw*enabled/running")
        require(all(record[key] == raw[key] for key in ("event", "raw", "enabled_ns", "running_ns", "scaled")), "PMU record differs from raw evidence")
        require(record["begin_ns"] == session["time_range"][0] and record["end_ns"] == session["time_range"][1], "PMU outer collection window differs from session")
        require(record.get("uncertainty_ns") is None, "unknown PMU read/exec timing uncertainty was hidden")
        results.append({key: record[key] for key in ("event", "raw", "enabled_ns", "running_ns", "scaled", "multiplex_ratio")})
    require(all(source["scope"] == "own_launched_process_tree:user" for source in session["sources"]), "task PMU scope changed")
    return dict(counter_count=len(records), counter_windows_only=True, counters=results)


def main():
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    out = ROOT / "artifacts" / "v2" / ("mini-live-" + stamp)
    out.resolve().relative_to((ROOT / "artifacts" / "v2").resolve())
    out.mkdir(parents=True, exist_ok=False)
    env = workspace_environment(dict(PYTHONIOENCODING="utf-8"))
    os.environ.update(env)
    command = [sys.executable, str(ROOT / "examples/mini_workload.py")]
    summary = dict(schema_version="lxtrace.mini-live-check.v1", created_at=stamp,
                   host=platform.node(), platform=platform.platform(), python=sys.version,
                   output=str(out), workload=command, scope="own_launch_only; no SDK; no attach; no privilege changes",
                   H1="blocked", H2="blocked", profiles={}, checks={})
    baseline_checksum = None
    try:
        baseline, elapsed = invoke(command, out / "baseline.log", env)
        require(baseline.returncode == 0, "baseline failed")
        baseline_checksum = checksum(baseline.stdout)
        summary["baseline"] = dict(status="pass", exit_code=0, checksum=baseline_checksum, elapsed_ns=elapsed)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        summary["baseline"] = dict(status="fail", reason=str(exc))
    for profile in ("proc-sample", "task-pmu"):
        archive = out / (profile + ".lxtrace")
        try:
            capture, elapsed = invoke([sys.executable, "-m", "lxtrace", "record", "--profile", profile,
                                       "--output", str(archive), "--"] + command, out / (profile + ".log"), env)
            require(capture.returncode == 0, "capture exit=%d: %s" % (capture.returncode, capture.stderr.strip()))
            require(baseline_checksum is not None and checksum(capture.stdout) == baseline_checksum, "checksum differs from baseline")
            session = load_session(archive)
            require(session["data_origin"] == "live", "session is not live")
            require(session.get("capture", {}).get("exit_code") == 0, "workload exit not observed as zero")
            require(not session["capture"].get("workload_running"), "workload still running")
            raw_files = raw_integrity(archive, session)
            details = validate_samples(session, archive) if profile == "proc-sample" else validate_pmu(session, archive)
            analysis = validate_analysis(session)
            write_json(analysis, out / (profile + ".analysis.json"))
            write_report(session, out / (profile + ".html"))
            summary["profiles"][profile] = dict(status="pass", business_exit_code=0, data_origin="live",
                                                checksum=baseline_checksum, checksum_matches_baseline=True,
                                                elapsed_ns=elapsed, record_count=len(session["records"]),
                                                raw_integrity="verified", raw_files=raw_files,
                                                residency_and_capacity="unavailable", hardware_overlap="unavailable", **details)
        except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
            summary["profiles"][profile] = dict(status="blocked" if Path(str(archive) + ".capability.json").exists() else "fail", reason=str(exc))
    marker = out / "strict-profile-business-must-not-run.txt"
    guarded = [sys.executable, "-c", "from pathlib import Path; Path(" + repr(str(marker)) + ").write_text('ran')"]
    try:
        strict_path = out / "resource.lxtrace"
        strict, _ = invoke([sys.executable, "-m", "lxtrace", "record", "--profile", "resource", "--output", str(strict_path), "--"] + guarded,
                           out / "resource.log", env)
        failure_path = Path(str(strict_path) + ".capability.json")
        evidence = json.loads(failure_path.read_text(encoding="utf-8")) if failure_path.exists() else {}
        guarded_ok = strict.returncode != 0 and not marker.exists() and evidence.get("status") == "blocked" and evidence.get("workload_started") is False
        require(guarded_ok, "strict resource source failure did not block before business startup")
        summary["checks"]["strict_resource_gate"] = dict(status="pass", expected_profile_status="blocked", workload_not_started=True, capability_file=failure_path.name)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        summary["checks"]["strict_resource_gate"] = dict(status="fail", reason=str(exc))
    passed = (summary.get("baseline", {}).get("status") == "pass"
              and all(summary["profiles"].get(profile, {}).get("status") == "pass" for profile in ("proc-sample", "task-pmu"))
              and summary["checks"].get("strict_resource_gate", {}).get("status") == "pass")
    summary["status"] = "pass" if passed else "fail"
    summary["interpretation"] = "Small live evidence and business-result check only; no continuous scheduling, full-domain, mature-engine, hardware-activity or overhead acceptance."
    write_json(summary, out / "summary.json")
    write_json(dict(directory=str(out), summary=str(out / "summary.json"), status=summary["status"],
                    created_at=stamp), ROOT / "artifacts/v2/latest-mini-live.json")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
