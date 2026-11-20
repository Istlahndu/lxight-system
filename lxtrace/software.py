"""Record one SDK-enabled launch tree into one native session and HTML report.

The SDK flushes at shutdown, so this collector waits for the launcher. It never
terminates the business program to manufacture a recording boundary. Original
per-process JSONL, immutable read snapshots and the capture manifest are retained.
"""
import hashlib
import base64
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from .workspace import workspace_environment


CHILD_EXIT_GRACE_SECONDS = 1.0


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def _group_members(group_id):
    """Inspect only the new process group belonging to this explicit launch."""
    if os.name != "posix" or not Path("/proc").is_dir():
        return None
    members = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "stat").read_text(encoding="utf-8")
            fields = text[text.rfind(")") + 2:].split()
            if int(fields[2]) == group_id and fields[0] not in ("Z", "X"):
                members.append({"pid": int(entry.name), "start_ticks": int(fields[19]),
                                "state": fields[0]})
        except (OSError, ValueError, IndexError):
            continue
    return sorted(members, key=lambda item: item["pid"])


def _read_meta(content):
    for line in content.splitlines():
        try:
            value = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("type") == "meta":
            return value
    return {}


def record_software(output, program, html=None, duration=None, compact=False, view='resource'):
    """Return (native_v2_session, business_exit_code), always exporting HTML.

    Each process inherits a run UUID and an SDK output template containing %p.
    A process-local SDK built with template support expands this token at flush
    time. An existing output/report or any duration is rejected before launch.
    Business failure preserves available evidence and marks capture non-clean.
    No usable SDK output is an explicit error, with a retained capture manifest.
    """
    out = Path(output).resolve()
    report_path = Path(html).resolve() if html is not None else out.with_suffix(".html")
    if duration is not None:
        raise ValueError("basic software recording does not support --duration: SDK records flush at shutdown; business was not started")
    if not isinstance(program, (list, tuple)) or not program or not all(
            isinstance(part, str) and part for part in program):
        raise ValueError("basic record requires -- <SDK-enabled program> [arguments]")
    if out == report_path or out in report_path.parents or report_path in out.parents:
        raise ValueError("session output and HTML must use separate non-nested paths")
    if out.exists() or report_path.exists():
        raise ValueError("output or HTML already exists; choose new paths; business was not started")
    if "%p" in str(out.parent):
        raise ValueError("output parent contains reserved SDK template token %p; choose another directory")
    from .collector import doctor
    from .importers import load_sdk
    from .report import write_view
    from .resource import write_session
    out.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    capture_dir = out.parent / (out.stem.replace("%p", "_pid_") + "-" + run_id + ".capture")
    raw_dir, snapshot_dir = capture_dir / "raw", capture_dir / "snapshots"
    raw_dir.mkdir(parents=True)
    snapshot_dir.mkdir()
    manifest_path = capture_dir / "manifest.json"
    template = raw_dir / ("sdk-%p.lxc" if compact else "sdk-%p.jsonl")
    snapshot = doctor(probe=False)
    manifest = {"format": "lxtrace.software-capture.v1", "run_id": run_id,
                "status": "starting", "command": list(program), "cwd": str(Path.cwd()),
                "output": str(out), "html": str(report_path),
                "capture_directory": str(capture_dir), "raw_output_template": str(template),
                "host_id": snapshot.get("host_id"), "boot_id": snapshot.get("boot_id"),
                "lifecycle": "launcher_exit_then_bounded_process_group_flush_grace",
                "coverage": "SDK-enabled processes that flushed; unannotated processes are not instrumented automatically",
                "duration_supported": False, "raw_files": [], "warnings": [],
                "process_group": {"created": False, "membership_verified": False,
                                  "scope": "explicitly launched process group; detached descendants are not guaranteed"}}
    _write_json(manifest_path, manifest)
    env = workspace_environment({"LXTRACE_COMPACT_OUTPUT" if compact else "LXTRACE_OUTPUT": str(template),
                                 "LXTRACE_RUN_ID": run_id, "LXTRACE_DISABLED": "0"})
    started = time.monotonic()
    observed = {}
    launch_options = {"env": env}
    if os.name == "posix":
        launch_options["start_new_session"] = True
    elif os.name == "nt":
        launch_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        process = subprocess.Popen(list(program), **launch_options)
    except (OSError, ValueError) as error:
        manifest.update(status="launch_failed", error=str(error), wall_seconds=time.monotonic() - started)
        _write_json(manifest_path, manifest)
        raise ValueError("cannot launch SDK-enabled program: " + str(error)) from error
    manifest["launcher_pid"] = process.pid
    manifest["process_group"].update(created=True, group_id=process.pid)
    while process.poll() is None:
        members = _group_members(process.pid)
        if members is not None:
            for member in members:
                observed[(member["pid"], member["start_ticks"])] = member
        time.sleep(0.02)
    code = process.wait()
    # Launcher scripts should normally wait for their own workers. Give orderly
    # worker shutdown a bounded grace, then preserve a partial capture without
    # killing any remaining business process or claiming a clean tree exit.
    deadline = time.monotonic() + CHILD_EXIT_GRACE_SECONDS
    live_members = _group_members(process.pid)
    while live_members and time.monotonic() < deadline:
        for member in live_members:
            observed[(member["pid"], member["start_ticks"])] = member
        time.sleep(0.02)
        live_members = _group_members(process.pid)
    membership_verified = live_members is not None
    manifest["process_group"].update(membership_verified=membership_verified,
                                      observed_members=list(observed.values()),
                                      live_members_at_snapshot=live_members,
                                      all_observed_members_exited=not live_members if membership_verified else None)
    if not membership_verified:
        manifest["warnings"].append("process group membership cannot be verified on this platform; descendant completeness is unknown")
    if live_members:
        manifest["warnings"].append("business children remain alive after launcher exit; evidence is a partial snapshot and business was not terminated")
    manifest.update(status="reading", launcher_exit_code=code, exit_code=code,
                    wall_seconds=time.monotonic() - started)
    sessions, raw_contents = [], {}
    for raw in sorted(raw_dir.glob("*.lxc" if compact else "*.jsonl")):
        item = {"path": str(raw), "filename": raw.name}
        try:
            if compact:
                from .compact import read_chunks, MAX_IMPORT_RECORDS, MAX_IMPORT_BYTES
                # Reject oversized normalization before making raw/base64 copies.
                # The original capture is retained for bounded inspect/select.
                read_chunks(raw, max_records=MAX_IMPORT_RECORDS, max_bytes=MAX_IMPORT_BYTES)
            before = raw.stat()
            content = raw.read_bytes()
            after = raw.stat()
            item.update(size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest(),
                        changed_during_read=(before.st_size, before.st_mtime_ns) !=
                        (after.st_size, after.st_mtime_ns))
            text = base64.b64encode(content).decode('ascii') if compact else content.decode("utf-8")
            raw_contents["raw/software/" + raw.name + ('.base64' if compact else '')] = text
            frozen = snapshot_dir / raw.name
            frozen.write_bytes(content)
            item["snapshot_path"] = str(frozen)
            if compact:
                from .compact import load_compact
                with frozen.open('rb') as compact_file:
                    compact_header = compact_file.read(32)
                    import struct
                    meta_size = struct.unpack_from('<I', compact_header, 24)[0]
                    meta = json.loads(compact_file.read(meta_size).decode('utf-8'))
                old = load_compact(frozen)
                meta.update({key: old[key] for key in ('host_id', 'boot_id', 'run_id') if old.get(key)})
            else:
                meta = _read_meta(text)
                old = load_sdk(frozen)
            if "%p" in raw.name:
                raise ValueError("SDK did not expand %p; rebuild the SDK with per-process output template support")
            if meta.get("run_id") not in (None, "", run_id):
                raise ValueError("SDK run_id does not match this capture")
            for identity in ("host_id", "boot_id"):
                observed, expected = meta.get(identity), snapshot.get(identity)
                if identity == 'host_id' and os.name == 'nt' and isinstance(observed, str) and isinstance(expected, str):
                    observed, expected = observed.casefold(), expected.casefold()
                if observed and expected and observed != expected:
                    raise ValueError("SDK %s does not match the locally launched host" % identity)
            old.update(run_id=run_id,
                       host_id=meta.get("host_id") or snapshot.get("host_id"),
                       boot_id=meta.get("boot_id") or snapshot.get("boot_id"),
                       capability_manifest=snapshot.get("capability_manifest", []))
            old["capture"] = {"kind": "software_sdk", "run_id": run_id,
                              "command": list(program), "launcher_pid": process.pid,
                              "launcher_exit_code": code, "exit_code": code, "pid": meta.get("pid"),
                              "raw_file": raw.name, "capture_manifest": str(manifest_path),
                              "host_identity_source": "sdk_meta" if meta.get("host_id") else "local_launch_doctor",
                              "boot_identity_source": "sdk_meta" if meta.get("boot_id") else "local_launch_doctor"}
            old.setdefault("raw_files", {})["capture/" + raw.name + ('.base64' if compact else '')] = text
            old["quality"].setdefault("warnings", [])
            if code or live_members or item["changed_during_read"] or not membership_verified:
                old["quality"]["clean_shutdown"] = False
                old["quality"]["source_continuity"] = "unknown"
            old["quality"]["process_exit_code"] = code
            old["quality"]["warnings"].extend(manifest["warnings"])
            if item["changed_during_read"]:
                old["quality"]["warnings"].append("SDK file changed while reading; immutable snapshot may be partial")
            parsed = capture_dir / "parsed" / (raw.stem + ".json")
            _write_json(parsed, old)
            sessions.append(parsed)
            item.update(status="imported", pid=meta.get("pid"),
                        clean_shutdown=old["quality"].get("clean_shutdown"),
                        dropped_records=old["quality"].get("dropped_records", 0),
                        incomplete_events=old["quality"].get("incomplete_events", 0),
                        protocol_errors=old["quality"].get("protocol_errors", 0))
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
            item.update(status="rejected", error=str(error))
            manifest["warnings"].append("rejected SDK output " + raw.name + ": " + str(error))
        manifest["raw_files"].append(item)
    manifest["imported_process_count"] = len(sessions)
    if not sessions:
        manifest.update(status="no_usable_sdk_output", clean_shutdown=False)
        _write_json(manifest_path, manifest)
        raise ValueError("SDK produced no usable per-process output; business_exit=%s; capture manifest=%s" % (code, manifest_path))
    try:
        from .report import merge_sessions
        merged = merge_sessions(sessions, run_id=run_id)
        merged["data_origin"] = "live"
        partial = (bool(code) or bool(live_members) or not membership_verified or
                   any(item["status"] != "imported" or item.get("changed_during_read")
                       or not item.get("clean_shutdown") or item.get("dropped_records")
                       or item.get("incomplete_events") or item.get("protocol_errors")
                       for item in manifest["raw_files"]))
        manifest.update(status="partial" if partial else "complete", clean_shutdown=not partial,
                        wall_seconds=time.monotonic() - started)
        merged["capture"] = dict(manifest)
        merged.setdefault("quality", {})["clean_shutdown"] = not partial
        merged["quality"]["process_exit_code"] = code
        merged["quality"]["capture_partial"] = partial
        merged["quality"].setdefault("warnings", []).extend(manifest["warnings"])
        merged.setdefault("raw_files", {}).update(raw_contents)
        merged["raw_files"]["raw/software/capture-manifest.json"] = json.dumps(
            manifest, ensure_ascii=False, indent=2) + "\n"
        merged.setdefault("provenance", []).append({"kind": "software_capture",
                                                     "run_id": run_id, "manifest": str(manifest_path),
                                                     "process_count": len(sessions)})
        _write_json(manifest_path, manifest)
        # Recheck immediately before publishing; neither pre-existing target is
        # overwritten if another capture finished while this workload ran.
        if out.exists() or report_path.exists():
            raise ValueError("output or HTML appeared during capture; evidence retained in " + str(capture_dir))
        write_session(merged, out)
        write_view(merged, report_path, view)
    except Exception as error:
        manifest.update(status="report_failed", error=str(error))
        _write_json(manifest_path, manifest)
        raise
    return merged, code
