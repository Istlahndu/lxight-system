#!/usr/bin/env python3
"""Package distributable source without local captures or validation outputs."""
import datetime
import tarfile
import zipfile
from pathlib import Path
from lxtrace.core import sha256, write_json

ROOT = Path(__file__).resolve().parent
ROOT_FILES = (
    '.gitignore', '.gitattributes', 'README.md', 'README.html', 'CONTRACT.txt',
    'support-contract.v2.json', 'requirements-docs.txt', 'start.sh',
    'package_release.py', 'run_demo.py', 'cpu_lesson.py', 'pipeline_lesson.py',
    'mlp_demo.py', 'demo_v2.py', 'validate_release.py', 'validate_server_v2.py',
    'validate_legacy_release.py',
)
SOURCE_DIRECTORIES = (
    'lxtrace', 'sdk', 'examples', 'web', 'legacy_web', 'fixtures', 'tests',
    'docs', 'configs', 'tools',
)
SOURCE_SUFFIXES = {
    '.py', '.md', '.rst', '.html', '.txt', '.sh', '.json', '.js', '.css',
    '.c', '.cc', '.cpp', '.h', '.hpp', '.svg',
}
EXCLUDED_PARTS = {
    'artifacts', 'local-docs', 'local-tools', 'reproduction', '__pycache__',
    '.git', '.venv', 'venv', 'node_modules', 'build', 'dist', '.pytest_cache',
    '.mypy_cache',
}


def source_files():
    """Select source explicitly; generated evidence is never a package input."""
    files = [ROOT / name for name in ROOT_FILES if (ROOT / name).is_file()]
    for name in SOURCE_DIRECTORIES:
        directory = ROOT / name
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in directory.rglob('*'):
            relative = path.relative_to(ROOT)
            if any(part.lower() in EXCLUDED_PARTS for part in relative.parts):
                continue
            if path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            if path.is_file() and not path.is_symlink():
                files.append(path)
    # This small synthetic format fixture is source input, not an executable.
    fixture = ROOT / 'fixtures/gftrace-demo.bin'
    if fixture.is_file():
        files.append(fixture)
    return sorted({
        path for path in files
        if not path.is_symlink()
        and not any(parent.is_symlink() for parent in path.parents if parent != ROOT)
        and path.resolve().is_relative_to(ROOT.resolve())
    })


def main():
    files = source_files()
    manifest={p.relative_to(ROOT).as_posix():sha256(p) for p in files}
    created=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
    output = ROOT / 'artifacts/releases'
    output.mkdir(parents=True, exist_ok=True)
    checksum_path = output / 'SHA256SUMS.json'
    write_json(dict(version='0.2.0-demo',created_at=created,files=manifest), checksum_path)
    zip_path=output/'lx2-profiler-demo-v0.2.0.zip'
    tar_path=output/'lx2-profiler-demo-v0.2.0.tar.gz'
    with zipfile.ZipFile(zip_path,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in files: z.write(p,str(Path('lx2-profiler-demo-v2')/p.relative_to(ROOT)))
        z.write(checksum_path, 'lx2-profiler-demo-v2/SHA256SUMS.json')
    with tarfile.open(tar_path,'w:gz') as t:
        for p in files: t.add(p,arcname=p.relative_to(ROOT).as_posix(),recursive=False)
        t.add(checksum_path, arcname='SHA256SUMS.json', recursive=False)
    result=dict(version='0.2.0-demo',created_at=created,files=len(files)+1,
                zip=str(zip_path),zip_sha256=sha256(zip_path),tar=str(tar_path),tar_sha256=sha256(tar_path))
    write_json(result,output/'lx2-profiler-demo-v0.2.0.release.json')
    print(result)


if __name__=='__main__': main()
