import argparse
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from .core import ROOT, analyze, doctor, export_trace, finalize_session, sha256, synthetic, write_csv, write_json, write_report
from .workspace import workspace_environment


def timestamp(text):
    import re
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ns|us|ms|s)?", str(text))
    if not match:
        raise argparse.ArgumentTypeError("use e.g. 2ms, 100us, or integer ns")
    return int(float(match[1]) * {None: 1, "ns": 1, "us": 1000, "ms": 1000000, "s": 1000000000}[match[2]])


def load(path):
    return finalize_session(json.loads(Path(path).read_text(encoding="utf-8-sig")))


def main(argv=None):
    parser = argparse.ArgumentParser(description="LX2 Trace D0 · software semantic timelines; no privileged collector")
    parser.add_argument("--version", action="version", version="0.1.0-d0")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("doctor", help="read-only capability snapshot; does not execute perf")
    p.add_argument("--output", default="capability.json")
    p = commands.add_parser("record", help="launch only the explicitly supplied program with SDK env")
    p.add_argument("--profile", choices=["basic", "schedule"], default="basic")
    p.add_argument("--duration", type=timestamp)
    p.add_argument("--output", required=True)
    p.add_argument("program", nargs=argparse.REMAINDER)
    p = commands.add_parser("import", help="SDK JSONL / GFTRACE1 software, or native v2 GFTRACE2 events and PMU")
    p.add_argument("--type", choices=["sdk", "gftrace"], required=True)
    p.add_argument("--input", required=True)
    p.add_argument("--metadata")
    p.add_argument("--mapping")
    p.add_argument("--output", required=True)
    p = commands.add_parser("analyze")
    p.add_argument("session")
    p.add_argument("--from", dest="start", type=timestamp, default=0)
    p.add_argument("--to", dest="end", type=timestamp)
    p.add_argument("--thread", action="append", default=[])
    p.add_argument("--frame", action="append", default=[], help="explicit frame event ID or application frame label")
    p.add_argument("--operator", action="append", default=[], help="explicit operator ID or instance ID")
    p.add_argument("--cpu", type=int, help="D0 has no CPU-residency query; fails explicitly")
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--output", required=True)
    p = commands.add_parser("export")
    p.add_argument("session")
    p.add_argument("--format", choices=["html", "chrome-trace"], required=True)
    p.add_argument("--output", required=True)
    p = commands.add_parser("synthetic")
    p.add_argument("--count", type=int, default=100000)
    p.add_argument("--output", required=True)
    p = commands.add_parser("serve", help="loopback-only static viewer; use an SSH tunnel from workstation")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--directory", default="artifacts")
    p = commands.add_parser("bundle-viewer")
    p.add_argument("session")
    p.add_argument("--output", required=True, help="directory")
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            write_json(doctor(), args.output)
        elif args.command == "synthetic":
            before = time.perf_counter()
            write_json(synthetic(args.count), args.output)
            print("synthetic events=%d elapsed_s=%.3f" % (args.count, time.perf_counter()-before))
        elif args.command == "record":
            if args.profile == "schedule":
                raise ValueError("schedule unavailable in D0: no continuous scheduler collector. Select basic explicitly.")
            program = args.program[1:] if args.program[:1] == ["--"] else args.program
            if not program:
                raise ValueError("record requires -- <explicit SDK-enabled executable> [args]")
            out = Path(args.output).resolve()
            out.parent.mkdir(parents=True, exist_ok=True)
            raw = out.with_suffix(out.suffix + ".raw.jsonl")
            if out.exists() or raw.exists():
                raise ValueError("output already exists; choose a new session filename")
            env = workspace_environment(dict(LXTRACE_OUTPUT=str(raw), LXTRACE_DISABLED="0"))
            before = time.perf_counter()
            timeout = args.duration / 1e9 if args.duration else None
            timed_out = False
            try:
                result = subprocess.run(program, env=env, timeout=timeout)
                code = result.returncode
            except subprocess.TimeoutExpired:
                timed_out, code = True, 124
            if not raw.exists():
                raise ValueError("SDK produced no output. Program may lack SDK, or stopped before flush; business_exit=" + str(code))
            from .importers import load_sdk
            session = load_sdk(raw)
            snapshot = doctor()
            for key in ("capability_manifest", "topology", "authorized_uid", "host_id", "boot_id"):
                session[key] = snapshot[key]
            session["capture"] = dict(command=program, exit_code=code, timed_out=timed_out,
                                      wall_seconds=time.perf_counter()-before, cwd=str(Path.cwd()))
            if code:
                session.setdefault("quality", {})["process_exit_code"] = code
                session["quality"]["clean_shutdown"] = False
            executable = Path(program[0])
            if executable.is_file():
                session.setdefault("app", {})["executable_sha256"] = sha256(executable)
            write_json(finalize_session(session), out)
            print("session=" + str(out) + " business_exit=" + str(code))
            return code if code >= 0 else 1
        elif args.command == "import":
            from .importers import load_sdk, load_gftrace
            s = load_sdk(args.input) if args.type == "sdk" else load_gftrace(args.input, args.metadata, args.mapping)
            if s.get("schema_version") == "lxtrace.resource-session.v2":
                from .resource import write_session
                write_session(s, args.output)
            else:
                write_json(finalize_session(s), args.output)
        elif args.command == "analyze":
            if args.cpu is not None:
                raise ValueError("CPU-residency analysis unavailable in D0. CPU point values/affinity are not intervals.")
            session = load(args.session)
            result = analyze(session, args.start, args.end if args.end is not None else session["time_range"][1],
                             {"thread_ids": args.thread, "frame_ids": args.frame, "operator_ids": args.operator})
            result["session_id"] = session["session_id"]
            result["data_origin"] = session["data_origin"]
            result["capability_manifest"] = session["capability_manifest"]
            result["provenance"] = session["provenance"]
            result["clock"] = session["clock"]
            result["quality"] = session["quality"]
            result["sources"] = session["sources"]
            if args.format == "csv":
                write_csv(result, args.output)
                write_json({k: v for k, v in result.items() if k != "events"}, args.output + ".metadata.json")
            else:
                write_json(result, args.output)
        elif args.command == "export":
            session = load(args.session)
            if args.format == "html":
                write_report(session, args.output)
            else:
                write_json(export_trace(session), args.output)
        elif args.command == "bundle-viewer":
            out = Path(args.output)
            out.mkdir(parents=True, exist_ok=True)
            for f in (ROOT / "legacy_web").iterdir():
                if f.is_file():
                    shutil.copy2(str(f), str(out / f.name))
            write_json(load(args.session), out / "demo-session.json")
        elif args.command == "serve":
            directory = Path(args.directory).resolve()
            if not directory.is_dir():
                raise ValueError("serve directory does not exist")
            handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
            server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler)
            print("LX2 Trace: http://127.0.0.1:%d/ (Ctrl+C to stop)" % args.port, flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        print("wrote " + str(getattr(args, "output", "")))
        return 0
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print("lxtrace: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
