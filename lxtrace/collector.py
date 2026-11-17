"""Unprivileged external collectors with explicit source and authorization limits.

The resource profile is deliberately strict: a missing continuous scheduler
adapter cannot be replaced by /proc polling.  The two smaller profiles are
named after the evidence they actually collect, and never claim CPU residency.
"""
import ctypes
import errno
import hashlib
import json
import os
import platform
import shutil
import socket
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .workspace import temporary_directory, workspace_environment

SCHEMA = "lxtrace.resource-session.v2"
VERSION = "0.2.0"
EVENTS = (("cycles:u", 0), ("instructions:u", 1))
LINUX = sys.platform.startswith("linux")


class CollectorBlocked(ValueError):
    """A requested profile cannot be collected; the workload was not started."""
    def __init__(self, reason, capability=None):
        super().__init__(reason)
        self.reason = reason
        self.capability = capability


def _read(path, default=None):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return default


def _cpulist(text):
    result = set()
    for part in (text or "").split(","):
        if not part:
            continue
        pair = part.split("-")
        start = int(pair[0])
        end = int(pair[-1])
        if end < start or end - start > 1_000_000:
            raise ValueError("invalid CPU list")
        result.update(range(start, end + 1))
    return sorted(result)


def _number(path):
    text = _read(path)
    try:
        value = int(text)
        return value if value >= 0 else None
    except (TypeError, ValueError):
        return None


def topology():
    """Read public topology; unknown domain/die relationships remain unknown."""
    resources = [dict(resource_id="node:" + socket.gethostname(), type="node",
                      parent_id=None, name=socket.gethostname(), mapping_source="hostname")]
    node_id = resources[0]["resource_id"]
    try:
        affinity = sorted(os.sched_getaffinity(0))
    except AttributeError:
        affinity = list(range(os.cpu_count() or 1))
    online = _cpulist(_read("/sys/devices/system/cpu/online")) if LINUX else affinity
    online = online or affinity
    seen = {node_id}

    def add(item):
        if item["resource_id"] not in seen:
            resources.append(item)
            seen.add(item["resource_id"])

    for cpu in online:
        base = Path("/sys/devices/system/cpu/cpu%d" % cpu)
        package = _number(base / "topology/physical_package_id") if LINUX else None
        die = _number(base / "topology/die_id") if LINUX else None
        core = _number(base / "topology/core_id") if LINUX else None
        parent = node_id
        if package is not None:
            parent = "package:%d" % package
            add(dict(resource_id=parent, type="package", parent_id=node_id,
                     name="Package %d" % package, mapping_source="sysfs:physical_package_id"))
        if die is not None and package is not None:
            die_id = "die:%d:%d" % (package, die)
            add(dict(resource_id=die_id, type="die", parent_id=parent,
                     name="Die %d" % die, mapping_source="sysfs:die_id"))
            parent = die_id
        if core is not None and package is not None:
            core_id = "core:%s:%s:%s" % (package, die if die is not None else "unknown", core)
            add(dict(resource_id=core_id, type="core", parent_id=parent,
                     name="Core %d" % core, physical_core=core,
                     mapping_source="sysfs:core_id;die_unknown" if die is None else "sysfs:core_id"))
            parent = core_id
        links = []
        if LINUX:
            for memory in sorted(base.glob("node[0-9]*")):
                mem_id = "memory:" + memory.name
                add(dict(resource_id=mem_id, type="memory", parent_id=node_id,
                         name=memory.name, mapping_source="sysfs:cpu/node symlink"))
                links.append(mem_id)
            for index in sorted((base / "cache").glob("index*")):
                shared = _read(index / "shared_cpu_list")
                level = _read(index / "level")
                cache_type = _read(index / "type")
                if shared and level:
                    cache_id = "cache:L%s:%s:%s" % (level, cache_type, shared)
                    add(dict(resource_id=cache_id, type="cache", parent_id=node_id,
                             name="L%s %s · CPUs %s" % (level, cache_type, shared),
                             level=int(level), size=_read(index / "size"),
                             shared_cpu_list=shared, mapping_source="sysfs:cache"))
                    links.append(cache_id)
        resources.append(dict(resource_id="logical_cpu:%d" % cpu, type="logical_cpu",
                              parent_id=parent, name="CPU %d" % cpu, logical_cpu=cpu,
                              physical_core=core, die=die, domain=None, links=links,
                              mapping_source="sysfs" if LINUX else "os.cpu_count;topology_unknown"))
    return resources, affinity


def _event_open(event_config, pid=0, cpu=-1, inherit=False, enable_on_exec=False, group_fd=-1, event_type=0):
    """Open a disabled PMU event; never changes system configuration."""
    if not LINUX:
        raise OSError(errno.ENOSYS, "perf_event_open requires Linux")
    machine = platform.machine().lower()
    syscall_number = {"x86_64": 298, "amd64": 298, "aarch64": 241, "arm64": 241}.get(machine)
    if syscall_number is None:
        raise OSError(errno.ENOSYS, "unsupported perf_event_open syscall architecture")
    attr = ctypes.create_string_buffer(128)
    # PERF_TYPE_HARDWARE by default; PERF_TYPE_RAW only when explicitly requested.
    flags = 1 | (1 << 5) | (1 << 6)
    if inherit:
        flags |= 1 << 1
    if enable_on_exec:
        flags |= 1 << 12
    struct.pack_into("=IIQQQQQ", attr, 0, event_type, 128, event_config, 0, 0, 3, flags)
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.syscall(ctypes.c_long(syscall_number), ctypes.byref(attr),
                      ctypes.c_int(pid), ctypes.c_int(cpu), ctypes.c_int(group_fd), ctypes.c_ulong(8))
    if fd < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return int(fd)


def _probe_event(config, cpu=-1, event_type=0):
    try:
        options = dict(pid=-1 if cpu >= 0 else 0, cpu=cpu)
        if event_type:
            options["event_type"] = event_type
        fd = _event_open(config, **options)
        os.close(fd)
        return dict(availability="available", errno=None, reason_code="disabled_event_open_succeeded",
                    failure_stage=None)
    except OSError as exc:
        return dict(availability="denied" if exc.errno in (errno.EACCES, errno.EPERM) else
                    "unsupported" if exc.errno in (errno.ENOSYS, errno.ENOENT, errno.ENODEV,
                                                    errno.EOPNOTSUPP) else "unverified",
                    errno=exc.errno, reason_code=str(exc), failure_stage="perf_event_open")


def _task_events(profile, config):
    """Retain the default event contract; RAW opt-in is cycles plus exact codes."""
    if "raw_events" not in config:
        return [(name, code, 0) for name, code in EVENTS]
    if profile != "task-pmu":
        raise ValueError("raw_events is supported only by the task-pmu profile")
    requested = config["raw_events"]
    if not isinstance(requested, list) or len(requested) > 8:
        raise ValueError("raw_events must be a list of at most eight distinct 16-bit event codes")
    codes = []
    for raw in requested:
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise ValueError("RAW event codes must be integers or 0x-prefixed hexadecimal strings")
        if isinstance(raw, str):
            if not raw.startswith(("0x", "0X")) or len(raw) <= 2 or any(c not in "0123456789abcdefABCDEF" for c in raw[2:]):
                raise ValueError("RAW string event codes must use 0x-prefixed hexadecimal")
            code = int(raw, 16)
        else:
            code = raw
        if not 0 <= code <= 0xffff:
            raise ValueError("RAW event codes must fit 16 bits")
        if code in codes:
            raise ValueError("RAW event codes must be distinct")
        codes.append(code)
    return [("cycles:u", 0, 0)] + [("raw:0x%04x:u" % code, code, 4) for code in codes]


def _cap(feature, availability="unverified", reason="not_probed", scope=None, **extra):
    result = dict(feature_id=feature, feature=feature, availability=availability,
                  implementation_status="not_implemented", semantics_status="unverified",
                  boundary_support="none", correlation_status="unverified", reason_code=reason,
                  errno=None, failure_stage=None, event=None, scope=scope, filter=None,
                  evidence_ref="raw/capability/snapshot.json", checked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  platform_version=platform.platform())
    result.update(extra)
    return result


def doctor(job_id=None, probe=True):
    """A fresh capability snapshot, including safe disabled permission probes."""
    resources, affinity = topology()
    source_info = []
    for event, config in EVENTS:
        result = _probe_event(config) if probe else dict(availability="unverified", reason_code="probe_disabled")
        source_info.append(_cap("task_pmu." + event, scope="own_launched_process_tree:user",
                                implementation_status="implemented", semantics_status="generic_linux_event",
                                boundary_support="counter_window", correlation_status="process_instance",
                                event=event, filter="exclude_kernel,exclude_hypervisor", **result))
    result = _probe_event(0, affinity[0]) if probe and affinity else dict(availability="unverified", reason_code="probe_disabled_or_no_cpu")
    source_info.append(_cap("core_pmu", scope="logical_cpu", event="cycles:u",
                            implementation_status="not_implemented", boundary_support="counter_window",
                            semantics_status="generic_linux_event", **result))
    metadata = []
    for trace_root in ("/sys/kernel/tracing/events/sched", "/sys/kernel/debug/tracing/events/sched"):
        for event in ("sched_switch", "sched_wakeup", "sched_wakeup_new", "sched_migrate_task",
                      "sched_process_fork", "sched_process_exit"):
            try:
                file = Path(trace_root) / event / "format"
                content = file.read_text(encoding="utf-8")
                metadata.append(dict(path=str(file), event=event, status="readable",
                                     sha256=hashlib.sha256(content.encode()).hexdigest()))
            except OSError as exc:
                metadata.append(dict(path=str(Path(trace_root) / event / "format"), event=event,
                                     status="denied" if exc.errno in (errno.EACCES, errno.EPERM) else "unavailable",
                                     errno=exc.errno))
    readable = any(m["status"] == "readable" for m in metadata)
    denied = next((m for m in metadata if m["status"] == "denied"), None)
    source_info.append(_cap("schedule", availability="denied" if denied and not readable else "unverified",
                            reason="continuous_collector_implementation_gap", scope="authorized_cpus",
                            failure_stage="tracepoint_metadata" if denied else "collector_implementation",
                            errno=denied["errno"] if denied else None,
                            metadata_access="readable" if readable else "denied" if denied else "unavailable"))
    proc_ok = LINUX and Path("/proc/self/task").is_dir()
    source_info.append(_cap("proc_sample", availability="available" if proc_ok else "unsupported",
                            reason="own_child_procfs_only" if proc_ok else "procfs_not_available",
                            scope="own_launched_process_threads", implementation_status="implemented",
                            semantics_status="linux_proc_stat_cpu_point", boundary_support="sample_only",
                            correlation_status="thread_instance"))
    for feature, scope, reason in (("spe", "sample", "decoder_and_context_implementation_gap"),
                                    ("uncore", "shared_resource", "scope_and_collector_implementation_gap"),
                                    ("hardware_activity", "hardware_resource", "verified_boundary_source_not_available"),
                                    ("device_transfer", "device_queue", "verified_device_trace_not_available")):
        source_info.append(_cap(feature, reason=reason, scope=scope))
    now = time.monotonic_ns()
    return dict(tool_version=VERSION, checked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                host_id=socket.gethostname(), boot_id=_read("/proc/sys/kernel/random/boot_id"),
                platform_version=platform.platform(), machine=platform.machine(),
                cpu_part=_read("/proc/cpuinfo"), job_id=job_id, resources=resources,
                allocation=dict(status="unverified", cpu_ids=affinity, source="process_affinity",
                                reason="affinity_is_not_trusted_job_authorization"),
                capability_manifest=source_info, tracepoint_metadata=metadata,
                clocks=[dict(clock_id="CLOCK_MONOTONIC", unit="ns", origin_ns=now,
                             resolution_ns=max(1, int(time.get_clock_info("monotonic").resolution * 1e9)),
                             error_ns=None, timestamp_origin="time.monotonic_ns")],
                deployment_status="no_trusted_collector_service", h1_status="blocked",
                gaps=["continuous_scheduler_adapter", "verified_job_allocation", "core_level_counter_capture",
                      "verified_full_domain_capture", "mature_engine_validation", "hardware_activity_boundaries"])


def _save_blocked(output, snapshot, reason):
    path = Path(str(output) + ".capability.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(status="blocked", reason=reason, workload_started=False,
                                   capability=snapshot), ensure_ascii=False, indent=2), encoding="utf-8")
    raise CollectorBlocked(reason + "; evidence=" + str(path), snapshot)


def _proc_stat(pid):
    result = []
    for path in sorted(Path("/proc/%d/task" % pid).glob("*/stat")):
        text = _read(path)
        if not text:
            continue
        try:
            result.append(_parse_proc_stat(text))
        except (ValueError, IndexError):
            continue
    return result


def _parse_proc_stat(text):
    # comm is parenthesized and may contain whitespace and closing parentheses.
    close = text.rfind(")")
    values = text[close + 2:].split()
    if close < 0 or len(values) < 37:
        raise ValueError("short proc stat")
    return dict(tid=int(text[:text.find("(")].strip()), comm=text[text.find("(")+1:close],
                state=values[0], utime_ticks=int(values[11]), stime_ticks=int(values[12]),
                start_ticks=int(values[19]), cpu=int(values[36]))


def _read_counter(fd):
    import fcntl
    fcntl.ioctl(fd, 0x2401, 0)  # PERF_EVENT_IOC_DISABLE
    data = os.read(fd, 24)
    if len(data) != 24:
        raise OSError("short perf counter read")
    raw, enabled, running = struct.unpack("=QQQ", data)
    return dict(raw=raw, enabled_ns=enabled, running_ns=running,
                scaled=raw * enabled / running if running else None,
                multiplex_ratio=running / enabled if enabled else None)


_GATE = """import os, sys, time
from pathlib import Path
ready, go = Path(sys.argv[1]), Path(sys.argv[2])
ready.write_text(str(os.getpid()), encoding='ascii')
deadline=time.monotonic()+30
while not go.exists():
 if time.monotonic()>deadline: raise SystemExit(125)
 time.sleep(.001)
os.execvpe(sys.argv[3], sys.argv[3:], os.environ)
"""


def record_external(output, program=None, attach=None, duration_ns=None, profile="resource", config=None, job_id=None):
    """Collect an explicitly named profile and return (session, exit_code).

    duration limits collection, not application lifetime.  At that boundary the
    collector closes sources and returns; it does not terminate the workload.
    An unavailable exit code is recorded as null in the session.
    """
    from .resource import write_session
    if profile not in ("resource", "task-pmu", "proc-sample"):
        raise ValueError("unknown external profile: " + str(profile))
    out = Path(output)
    if out.exists() or Path(str(out) + ".capability.json").exists():
        raise ValueError("output already exists; choose a new session path")
    if duration_ns is not None and duration_ns <= 0:
        raise ValueError("duration must be positive")
    if config is not None and not isinstance(config, dict):
        raise ValueError("config must be a JSON object")
    task_events = _task_events(profile, config or {})
    interval = float((config or {}).get("sample_interval_ms", 10)) / 1000
    if not .001 <= interval <= 10:
        raise ValueError("sample_interval_ms must be between 1 and 10000")
    quota = int((config or {}).get("max_samples", 100000))
    if not 1 <= quota <= 1000000:
        raise ValueError("max_samples must be between 1 and 1000000")
    snapshot = doctor(job_id=job_id)
    if profile == "task-pmu" and "raw_events" in (config or {}):
        for name, code, event_type in task_events:
            if event_type != 4:
                continue
            snapshot["capability_manifest"].append(_cap(
                "task_pmu." + name, scope="own_launched_process_tree:user",
                implementation_status="implemented", semantics_status="caller_supplied_raw_encoding_unverified",
                boundary_support="counter_window", correlation_status="process_instance", event=name,
                event_type=event_type, event_config=code, filter="exclude_kernel,exclude_hypervisor",
                **_probe_event(code, event_type=event_type)))
    if attach is not None:
        _save_blocked(out, snapshot, "attach requires a trusted collector service and verified allocation; shared UID/PID/job text is not authorization")
    if not program or not isinstance(program, (list, tuple)):
        raise ValueError("record requires -- <original program> [arguments]")
    if not all(isinstance(part, str) and part for part in program):
        raise ValueError("program arguments must be nonempty strings")
    required = ["schedule", "core_pmu"] if profile == "resource" else ["proc_sample"] if profile == "proc-sample" else ["task_pmu." + name for name, _, _ in task_events]
    if config:
        requested = config.get("required_sources", required)
        if requested != required:
            _save_blocked(out, snapshot, "profile source contract cannot be weakened by config; choose a specifically named profile")
    capabilities = {c["feature_id"]: c for c in snapshot["capability_manifest"]}
    missing = [name for name in required if name not in capabilities or
               capabilities[name]["availability"] != "available" or
               capabilities[name]["implementation_status"] != "implemented"]
    if missing:
        _save_blocked(out, snapshot, "required sources unavailable: " + ", ".join(missing))
    if not LINUX:
        _save_blocked(out, snapshot, "external live collection requires Linux")
    session_id = str(uuid.uuid4())
    raw_samples, records, counter_results, capture_warnings = [], [], [], []
    fds = []
    process = None
    started = time.monotonic_ns()
    with temporary_directory() as scratch:
        scratch = Path(scratch)
        ready, go, gate = scratch / "ready", scratch / "go", scratch / "gate.py"
        gate.write_text(_GATE, encoding="utf-8")
        try:
            process = subprocess.Popen([sys.executable, str(gate), str(ready), str(go)] + list(program),
                                       env=workspace_environment(), start_new_session=True)
            deadline = time.monotonic() + 10
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.001)
            if not ready.exists():
                raise ValueError("launch handshake failed before workload start")
            pid = process.pid
            initial = _proc_stat(pid)
            process_start = initial[0]["start_ticks"] if initial else None
            if profile == "task-pmu":
                leader = -1
                for event, encoding, event_type in task_events:
                    try:
                        options = dict(pid=pid, inherit=True, enable_on_exec=True, group_fd=leader)
                        if event_type:
                            options["event_type"] = event_type
                        fd = _event_open(encoding, **options)
                    except OSError as exc:
                        capabilities["task_pmu." + event].update(
                            availability="denied" if exc.errno in (errno.EACCES, errno.EPERM) else
                                         "unsupported" if exc.errno in (errno.ENOSYS, errno.ENOENT, errno.ENODEV, errno.EOPNOTSUPP) else "unverified",
                            errno=exc.errno, reason_code=str(exc), failure_stage="capture_event_open")
                        _save_blocked(out, snapshot, "task counter setup failed before workload start: " + event)
                    fds.append((event, fd))
                    if leader == -1:
                        leader = fd
            started = time.monotonic_ns()
            go.write_text("exec", encoding="ascii")
            max_time = started + duration_ns if duration_ns is not None else None
            stop_reason = "workload_exit"
            while process.poll() is None:
                before = time.monotonic_ns()
                if max_time is not None and before >= max_time:
                    stop_reason = "duration_limit;workload_continues"
                    break
                if profile == "proc-sample":
                    values = _proc_stat(pid)
                    after = time.monotonic_ns()
                    for value in values:
                        if len(raw_samples) >= quota:
                            break
                        raw_samples.append(dict(timestamp_ns=before, read_end_ns=after, **value))
                    if len(raw_samples) >= quota:
                        stop_reason = "sample_quota;workload_continues"
                        break
                time.sleep(interval if profile == "proc-sample" else .005)
            ended = time.monotonic_ns()
            for event, fd in fds:
                try:
                    item = dict(event=event, **_read_counter(fd))
                    if "raw_events" in (config or {}):
                        encoding, event_type = next((code, typ) for name, code, typ in task_events if name == event)
                        item.update(event_type=event_type, event_config=encoding,
                                    event_semantics="caller_supplied_raw_encoding_unverified" if event_type == 4 else "generic_linux_event")
                    counter_results.append(item)
                except OSError as exc:
                    counter_results.append(dict(event=event, error=str(exc), errno=exc.errno))
                    capture_warnings.append("counter_read_failed:" + event)
            exit_code = process.poll()
        except BaseException:
            # Before the go-file exists the process is our gate, never a user workload.
            if process is not None and process.poll() is None and not go.exists():
                process.terminate()
                process.wait(timeout=5)
            raise
        finally:
            for _, fd in fds:
                os.close(fd)
        duration = max(1, ended - started)
        process_id = "process:%d:%s" % (pid, process_start)
        resources = snapshot["resources"] + [dict(resource_id=process_id, type="process",
                      parent_id=snapshot["resources"][0]["resource_id"], name=Path(program[0]).name,
                      pid=pid, start_ticks=process_start, mapping_source="collector_launch_handshake")]
        source_id = "task-pmu" if profile == "task-pmu" else "proc-sample"
        sources = [dict(source_id=source_id, source_layer="hardware" if profile == "task-pmu" else "os",
                        scope="own_launched_process_tree:user" if profile == "task-pmu" else "own_launched_process_threads",
                        resource_id=process_id, clock_id="CLOCK_MONOTONIC", native_format="perf_event_read" if profile == "task-pmu" else "linux_proc_stat",
                        filter="exclude_kernel,exclude_hypervisor" if profile == "task-pmu" else "own_child_only",
                        resolution_ns=None if profile == "task-pmu" else int(interval * 1e9),
                        completeness="single_counter_window" if profile == "task-pmu" else "discrete_samples",
                        lost_records=0, boundary_support="counter_window" if profile == "task-pmu" else "sample_only")]
        if profile == "task-pmu" and "raw_events" in (config or {}):
            sources[0].update(raw_events=[code for _, code, typ in task_events if typ == 4],
                              raw_semantics="caller supplied; requires hardware-specific validation",
                              attribution_scope="launched_process_tree_aggregate;not_per_thread_or_per_core")
        for i, counter in enumerate(counter_results):
            if "error" in counter:
                continue
            records.append(dict(record_id="counter:%d" % i, resource_id=process_id, source_id=source_id,
                                category="pmu", layer="hardware", representation="counter_window",
                                begin_ns=0, end_ns=duration,
                                unit="count", value=counter["raw"], **counter,
                                boundary_semantics="outer collection/read window; counter enabled/running below; not activity boundaries",
                                uncertainty_ns=None, correlation_ids=[], raw_refs=["raw/task-pmu/events.json#%d" % i]))
        known_cpus = {r["logical_cpu"] for r in resources if r["type"] == "logical_cpu"}
        for i, value in enumerate(raw_samples):
            cpu = value["cpu"]
            rid = "logical_cpu:%d" % cpu
            if cpu not in known_cpus:
                resources.append(dict(resource_id=rid, type="logical_cpu", parent_id=snapshot["resources"][0]["resource_id"],
                                      name="CPU %d" % cpu, logical_cpu=cpu, mapping_source="proc_stat;topology_missing"))
                known_cpus.add(cpu)
            records.append(dict(record_id="sample:%d" % i, resource_id=rid, source_id=source_id,
                                category="cpu_point", layer="os", representation="sample",
                                timestamp_ns=max(0, value["timestamp_ns"] - started),
                                begin_ns=None, end_ns=None, name="TID %d · %s" % (value["tid"], value["state"]),
                                thread_instance_id="thread:%d:%d" % (value["tid"], value["start_ticks"]),
                                pid=pid, tid=value["tid"], cpu=cpu, state=value["state"],
                                utime_ticks=value["utime_ticks"], stime_ticks=value["stime_ticks"],
                                tick_frequency=os.sysconf("SC_CLK_TCK"),
                                boundary_semantics="sampled last-executed CPU and proc thread state; no continuous residency",
                                uncertainty_ns=value["read_end_ns"] - value["timestamp_ns"],
                                correlation_ids=[], raw_refs=["raw/proc-sample/samples.jsonl#%d" % i]))
        enabled = [source_id]
        snapshot_clock = dict(snapshot["clocks"][0], origin_ns=started,
                              transform=dict(scale=1, offset_ns=-started), error_ns=0,
                              calibration="same_process_monotonic_read;no_cross_device_clock")
        raw_files = {"capability/snapshot.json": json.dumps(snapshot, ensure_ascii=False, indent=2)}
        if profile == "task-pmu":
            raw_files["task-pmu/events.json"] = json.dumps(counter_results, ensure_ascii=False, indent=2)
        else:
            raw_files["proc-sample/samples.jsonl"] = "\n".join(json.dumps(r, ensure_ascii=False) for r in raw_samples) + "\n"
        session = dict(schema_version=SCHEMA, session_id=session_id, run_id=session_id, data_origin="live",
                       tool_version=VERSION, host_id=snapshot["host_id"], boot_id=snapshot["boot_id"], job_id=job_id,
                       app=dict(name=Path(program[0]).name, build_id=None), collector_versions={"external": VERSION},
                       decoder_versions={"proc_stat": "linux-stat-v1", "perf_read": "u64-raw-enabled-running-v1"},
                       support_contract_id="external-" + profile + "-v0.2", actual_profile=profile,
                       required_sources=required, enabled_sources=enabled, resources=resources,
                       allocation=snapshot["allocation"], verified_allocation=None, policy_version=None,
                       time_range=[0, duration], sources=sources, clocks=[snapshot_clock], records=records,
                       capability_manifest=snapshot["capability_manifest"],
                       quality=dict(coverage="samples_only" if profile == "proc-sample" else "task_counters_only",
                                    warnings=capture_warnings, scheduler_coverage=0,
                                    missing_sources=["schedule", "core_pmu", "hardware_activity", "device_transfer"],
                                    unknown_windows=[], dropped_records=0, h1_status="blocked"),
                       provenance=[dict(kind="external_capture", collector=VERSION, command=list(program),
                                        source="current_tool_direct_capture", raw_clock="CLOCK_MONOTONIC")],
                       capture=dict(command=list(program), pid=pid, process_instance=process_id,
                                    exit_code=exit_code, workload_running=exit_code is None, stop_reason=stop_reason,
                                    duration_policy="stop_collection_without_terminating_application",
                                    start_handshake="PMU enable_on_exec; proc polling begins at gate release",
                                    elapsed_ns=duration, cwd=str(Path.cwd())), raw_files=raw_files)
        executable = Path(shutil.which(program[0]) or program[0])
        if executable.is_file():
            session["app"]["executable_sha256"] = hashlib.sha256(executable.read_bytes()).hexdigest()
        write_session(session, out)
        return session, exit_code if exit_code is not None and exit_code >= 0 else 0 if exit_code is None else 128 - exit_code
