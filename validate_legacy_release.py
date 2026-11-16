#!/usr/bin/env python3
"""Freeze functional QA, artifact digests and the human-readable release report."""
import datetime
import html
import io
import json
import platform
import sys
import time
import unittest
from pathlib import Path
from lxtrace.core import sha256, write_json

ROOT = Path(__file__).resolve().parent


def main():
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    start = time.perf_counter()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    elapsed = time.perf_counter() - start
    out = ROOT / "artifacts"
    out.mkdir(exist_ok=True)
    (out / "qa-tests.log").write_text(stream.getvalue(), encoding="utf-8")
    qa = dict(schema_version="lxtrace.qa.v1", checked_at=now, tests_run=result.testsRun,
              passed=result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),
              failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
              successful=result.wasSuccessful(), elapsed_seconds=elapsed,
              host={"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()})
    qa["implementation_files"] = {str(p.relative_to(ROOT)).replace("\\", "/"): sha256(p)
                                    for directory in ("lxtrace", "web", "sdk", "examples", "tests")
                                    for p in (ROOT / directory).glob("*") if p.is_file() and p.suffix in (".py", ".cpp", ".c", ".h", ".js", ".html", ".css")}
    write_json(qa, out / "qa-tests.json")
    remote = out / "server" / "validation.json"
    capture = json.loads(remote.read_text(encoding="utf-8")) if remote.exists() else json.loads((out / "local/validation.json").read_text(encoding="utf-8"))
    bench, runs = capture["benchmark"], capture["runs"]
    def esc(value):
        return html.escape(str(value))
    accepted = [
        ("AT-19 用户态主链路", "通过 D0 样例链路", "真实 C/C++ SDK 多线程捕获，导入、线程查看、选区及各格式导出。首个生产引擎尚未接入。"),
        ("AT-20 选区数学", "通过", "半开裁剪、区间并集、同线程等待排除、跨线程合并、零分母、driver 完整覆盖保护；Python–JS 一致。"),
        ("AT-21 能力边界", "通过 D0 降级", "schedule 明确失败且不启动业务；无 CPU 驻留插补；doctor 将元数据状态与未尝试的调度捕获分别保存。"),
        ("AT-22 失效与业务", "通过样例检查", "SDK 开关 checksum 一致；容量耗尽不 abort；未闭合保留 null；未知等待和无位置丢失采用保守计算下界。正式业务开销预算未验收。"),
        ("AT-25 离线与可复核", "功能检查通过，规模记录见下", "100,000 合成软件事件可导入 / 搜索 / 选区 / 导出；HTML 特殊字符安全；报告包含全部脚本、样式与数据。自动浏览器 file:// 被策略禁止，浏览器检查使用回环 HTTP。"),
        ("AT-23 / AT-24", "未开启", "可选 PMU / SPE 增强不包含在当前发布。"),
        ("MVP-B / P1 / P2 / AT-16", "未完成", "连续调度、核驻留、硬件归因和实际传输仍待相应权限、接口与计算节点验收。")]
    rows = "".join("<tr><td>" + "</td><td>".join(esc(x) for x in row) + "</td></tr>" for row in accepted)
    env = esc(json.dumps({k: capture[k] for k in ("host", "platform", "python", "compiler")}, ensure_ascii=False, indent=2))
    disabled, enabled = bench["disabled"]["median_seconds"], bench["enabled"]["median_seconds"]
    body = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LX2 Trace D0 · 验收记录</title>
<style>body{{margin:0;background:#f3f6f8;color:#183447;font:15px/1.9 "Segoe UI","Microsoft YaHei",sans-serif}}main{{max-width:1080px;margin:32px auto;background:white;padding:38px 46px;border-top:5px solid #087f8c}}h1{{font-size:31px}}h2{{margin-top:32px}}p,li{{color:#476173}}a{{color:#087f8c}}table{{width:100%;border-collapse:collapse;font-size:13px}}td,th{{border:1px solid #dce5ea;padding:12px;text-align:left;vertical-align:top}}th{{background:#eef4f6}}pre{{background:#173447;color:#dfedf1;border-radius:5px;padding:20px;white-space:pre-wrap;overflow-wrap:anywhere}}.callout{{border-left:4px solid #b68a30;background:#fff8ed;padding:14px 20px}}code{{background:#eef3f5;padding:2px 5px}}@media(max-width:700px){{main{{padding:20px;margin:0}}}}</style>
<main><a href="../README.html">← 使用指南</a><h1>LX2 Trace D0 · 验收记录</h1><p>{esc(now)} · v0.1.0-d0 · 依据 SRS v0.11 第 12A 节</p>
<p><b>自动化：{qa['passed']} / {qa['tests_run']} 通过，{qa['skipped']} 跳过，耗时 {elapsed:.3f}s。</b>测试覆盖区间数学、缺失、导入、CLI、导出和 Python–JS 一致性。<a href="../artifacts/qa-tests.json">结构化结果与源码摘要</a> · <a href="../artifacts/qa-tests.log">完整日志</a></p>
<table><tr><th>项目</th><th>结果</th><th>证据与范围</th></tr>{rows}</table>
<h2>真实 SDK 样例</h2><p>执行主机 <b>{esc(capture['host'])}</b>。正常会话 {runs['demo']['events']} 个归一化事件；未闭合场景保留 {runs['incomplete']['quality']['incomplete_events']} 个未结束事件；缓冲满场景丢失 {runs['loss']['quality'].get('dropped_records')} 条记录，业务正常退出。</p>
<pre>{env}</pre><p>业务输出：<code>{esc(runs['demo']['business'])}</code>。启用 / 禁用 SDK 的输出一致：<b>{esc(bench['business_outputs_match'])}</b>。原始 JSONL、会话、可执行文件摘要、命令、能力、质量及各轮耗时保存在 <a href="../artifacts/server/validation.json">服务器验证记录</a>。</p>
<p>{len(bench['enabled']['wall_seconds'])} 轮短样例进程墙钟中位数：关闭 {disabled*1000:.3f}ms，开启 {enabled*1000:.3f}ms，差值 {(enabled/disabled-1)*100:.2f}%。包含进程启动、sleep、打点及输出；时序抖动可能产生负差值，不将其解释为 SDK 加速。该结果不构成生产引擎、608 核规模或正式低于某百分比的开销验收。</p>
<div class="callout">当前远端样例在登录节点运行，目的是验证 ARM64 构建与 D0 软件打点链路。没有提交新的计算作业，没有将其写成 cn00409 的 LX2 硬件测量或实际模型验收。</div>
<h2>浏览器验证</h2><ul><li>真实软件会话可显示帧、算子、wait、请求与驱动调用外；数值选区和请求搜索跳转可用。</li><li>100,000 合成软件事件实际导入页面报告约 730ms。搜索特殊标签只显示文字，不执行 HTML；0–10ms 选区 JSON 与 Python 的 C_sem / R / overlap / wall 精确一致。</li><li>缩放 / 全览后保留原数值选区及结果。完整 HTML 导出再载入仍为新导入的 100,000 事件会话，避免被旧内嵌数据覆盖。</li><li>检查未闭合报告：end_ts=null，比例 N/A；能力页保留 unavailable / unverified。浏览器无脚本错误。</li><li>页面静态资源已内嵌，自包含检查通过；自动化浏览器只允许 HTTP/HTTPS，未绕过其 file:// 策略。查看器在本机回环静态服务中完成 UI 检查。</li></ul>
<p>参考工作站：Windows AMD64 / Chromium in-app browser。Node 24 上一次 10 万事件解析后分析约 162.7ms，查询后 heapUsed 约184.1MB；模拟 DOM 完成加载约513ms、Node 堆约156MiB。上述均为单次观察，非浏览器峰值内存或正式性能预算签收。</p>
<h2>仍待后续</h2><ul><li>生产模型 / 引擎与实际 GFTRACE 变体适配；MPI ABI 与 PMPI 自动拦截。</li><li>获准调度事件、可信作业 allocation / cpuset、CPU 驻留及 die / domain 验证映射。</li><li>PMU / SPE 增强、硬件诊断模型标定、真实设备传输与通信收益对照。</li><li>大于10万事件、10⁷事件 / 608核、生产开销和复杂历史模型导入的正式验收。</li></ul>
<p><a href="../artifacts/demo.html">打开 Demo</a> · <a href="../artifacts/synthetic-100k.html">10万事件报告</a> · <a href="../artifacts/gftrace.html">GFTRACE 合成格式夹具</a></p></main></html>'''
    destination = ROOT / "artifacts/local-docs/docs/validation.html"
    destination.parent.mkdir(parents=True, exist_ok=True)
    body = body.replace('href="../README.html"', 'href="../../../README.html"')
    body = body.replace('href="../artifacts/', 'href="../../')
    destination.write_text(body, encoding="utf-8")
    manifests = {str(p.relative_to(ROOT)).replace("\\", "/"): sha256(p)
                 for directory in ("lxtrace", "web", "sdk", "examples", "fixtures", "tests", "docs")
                 for p in (ROOT / directory).rglob("*") if p.is_file() and "__pycache__" not in str(p) and p.suffix != ".exe"}
    manifests["README.html"] = sha256(ROOT / "README.html")
    write_json({"created_at": now, "files": manifests}, ROOT / "artifacts/local-docs/releases/legacy-SHA256SUMS.json")
    print(json.dumps({"tests": qa['tests_run'], "passed": qa['passed'], "skipped": qa['skipped'], "success": result.wasSuccessful()}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
