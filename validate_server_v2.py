#!/usr/bin/env python3
"""A bounded fresh validation on our own scheduler-launched workload only."""
import datetime
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from lxtrace.collector import doctor, record_external, CollectorBlocked
from lxtrace.resource import analyze_resources, write_report, load_session
from lxtrace.workspace import workspace_environment
from lxtrace.core import write_json

ROOT = Path(__file__).resolve().parent


def main():
    # Project-owned output only. A second run gets a separate directory.
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = ROOT/'artifacts'/'v2'/('server-'+stamp)
    out.mkdir(parents=True,exist_ok=False)
    os.environ.update(workspace_environment())
    report = dict(version='0.2.0-demo',host=platform.node(),platform=platform.platform(),
                  python=sys.version,created_at=stamp,output=str(out),tests={},profiles={},
                  H1='blocked',H2='blocked',scope='own_launch_only; no attach, no other jobs')
    snap = doctor(probe=True)
    write_json(snap,out/'capability.json')
    command = [sys.executable,str(ROOT/'examples/plain_workload.py')]
    marker = out/'must-not-run.txt'
    guarded = [sys.executable,'-c','from pathlib import Path; Path('+repr(str(marker))+').write_text("ran")']
    try:
        record_external(out/'resource.lxtrace',guarded,profile='resource')
        report['profiles']['resource'] = dict(status='unexpected_success')
    except CollectorBlocked as e:
        report['profiles']['resource'] = dict(status='blocked',reason=str(e),workload_not_started=not marker.exists())
    # One smoke comparison, not an overhead or inference-engine acceptance test.
    baseline = subprocess.run(command,cwd=str(ROOT),capture_output=True,text=True,timeout=20,env=workspace_environment())
    (out/'business-baseline.log').write_text(baseline.stdout+baseline.stderr,encoding='utf-8')
    baseline_checksum = re.findall(r'checksum=[0-9.]+',baseline.stdout)
    report['baseline'] = dict(exit_code=baseline.returncode, checksum=baseline_checksum)
    for profile in ('proc-sample','task-pmu'):
        try:
            capture = subprocess.run([sys.executable,'-m','lxtrace','record','--profile',profile,
                    '--output',str(out/(profile+'.lxtrace')),'--']+command,
                    cwd=str(ROOT),capture_output=True,text=True,timeout=30,env=workspace_environment())
            (out/(profile+'.business.log')).write_text(capture.stdout+capture.stderr,encoding='utf-8')
            if capture.returncode:
                raise ValueError(capture.stderr.strip() or 'capture failed')
            session = load_session(out/(profile+'.lxtrace'))
            code = session.get('capture',{}).get('exit_code')
            write_report(session,out/(profile+'.html'))
            analysis = analyze_resources(session)
            write_json(analysis,out/(profile+'.analysis.json'))
            report['profiles'][profile] = dict(status='captured',business_exit_code=code,
                    record_count=len(session['records']),representations=sorted({r['representation'] for r in session['records']}),
                    no_residency_invented=not any(r.get('category')=='schedule' for r in session['records']),
                    session=str(out/(profile+'.lxtrace')),data_origin=session['data_origin'],
                    checksum_matches_baseline=bool(baseline_checksum) and re.findall(r'checksum=[0-9.]+',capture.stdout)==baseline_checksum)
        except (CollectorBlocked,OSError,ValueError) as e:
            report['profiles'][profile] = dict(status='blocked',reason=str(e))
    result = subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_*v2.py','-v'],
                            cwd=str(ROOT),capture_output=True,text=True,timeout=60,env=workspace_environment())
    (out/'tests.log').write_text(result.stdout+result.stderr,encoding='utf-8')
    report['tests'] = dict(exit_code=result.returncode,scope='v2 math/import/collector contracts; not hardware H1/H2 acceptance')
    limited_profiles_pass = all(
        profile.get('status') == 'captured'
        and profile.get('business_exit_code') == 0
        and profile.get('data_origin') == 'live'
        and profile.get('record_count', 0) > 0
        and profile.get('no_residency_invented') is True
        and profile.get('checksum_matches_baseline') is True
        for profile in (report['profiles'].get(name, {}) for name in ('proc-sample', 'task-pmu'))
    )
    strict_profile = report['profiles'].get('resource', {})
    smoke_pass = (baseline.returncode == 0 and bool(baseline_checksum)
                  and result.returncode == 0 and limited_profiles_pass
                  and strict_profile.get('status') == 'blocked'
                  and strict_profile.get('workload_not_started') is True)
    report['smoke_status'] = 'pass' if smoke_pass else 'fail'
    write_json(report,out/'validation.json')
    write_json(dict(directory=str(out)),ROOT/'artifacts/v2/latest-server.json')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if smoke_pass else 1


if __name__ == '__main__': raise SystemExit(main())
