#!/usr/bin/env python3
"""Build formal HTML guides from Markdown and verify local links."""
import argparse
import html
import os
import re
import shutil
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

try:
    import markdown
except ImportError as error:
    raise SystemExit("Documentation build requires: python -m pip install -r requirements-docs.txt") from error

ROOT = Path(__file__).resolve().parents[1]
STYLE = '''
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#eef3f6;color:#203647;font:16px/1.85 "Segoe UI","Microsoft YaHei",sans-serif}main{max-width:1140px;margin:28px auto;background:#fff;border-top:5px solid #087f8c;padding:30px 46px 48px;box-shadow:0 3px 18px #18364710}h1{font-size:30px;line-height:1.4;margin:28px 0 20px}h2{font-size:23px;margin-top:40px;padding-top:8px;border-top:1px solid #e1e8ec}h3{font-size:18px;margin-top:28px}a{color:#087887;text-underline-offset:3px}a:hover{color:#03505b}p{margin:15px 0}code,pre{font:13px/1.7 Consolas,"Microsoft YaHei",monospace}code{background:#edf3f5;padding:2px 5px;border-radius:3px;overflow-wrap:anywhere}pre{background:#173446;color:#e5f0f4;padding:18px 20px;border-radius:5px;overflow:auto;tab-size:4}pre code{background:none;padding:0;color:inherit;overflow-wrap:normal}.table-wrap{overflow:auto;margin:20px 0}table{width:100%;border-collapse:collapse;font-size:14px;line-height:1.75}th,td{border:1px solid #d5e1e7;padding:10px 12px;text-align:left;vertical-align:top}th{background:#e8f2f4;color:#16586a}tbody tr:nth-child(even){background:#f7fafb}td:first-child{min-width:105px}nav{display:flex;gap:10px;flex-wrap:wrap}nav a{background:#e8f3f5;border:1px solid #c8e0e5;padding:5px 13px;border-radius:4px;text-decoration:none;font-size:14px}nav a:first-child{background:#087f8c;color:#fff;border-color:#087f8c}.toc-panel{margin-top:24px;background:#f4f8fa;border:1px solid #dce6ec;padding:10px 18px;border-radius:5px}.toc-panel summary{cursor:pointer;font-weight:600}.toc-panel ul{margin:8px 0;padding-left:22px}.toc-panel li{font-size:14px;margin:2px 0}.toc-panel ul ul{display:none}.footer{border-top:1px solid #dce6ec;margin-top:36px;padding-top:14px;font-size:13px;color:#587080}h1,h2,h3{scroll-margin-top:20px}@media(max-width:700px){body{font-size:15px}main{margin:0;padding:20px 18px 32px}h1{font-size:25px}h2{font-size:21px}pre{padding:14px}th,td{padding:8px;min-width:140px}nav{gap:6px}}@media print{body{background:#fff}main{box-shadow:none;margin:0;max-width:none;padding:10px}nav,.toc-panel{display:none}pre{white-space:pre-wrap;background:#f1f4f6;color:#203647}a{color:inherit}table{font-size:11px}.table-wrap{overflow:visible}}
'''

SPECS = (
    ('README.md', 'README.html', 'usage.html', '灵晟 LX2 性能分析器 · 使用指南'),
    ('docs/sdk-quickstart.md', 'docs/sdk-quickstart.html', 'sdk-quickstart.html', 'LX2 性能分析器 · C/C++ SDK 接入指南'),
    ('docs/scheduler-interchange.md', 'docs/scheduler-interchange.html', 'scheduler-interchange.html', 'LX2 性能分析器 · 调度流导入格式'),
    ('docs/production-attribution.md', 'docs/production-attribution.html', 'production-attribution.html', 'LX2 性能分析器 · 生产归因报告'),
)


def links_for(source, served):
    if served:
        return [('usage.html', '使用说明'), ('sdk-quickstart.html', 'SDK 接入'), ('scheduler-interchange.html', '调度数据格式'), ('production-attribution.html', '生产归因')]
    prefix = '../' if source.startswith('docs/') else ''
    docs = '' if prefix else 'docs/'
    return [(prefix + 'README.html', '使用说明'), (docs + 'sdk-quickstart.html', 'SDK 接入'), (docs + 'scheduler-interchange.html', '调度数据格式'), (docs + 'production-attribution.html', '生产归因')]


def rewrite_href(match, source, target, served):
    url = html.unescape(match.group(1))
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith('/'):
        return match.group(0)
    resolved = (ROOT / source).parent / unquote(parsed.path)
    resolved = resolved.resolve()
    outputs = {ROOT / spec[0]: ROOT / ('artifacts/v2/' + spec[2] if served else spec[1]) for spec in SPECS}
    destination = outputs.get(resolved, resolved)
    if served and resolved == ROOT / 'CONTRACT.txt':
        destination = ROOT / 'artifacts/v2/CONTRACT.txt'
    href = Path(os.path.relpath(destination, target.parent)).as_posix()
    href = urlunsplit(('', '', href, parsed.query, parsed.fragment))
    return 'href="' + html.escape(href, quote=True) + '"'


def render(source, target, title, served):
    md = markdown.Markdown(extensions=['tables', 'fenced_code', 'toc'], extension_configs={'toc': {'toc_depth': '2-2', 'permalink': False}})
    body = md.convert((ROOT / source).read_text(encoding='utf-8-sig'))
    body = re.sub(r'href="([^"]+)"', lambda match: rewrite_href(match, source, target, served), body)
    body = re.sub(r'(<table>.*?</table>)', r'<div class="table-wrap">\1</div>', body, flags=re.S)
    nav = ''.join('<a href="' + html.escape(url, quote=True) + '">' + html.escape(label) + '</a>' for url, label in links_for(source, served))
    toc = '<details class="toc-panel"><summary>本页目录</summary>' + md.toc + '</details>' if md.toc_tokens else ''
    page = '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + html.escape(title) + '</title><style>' + STYLE + '</style></head><body><main><nav aria-label="文档导航">' + nav + '</nav>' + toc + body + '<p class="footer">灵晟 LX2 性能分析器 · 0.2.0-demo · 使用文档</p></main></body></html>\n'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding='utf-8')


def verify(outputs):
    errors = []
    for path in outputs:
        content = path.read_text(encoding='utf-8')
        for url in re.findall(r'href="([^"]+)"', content):
            parsed = urlsplit(html.unescape(url))
            if parsed.scheme or parsed.netloc:
                continue
            target = path.parent / unquote(parsed.path) if parsed.path else path
            if not target.exists():
                errors.append(f'{path.relative_to(ROOT)}: missing {url}')
            elif parsed.fragment and target.suffix == '.html':
                target_html = target.read_text(encoding='utf-8')
                fragment = re.escape(html.escape(unquote(parsed.fragment), quote=True))
                if not re.search(r'id=["\']' + fragment + r'["\']', target_html):
                    errors.append(f'{path.relative_to(ROOT)}: missing fragment {url}')
        if re.search(r'<(?:script|link)\b[^>]*(?:src|href)=["\']https?://', content, flags=re.I):
            errors.append(f'{path.relative_to(ROOT)}: remote asset dependency')
    if errors:
        raise SystemExit('\n'.join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--served', action='store_true', help='Also generate artifacts/v2 HTML entry points and their support contracts')
    args = parser.parse_args()
    outputs = []
    for source, formal, served, title in SPECS:
        target = ROOT / formal
        render(source, target, title, False)
        outputs.append(target)
        if args.served:
            target = ROOT / 'artifacts/v2' / served
            render(source, target, title, True)
            outputs.append(target)
    if args.served:
        for name in ('CONTRACT.txt', 'support-contract.v2.json'):
            shutil.copyfile(ROOT / name, ROOT / 'artifacts/v2' / name)
    verify(outputs)
    print(f'Generated {len(outputs)} HTML guides; all local links and fragments resolve; no remote runtime assets.')


if __name__ == '__main__':
    main()
