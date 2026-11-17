"""Resource-first CLI, with explicit legacy software compatibility."""
import argparse
import csv
import functools
import http.server
import json
import re
import shutil
import sys
from pathlib import Path
from .core import ROOT, write_json
from .legacy_cli import timestamp

VERSION = '0.2.0-demo'


def is_v1(path):
    p = Path(path)
    return p.is_file() and json.loads(p.read_text(encoding='utf-8-sig')).get('schema_version') == 'lxtrace.session.v1'


def serve(directory, port):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(Path(directory).resolve()))
    with http.server.ThreadingHTTPServer(('127.0.0.1', port), handler) as server:
        print('LX2 Profiler v2: http://127.0.0.1:%d/ (Ctrl+C to stop)' % server.server_port, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


def export_trace(s):
    from .resource import _counter_window
    events = []
    for r in s['records']:
        base = dict(name=r.get('name', r['category']), cat=r['layer']+'.'+r['category'],
                    pid=r['resource_id'], tid=r.get('source_id', 'unknown'), args=r)
        if r['representation'] == 'interval' and r.get('end_ns') is not None:
            if r['layer'] == 'software' and r.get('kind') == 'request':
                # Async request lifetimes may cross synchronous compute scopes.
                # Complete X slices on one thread would imply a nested stack.
                request = dict(base, id='lxtrace-request:'+r['record_id'], scope='lxtrace.request')
                events.append(dict(request, ph='b', ts=r['begin_ns']/1000))
                events.append(dict(request, ph='e', ts=r['end_ns']/1000))
            else:
                events.append(dict(base, ph='X', ts=r['begin_ns']/1000, dur=(r['end_ns']-r['begin_ns'])/1000))
        elif r['representation'] == 'interval':
            events.append(dict(base, ph='i', s='t', ts=r['begin_ns']/1000,
                               name=base['name']+' · incomplete'))
        elif r['representation'] == 'counter_window':
            counter = _counter_window(r, *s['time_range'])
            base['args'] = dict(r, selection_value=counter['selection_value'],
                                selection_status=counter['selection_status'],
                                counter_status=counter['counter_status'],
                                included_in_totals=counter['included_in_totals'],
                                semantics='point annotates counter read window; not activity begin/end')
            events.append(dict(base, ph='i', s='t', ts=r['begin_ns']/1000,
                               name=base['name']+' · 计数窗'))
        elif r['representation'] == 'sample':
            events.append(dict(base, ph='i', s='t', ts=r.get('timestamp_ns', r.get('begin_ns', 0))/1000))
    events.sort(key=lambda event: event['ts'])
    return dict(traceEvents=events, displayTimeUnit='ms', lxtrace_native=s,
                format_losses=['Trace Event does not preserve native resource graph, windows, null ends or uncertainty. PMU windows are annotations, never hardware activity. Async request lifetimes run from submit to completion observed, not physical transfer activity. Use lxtrace_native for analysis.'])


def export_csv(result, path):
    rows = result.get('per_cpu', [])
    keys = sorted({k for row in rows for k in row if not isinstance(row[k], (dict, list))})
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for row in rows:
            w.writerow({k: _csv_value(row.get(k)) for k in keys})
    write_json(result, str(path)+'.metadata.json')


def _csv_value(value):
    """Keep untrusted text literal in spreadsheets; leave numeric cells numeric."""
    if isinstance(value, str) and (re.match(r'^[\s\ufeff]*[=+\-@]', value)
                                  or value.startswith(('\t', '\r', '\n'))):
        return "'" + value
    return value


def make_parser():
    p = argparse.ArgumentParser(description='LX2 Profiler v2 · resource evidence / offline analysis')
    p.add_argument('--version', action='version', version=VERSION)
    c = p.add_subparsers(dest='command', required=True)
    q = c.add_parser('doctor', help='probe own-task PMU, topology and source access; no policy changes')
    q.add_argument('--job'); q.add_argument('--no-probe', action='store_true')
    q.add_argument('--output', default='artifacts/v2/capability.json')
    q = c.add_parser('record', help='capture a program and automatically write its unified HTML report')
    q.add_argument('--profile', choices=['resource','task-pmu','proc-sample','schedule','basic','compact'], default='resource')
    q.add_argument('--config', help='JSON with required_sources / optional_sources')
    q.add_argument('--job'); q.add_argument('--attach', type=int)
    q.add_argument('--duration', type=timestamp); q.add_argument('--output', required=True)
    q.add_argument('--html', help='HTML report path; defaults to the output stem plus .html')
    q.add_argument('program', nargs=argparse.REMAINDER)
    q = c.add_parser('demo', help='generate labeled synthetic resource fixtures')
    q.add_argument('--case', choices=['resource','parallel','serial','loss','aggregate','sparse','open','counters','uncalibrated'], default='resource')
    q.add_argument('--output', default='artifacts/v2/demo.lxtrace'); q.add_argument('--html')
    q = c.add_parser('analyze'); q.add_argument('session'); q.add_argument('--resource')
    q.add_argument('--from', dest='start', type=timestamp); q.add_argument('--to', dest='end', type=timestamp)
    q.add_argument('--thread', action='append', default=[]); q.add_argument('--frame', action='append', default=[])
    q.add_argument('--operator', action='append', default=[]); q.add_argument('--cpu', type=int)
    q.add_argument('--format', choices=['json','csv'], default='json'); q.add_argument('--output', required=True)
    q = c.add_parser('import', help='import SDK, GFTRACE1 software, GFTRACE2 events/PMU, or a native session')
    q.add_argument('--type', choices=['sdk','gftrace','v1','session','schedule'], required=True)
    q.add_argument('--input', required=True); q.add_argument('--metadata'); q.add_argument('--mapping')
    q.add_argument('--output', required=True)
    q.add_argument('--no-windows', action='store_true', help='GFTRACE2: omit derived PMU windows; keep cumulative readings and aggregates')
    q = c.add_parser('export'); q.add_argument('session')
    q.add_argument('--format', choices=['html','chrome-trace','json','csv'], required=True)
    q.add_argument('--output', required=True)
    q = c.add_parser('report', help='generate one unified pipeline HTML from a session or same-node rank sessions')
    q.add_argument('inputs', nargs='+', help='v1/v2 sessions, SDK JSONL, GFTRACE1/2 binary, or a directory containing sessions')
    q.add_argument('--output', required=True, help='self-contained HTML report')
    q.add_argument('--session-output', help='also save the merged native .lxtrace archive or JSON')
    q.add_argument('--run-id', help='explicit run grouping for older sessions without a shared run ID')
    q = c.add_parser('production-report', help='derive calibrated production attribution from separate timing/counting runs')
    source = q.add_mutually_exclusive_group(required=True)
    source.add_argument('--timing', help='ticks-only GFTRACE2 timing run; counting runs align by logical work, not timestamps')
    source.add_argument('--mfu-counts', help='standalone complete-window MFU summary JSON; not mixed with another trace configuration')
    q.add_argument('--count', action='append', default=[], metavar='RUN=PATH', help='GFTRACE2 counting run, repeat in event-priority order')
    q.add_argument('--hardware', help='versioned hardware profile name or JSON path')
    q.add_argument('--model', help='engine/model profile name or JSON path')
    q.add_argument('--strict-alignment', action='store_true', help='reject rate fallback when dynamic work changes a block/mode key')
    q.add_argument('--bin-ms', type=float, default=0.2, help='aggregate projection bin size in milliseconds (default: 0.2)')
    q.add_argument('--reference', help='optional external reference JSON, displayed separately and never used to compute results')
    q.add_argument('--output', required=True, help='self-contained production attribution HTML')
    q.add_argument('--analysis-output', help='analysis JSON (defaults to OUTPUT.analysis.json)')
    q.add_argument('--evidence-output', help='optional resource .lxtrace archive of calibrated aggregate evidence')
    q = c.add_parser('compact-info', help='stream-check an entire compact capture without building an in-memory timeline')
    q.add_argument('input'); q.add_argument('--output', help='summary JSON; default: stdout')
    q = c.add_parser('compact-select', help='copy complete selected compact thread instances, retaining source quality and provenance')
    q.add_argument('input'); q.add_argument('--thread', type=int, required=True, action='append')
    q.add_argument('--output', required=True, help='new compact file; existing files are never replaced')
    q = c.add_parser('bundle-viewer'); q.add_argument('session'); q.add_argument('--output', required=True)
    q = c.add_parser('serve'); q.add_argument('--port', type=int, default=8765)
    q.add_argument('--directory', default='artifacts/v2')
    q = c.add_parser('view'); q.add_argument('session'); q.add_argument('--output', default='artifacts/v2/view.html')
    q.add_argument('--no-open', action='store_true')
    q = c.add_parser('rebuild',help='rebuild derived/index into a new archive; verify all original evidence')
    q.add_argument('session'); q.add_argument('--output',required=True)
    q = c.add_parser('legacy', help='v1 software-only compatibility'); q.add_argument('arguments', nargs=argparse.REMAINDER)
    q = c.add_parser('synthetic', help='legacy v1 scale fixture; use demo for resource v2')
    q.add_argument('--count', type=int, default=100000); q.add_argument('--output', required=True)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = make_parser().parse_args(argv)
    try:
        from . import resource as R
        if args.command in ('legacy','synthetic'):
            from .legacy_cli import main as old_main
            return old_main(args.arguments if args.command == 'legacy' else argv)
        if args.command == 'doctor':
            from .collector import doctor
            write_json(doctor(args.job, not args.no_probe), args.output)
        elif args.command == 'record':
            program = args.program[1:] if args.program[:1] == ['--'] else args.program
            html = Path(args.html) if args.html else Path(args.output).with_suffix('.html')
            if (html.resolve() == Path(args.output).resolve()
                    or Path(args.output).resolve() in html.resolve().parents
                    or html.resolve() in Path(args.output).resolve().parents):
                raise ValueError('HTML report must be separate from the native session archive')
            if html.exists():
                raise ValueError('HTML report already exists; choose a new output path')
            if args.profile in ('basic', 'compact'):
                if args.attach is not None or args.config or args.job:
                    raise ValueError('basic launches an explicit SDK-enabled program; --attach/--config/--job are unsupported')
                from .software import record_software
                session, code = record_software(args.output, program, str(html), args.duration,
                                                **({'compact': True} if args.profile == 'compact' else {}))
            else:
                from .collector import record_external
                config = json.loads(Path(args.config).read_text(encoding='utf-8-sig')) if args.config else None
                session, code = record_external(args.output, program or None, args.attach, args.duration,
                                          'resource' if args.profile == 'schedule' else args.profile, config, args.job)
                R.write_report(session, html, overwrite=False)
            business_exit = session.get('capture',{}).get('exit_code')
            print('session='+str(args.output)+' html='+str(html)+' business_exit='+str(business_exit))
            return code if code >= 0 else 1
        elif args.command == 'report':
            from .report import write_unified_report
            session = write_unified_report(args.inputs, args.output, args.session_output, args.run_id)
            print('records='+str(len(session['records']))+' html='+str(args.output))
        elif args.command == 'production-report':
            from .attribution.workflow import run_report
            analysis = run_report(args)
            print('attribution='+str(analysis['schema_version'])+' html='+str(args.output))
        elif args.command == 'compact-info':
            from .compact_stream import inspect_compact
            if args.output and (Path(args.output).resolve() == Path(args.input).resolve() or Path(args.output).exists()):
                raise ValueError('compact summary must use a new path separate from the input capture')
            summary = inspect_compact(args.input)
            if args.output:
                write_json(summary, args.output)
                print('summary='+str(args.output)+' events='+str(summary['event_count']))
            else:
                print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        elif args.command == 'compact-select':
            from .compact_stream import select_compact
            summary = select_compact(args.input, args.output, args.thread)
            print('compact='+str(args.output)+' events='+str(summary['event_count'])+' threads='+str(summary['thread_count']))
            return 0
        elif args.command == 'demo':
            s = R.demo_session(args.case); R.write_session(s, args.output)
            if args.html: R.write_report(s, args.html)
        elif args.command == 'rebuild':
            s=R.load_session(args.session,allow_missing_derived=True)
            s.pop('archive',None)
            R.write_session(s,args.output)
        elif args.command == 'analyze':
            if is_v1(args.session) and not args.resource:
                from .legacy_cli import main as old_main
                return old_main(argv)
            s = R.load_session(args.session)
            if args.thread or args.frame or args.operator:
                raise ValueError('v2 resource analysis uses --resource; use legacy analyze for semantic filters')
            rid = args.resource
            if args.cpu is not None:
                rid = next((r['resource_id'] for r in s['resources'] if r.get('logical_cpu') == args.cpu), None)
                if rid is None: raise ValueError('CPU not in session topology')
            result = R.analyze_resources(s, args.start, args.end, rid)
            if args.format == 'csv': export_csv(result, args.output)
            else: write_json(result, args.output)
        elif args.command == 'import':
            if args.no_windows and args.type != 'gftrace':
                raise ValueError('--no-windows is a GFTRACE2 import option')
            if args.type == 'schedule':
                if not args.metadata: raise ValueError('schedule import requires --metadata with topology, clock, scope and stable instance IDs')
                from .schedule import import_schedule
                s = import_schedule(args.input,args.metadata)
            elif args.type in ('sdk','gftrace'):
                from .importers import load_sdk, load_gftrace
                imported = load_sdk(args.input) if args.type == 'sdk' else load_gftrace(args.input,args.metadata,args.mapping,windows=not args.no_windows)
                s = R.validate_session(imported) if imported.get('schema_version') == R.SCHEMA else R.migrate_v1(imported)
            else: s = R.load_session(args.input)
            R.write_session(s, args.output)
        elif args.command in ('export','view'):
            s = R.load_session(args.session); fmt = 'html' if args.command == 'view' else args.format
            if fmt == 'html': R.write_report(s,args.output)
            elif fmt == 'chrome-trace': write_json(export_trace(s),args.output)
            elif fmt == 'csv': export_csv(R.analyze_resources(s),args.output)
            else: R.write_session(s,args.output)
            if args.command == 'view' and not args.no_open:
                import webbrowser
                webbrowser.open(Path(args.output).resolve().as_uri())
        elif args.command == 'bundle-viewer':
            out=Path(args.output); out.mkdir(parents=True,exist_ok=True)
            for f in (ROOT/'web').iterdir():
                if f.is_file(): shutil.copy2(str(f),str(out/f.name))
            write_json(R.load_session(args.session),out/'demo-session.json')
        elif args.command == 'serve':
            if not Path(args.directory).is_dir(): raise ValueError('viewer not found; run python demo_v2.py first')
            serve(args.directory,args.port)
        print('wrote '+str(getattr(args,'output','')))
        return 0
    except (ValueError,OSError,KeyError,TypeError,json.JSONDecodeError) as exc:
        print('lxtrace: '+str(exc),file=sys.stderr); return 2


if __name__ == '__main__':
    raise SystemExit(main())
