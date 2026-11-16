#!/usr/bin/env python3
"""Build the offline v2 workbench and explicitly synthetic acceptance fixtures."""
import argparse
import json
import shutil
from pathlib import Path
from lxtrace.resource import demo_session, analyze_resources, write_session, write_report, load_session
from lxtrace.core import write_json

ROOT = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', default='artifacts/v2')
    args = p.parse_args()
    out = Path(args.output).resolve()
    out.relative_to(ROOT.resolve())
    out.mkdir(parents=True,exist_ok=True)
    viewer = out/'viewer'; viewer.mkdir(exist_ok=True)
    for f in (ROOT/'web').iterdir():
        if f.is_file(): shutil.copy2(str(f),str(viewer/f.name))
    results = {}
    for case in ('resource','parallel','serial','loss','aggregate'):
        s = demo_session(case)
        archive = out/(case+'.lxtrace')
        if not archive.exists():
            write_session(s,archive)
        else:
            saved = load_session(archive)
            saved.pop('archive',None)
            if not saved.get('raw_files'): saved.pop('raw_files',None)
            if saved != s:
                raise ValueError('fixture archive differs; choose a new --output directory: '+str(archive))
        write_json(s,viewer/(case+'.json'))
        write_report(s,out/(case+'.html'))
        results[case] = analyze_resources(s)
        write_json(results[case],out/(case+'.analysis.json'))
        if case == 'resource':
            write_json(s,viewer/'demo-session.json')
            write_report(s,out/'index.html')
    # Keep an actual old session available, with its original evidence layer.
    legacy = ROOT/'artifacts/demo.lxtrace'
    legacy_archive = out/'legacy-import.lxtrace'
    migrated = None
    if legacy_archive.exists():
        migrated = load_session(legacy_archive)
    elif legacy.is_file():
        migrated = load_session(legacy)
        write_session(migrated,legacy_archive)
    if migrated is not None:
        write_report(migrated,out/'legacy-import.html')
    write_json(dict(version='0.2.0-demo',fixture_origin='synthetic',results=results,
                    hardware_acceptance='not_claimed',H1='blocked_pending_real_sources_and_full_domain_validation',
                    H2='blocked_pending_verified_hardware_activity_and_transfer_sources'),out/'demo-validation.json')
    print(json.dumps(dict(viewer=str(viewer/'index.html'),offline=str(out/'index.html'),fixtures=len(results)),ensure_ascii=False))


if __name__ == '__main__': main()
