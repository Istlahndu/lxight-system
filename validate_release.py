#!/usr/bin/env python3
"""Validate code and synthetic behavior separately from hardware acceptance."""
import json
import platform
import subprocess
import sys
import time
from pathlib import Path
from lxtrace.core import write_json
from lxtrace.resource import demo_session, analyze_resources, load_session
from lxtrace.workspace import workspace_environment

ROOT=Path(__file__).resolve().parent


def main():
    out=ROOT/'artifacts/v2';out.mkdir(parents=True,exist_ok=True)
    before=time.perf_counter()
    test=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-v'],cwd=str(ROOT),
                        capture_output=True,text=True,encoding='utf-8',env=workspace_environment(dict(PYTHONIOENCODING='utf-8')))
    (out/'tests.log').write_text(test.stdout+test.stderr,encoding='utf-8')
    if test.returncode:
        print(test.stdout+test.stderr); return test.returncode
    build=subprocess.run([sys.executable,'demo_v2.py'],cwd=str(ROOT),capture_output=True,text=True,
                         encoding='utf-8',env=workspace_environment(dict(PYTHONIOENCODING='utf-8')))
    if build.returncode:
        print(build.stdout+build.stderr);return build.returncode
    for case in ('resource','parallel','serial','loss','aggregate'):
        s=load_session(out/(case+'.lxtrace'))
        analyze_resources(s)
    parallel=analyze_resources(demo_session('parallel'))
    serial=analyze_resources(demo_session('serial'))
    assert parallel['target_cpu_ns']==serial['target_cpu_ns']==8000000
    assert (parallel['target_wall_ns'],parallel['peak_target_cpus'])==(4000000,2)
    assert (serial['target_wall_ns'],serial['peak_target_cpus'])==(8000000,1)
    page=(out/'index.html').read_text(encoding='utf-8')
    assert '<script src=' not in page and '<link rel="stylesheet"' not in page
    contract=json.loads((ROOT/'support-contract.v2.json').read_text(encoding='utf-8'))
    assert contract['H1']['status']=='blocked' and contract['H2']['status']=='blocked'
    result=dict(version='0.2.0-demo',code_tests='pass',synthetic_math='pass',offline_assets='inline',
                elapsed_seconds=time.perf_counter()-before,platform=platform.platform(),python=sys.version,
                H1='blocked',H2='blocked',interpretation='Code and fixture tests are not LX2 hardware acceptance; see server validation and support contract.')
    write_json(result,out/'release-validation.json')
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
