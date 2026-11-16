#!/usr/bin/env python3
"""Independent 10 ms, two-CPU worked example through the public v2 CLI.

Run from anywhere: python examples/mini_validation.py
No server, SDK, perf access or external packages are needed.  Every input is
synthetic.  Expected values below are hand calculated, never derived from the
analyzer being checked.  Existing output directories are deliberately refused.
"""
import argparse
import copy
import csv
import datetime
import hashlib
import html
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MS = 1000000
EXPECTED = {
    "full": {"target_cpu_ns": 8*MS, "target_wall_ns": 6*MS, "peak_concurrency": 2,
             "capacity_cpu_ns": 20*MS, "idle_cpu_ns": 8*MS, "other_cpu_ns": 2*MS, "unknown_cpu_ns": 2*MS},
    "partial": {"target_cpu_ns": 4*MS, "target_wall_ns": 3*MS, "peak_concurrency": 2,
                "capacity_cpu_ns": 8*MS, "idle_cpu_ns": 3*MS, "other_cpu_ns": MS, "unknown_cpu_ns": 0},
    "cpu2": {"target_cpu_ns": 4*MS, "target_wall_ns": 4*MS, "peak_concurrency": 1,
             "capacity_cpu_ns": 10*MS, "idle_cpu_ns": 6*MS, "other_cpu_ns": 0, "unknown_cpu_ns": 0},
}


def dump(value, path):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")


def fixture():
    # This fixture intentionally does not import or call demo_session.
    s = {
        "schema_version": "lxtrace.resource-session.v2", "session_id": "independent-mini-10ms",
        "run_id": "hand-calculated-mini-v1", "data_origin": "synthetic", "time_range": [0, 10*MS],
        "app": {"name": "两核 10ms 手算校验", "build_id": "independent-mini-v1"},
        "resources": [
            {"resource_id": "node:mini", "type": "node", "parent_id": None, "name": "合成验证节点", "mapping_source": "hand-authored synthetic fixture"},
            {"resource_id": "domain:mini", "type": "domain", "parent_id": "node:mini", "name": "手算计算域", "mapping_source": "explicit synthetic mapping"},
            {"resource_id": "cpu:2", "type": "logical_cpu", "logical_cpu": 2, "parent_id": "domain:mini", "name": "CPU 2", "mapping_source": "synthetic"},
            {"resource_id": "cpu:6", "type": "logical_cpu", "logical_cpu": 6, "parent_id": "domain:mini", "name": "CPU 6", "mapping_source": "synthetic"},
            {"resource_id": "process:mini", "type": "process", "parent_id": "node:mini", "name": "合成任务级计数对象", "mapping_source": "synthetic task instance"},
        ],
        "allocation": {"status": "verified_fixture", "cpu_ids": [2, 6], "evidence_ref": "raw/fixture/README.txt"},
        "sources": [
            {"source_id": "mini-sched", "source_layer": "os", "scope": "logical_cpu", "clock_id": "mini-clock", "loss": 0,
             "boundary_semantics": "hand-authored synthetic scheduler intervals"},
            {"source_id": "mini-task-counter", "source_layer": "hardware", "scope": "task", "clock_id": "mini-clock",
             "resource_id": "process:mini", "boundary_support": "counter_window", "semantics_status": "synthetic_counter_only"},
            {"source_id": "mini-sample", "source_layer": "os", "scope": "task", "clock_id": "mini-clock", "boundary_support": "sample_only"},
        ],
        "clocks": [{"clock_id": "mini-clock", "unit": "ns", "resolution_ns": 1000, "error_ns": 0,
                    "calibration_status": "verified_fixture", "aligned_to": "mini-session"}],
        "records": [], "capability_manifest": [], "quality": {"unknown_windows": []},
        "provenance": [{"kind": "synthetic", "generator": "examples/mini_validation.py", "purpose": "independent hand-calculated software validation; no hardware acceptance claim"}],
    }
    rows = [(2, 0, 4, "target", "target:A"), (2, 4, 10, "idle", None),
            (6, 0, 2, "idle", None), (6, 2, 6, "target", "target:B"),
            (6, 6, 8, "other", None), (6, 8, 10, "unknown", None)]
    for i, (cpu, a, b, state, instance) in enumerate(rows):
        s["records"].append({"record_id": "mini-sched-"+str(i), "resource_id": "cpu:"+str(cpu), "source_id": "mini-sched",
                             "category": "schedule", "layer": "os", "representation": "interval", "state": state,
                             "thread_instance_id": instance, "begin_ns": a*MS, "end_ns": b*MS, "uncertainty_ns": 0,
                             "raw_refs": ["raw/fixture/records.jsonl#"+str(i)], "correlation_ids": {}})
    s["records"].append({"record_id": "task-count-100", "resource_id": "process:mini", "source_id": "mini-task-counter",
                         "category": "pmu", "layer": "hardware", "representation": "counter_window", "begin_ns": 0, "end_ns": 10*MS,
                         "event": "synthetic-task-counter", "unit": "count", "raw_count": 100, "scaled_count": 100,
                         "enabled_ns": 10*MS, "running_ns": 10*MS, "uncertainty_ns": None,
                         "raw_refs": ["raw/fixture/records.jsonl#6"], "correlation_ids": {}})
    s["records"].append({"record_id": "sample-at-5ms", "resource_id": "cpu:6", "source_id": "mini-sample",
                         "category": "cpu_point", "layer": "os", "representation": "sample", "timestamp_ns": 5*MS,
                         "name": "5ms 离散观测点", "uncertainty_ns": 0, "raw_refs": ["raw/fixture/records.jsonl#7"], "correlation_ids": {}})
    s["raw_files"] = {
        "fixture/README.txt": "SYNTHETIC / 合成输入。手写两CPU、10ms调度范围，任务级计数窗及单个采样点；没有真实采集或硬件能力声明。\n",
        "fixture/records.jsonl": "\n".join(json.dumps(r, ensure_ascii=False) for r in s["records"])+"\n",
    }
    return s


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="artifacts/v2/mini-check", help="new project-local output directory; existing paths are refused")
    args = parser.parse_args()
    out = Path(args.output).resolve()
    out.relative_to(ROOT.resolve())
    if out.exists():
        raise ValueError("output already exists; choose a new --output directory: "+str(out))
    out.mkdir(parents=True)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", TMP=str(out), TEMP=str(out), TMPDIR=str(out))
    commands, checks = [], []

    def check(name, passed, evidence=None):
        checks.append({"name": name, "status": "pass" if passed else "fail", "evidence": evidence})

    def cli(*arguments, expect_failure=False):
        command = [sys.executable, "-B", "-m", "lxtrace", *map(str, arguments)]
        result = subprocess.run(command, cwd=ROOT, env=env, text=True, encoding="utf-8", capture_output=True, timeout=30)
        commands.append({"command": command, "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        passed = result.returncode != 0 if expect_failure else result.returncode == 0
        check("CLI "+" ".join(map(str, arguments[:2])), passed, {"exit_code": result.returncode, "stderr": result.stderr})
        if not passed:
            raise RuntimeError("CLI failed unexpectedly: "+" ".join(command)+"\n"+result.stderr)
        return result

    s = fixture()
    dump(s, out/"input.synthetic.json")
    dump(EXPECTED, out/"expected.hand-calculated.json")
    archive = out/"mini.lxtrace"
    cli("import", "--type", "session", "--input", out/"input.synthetic.json", "--output", archive)

    # Import only the public strict reader, never an analysis function, for integrity checks.
    sys.path.insert(0, str(ROOT))
    from lxtrace.resource import load_session
    readback = load_session(archive)
    check("归档严格读回与原始输入一致", readback["records"] == s["records"] and readback["data_origin"] == "synthetic")
    expected_raw = s["raw_files"]["fixture/records.jsonl"].encode("utf-8")
    check("UTF-8 raw原文和SHA256一致", (archive/"raw/fixture/records.jsonl").read_bytes() == expected_raw,
          {"sha256": hashlib.sha256(expected_raw).hexdigest(), "integrity": readback["archive"]["integrity"]})

    queries = {"full": [], "partial": ["--from", "3ms", "--to", "7ms"], "cpu2": ["--resource", "cpu:2"]}
    actual = {}
    for name, options in queries.items():
        destination = out/(name+".analysis.json")
        cli("analyze", archive, *options, "--output", destination)
        actual[name] = json.loads(destination.read_text(encoding="utf-8"))
        for field, expected in EXPECTED[name].items():
            check(name+" / "+field, actual[name].get(field) == expected, {"expected": expected, "actual": actual[name].get(field)})
        state_sum = sum(actual[name][key] for key in ("target_cpu_ns", "idle_cpu_ns", "other_cpu_ns", "unknown_cpu_ns"))
        check(name+" / 四态容量守恒", state_sum == EXPECTED[name]["capacity_cpu_ns"])

    check("全窗任务计数保持100", len(actual["full"]["counters"]) == 1 and actual["full"]["counters"][0]["selection_value"] == 100)
    check("部分选区不均摊整窗100", len(actual["partial"]["counters"]) == 1 and actual["partial"]["counters"][0]["selection_value"] is None)
    check("5ms样本点保持离散", len(actual["full"]["samples"]) == 1 and actual["full"]["samples"][0]["timestamp_ns"] == 5*MS)
    check("硬件重叠不由窗或样本产生", actual["full"]["hardware_overlap"]["status"] == "unavailable")

    baseline = copy.deepcopy(s)
    baseline["records"] = [r for r in baseline["records"] if r["source_id"] == "mini-sched"]
    baseline["raw_files"]["fixture/records.jsonl"] = "\n".join(json.dumps(r, ensure_ascii=False) for r in baseline["records"])+"\n"
    dump(baseline, out/"residency-only.synthetic.json")
    cli("analyze", out/"residency-only.synthetic.json", "--output", out/"residency-only.analysis.json")
    without_extras = json.loads((out/"residency-only.analysis.json").read_text(encoding="utf-8"))
    check("加入任务计数窗和样本不会改变驻留", all(without_extras[k] == actual["full"][k] for k in EXPECTED["full"]))

    cli("export", archive, "--format", "html", "--output", out/"index.html")
    cli("export", archive, "--format", "csv", "--output", out/"mini.csv")
    cli("export", archive, "--format", "chrome-trace", "--output", out/"mini.trace.json")
    cli("bundle-viewer", archive, "--output", out/"viewer")
    report = (out/"index.html").read_text(encoding="utf-8")
    check("离线HTML内嵌10ms会话及查看器", "window.LXTRACE_SESSION=" in report and 'src="viewer.js"' not in report
          and json.loads((out/"viewer/demo-session.json").read_text(encoding="utf-8"))["time_range"] == [0, 10*MS])
    with (out/"mini.csv").open(encoding="utf-8-sig", newline="") as f:
        csv_rows = list(csv.DictReader(f))
    check("CSV保留两核统计并附原始metadata", len(csv_rows) == 2 and {int(r["logical_cpu"]) for r in csv_rows} == {2, 6}
          and json.loads((out/"mini.csv.metadata.json").read_text(encoding="utf-8"))["counters"][0]["selection_value"] == 100)
    trace = json.loads((out/"mini.trace.json").read_text(encoding="utf-8"))
    check("Trace保存native且计数窗与样本仅为点注记", trace["lxtrace_native"]["records"] == s["records"]
          and all(e["ph"] == "i" for e in trace["traceEvents"] if e["args"]["representation"] in ("counter_window", "sample")))

    recover_source = out/"missing-index.lxtrace"
    shutil.copytree(archive, recover_source)
    (recover_source/"index/resources.json").unlink()
    cli("analyze", recover_source, "--output", out/"should-not-exist.json", expect_failure=True)
    cli("rebuild", recover_source, "--output", out/"rebuilt.lxtrace")
    rebuilt = load_session(out/"rebuilt.lxtrace")
    check("缺索引重建保留raw并不修改原归档", rebuilt["records"] == s["records"]
          and (out/"rebuilt.lxtrace/raw/fixture/records.jsonl").read_bytes() == expected_raw
          and not (recover_source/"index/resources.json").exists() and (archive/"index/resources.json").is_file())

    tampered = out/"tampered.lxtrace"
    shutil.copytree(archive, tampered)
    raw_file = tampered/"raw/fixture/records.jsonl"
    raw_file.write_bytes(raw_file.read_bytes()+b"tampered\n")
    rejection = cli("analyze", tampered, "--output", out/"tampered-should-not-exist.json", expect_failure=True)
    check("raw篡改副本被hash拒绝且不产出分析", "checksum mismatch" in rejection.stderr and not (out/"tampered-should-not-exist.json").exists())

    node = shutil.which("node")
    if node:
        requests = [{"session": s, "start": 0, "end": 10*MS, "options": {}},
                    {"session": s, "start": 3*MS, "end": 7*MS, "options": {}},
                    {"session": s, "start": 0, "end": 10*MS, "options": {"resource_id": "cpu:2"}}]
        script = "const a=require('./web/analysis.js');let t='';process.stdin.on('data',d=>t+=d);process.stdin.on('end',()=>process.stdout.write(JSON.stringify(JSON.parse(t).map(x=>a.analyze(x.session,x.start,x.end,x.options)))));"
        proc = subprocess.run([node, "-e", script], input=json.dumps(requests), text=True, encoding="utf-8", capture_output=True, cwd=ROOT, env=env, timeout=20)
        check("Node analyzer执行", proc.returncode == 0, proc.stderr)
        if proc.returncode == 0:
            js_results = json.loads(proc.stdout)
            dump(js_results, out/"node.analysis.json")
            for name, js in zip(queries, js_results):
                check("Node独立手算预期 / "+name, all(js.get(k) == v for k, v in EXPECTED[name].items()))
            check("Node部分计数窗N/A且无hardware overlap", js_results[1]["counters"][0]["selected_value"] is None
                  and js_results[0]["hardware_overlap"]["status"] == "unavailable")
    else:
        checks.append({"name": "Node analyzer parity", "status": "skipped", "evidence": "Node unavailable; Python/CLI checks remain recorded separately"})

    result = {"schema_version": "lxtrace.independent-mini-validation.v1", "created_at": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
              "data_origin": "synthetic", "scope": "independent hand-calculated 10ms example; no live hardware acceptance",
              "expected": EXPECTED, "checks": checks, "commands": commands,
              "passed": sum(c["status"] == "pass" for c in checks), "failed": sum(c["status"] == "fail" for c in checks),
              "skipped": sum(c["status"] == "skipped" for c in checks)}
    dump(result, out/"validation.json")
    summary = "通过" if not result["failed"] else "发现失败"
    rows = "".join("<tr><td>"+html.escape(name)+"</td>"+"".join("<td>"+str(values[k] if k == "peak_concurrency" else values[k]/MS)+"</td>" for k in EXPECTED["full"])+"</tr>" for name, values in EXPECTED.items())
    body = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>两核10ms独立手算验证</title><style>body{font:16px/1.8 "Microsoft YaHei",sans-serif;background:#f4f7f8;color:#203647;margin:0}main{max-width:1050px;margin:32px auto;padding:32px;background:white;border-top:5px solid #087f8c}table{border-collapse:collapse;width:100%;font-size:13px}td,th{border:1px solid #dce6eb;padding:8px;text-align:left}a{color:#087f8c}.result{font-size:22px}code{font-family:Consolas,monospace}</style><main><p>SYNTHETIC · 独立手算输入</p><h1>两核 10ms 小例验证</h1><p class="result">'+summary+'：'+str(result["passed"])+' 项通过，'+str(result["failed"])+' 项失败，'+str(result["skipped"])+' 项跳过。</p><p><a href="index.html">打开10ms交互时间线</a> · <a href="validation.json">验证结果与完整命令</a> · <a href="expected.hand-calculated.json">独立手算预期</a> · <a href="mini.csv">CSV</a> · <a href="mini.trace.json">Trace</a></p><p>CPU2：target [0,4)、idle [4,10) ms。CPU6：idle [0,2)、target [2,6)、other [6,8)、unknown [8,10) ms。所有范围为半开区间。</p><table><tr><th>查询</th><th>目标 CPU-ms</th><th>目标墙钟 ms</th><th>峰值CPU</th><th>容量 CPU-ms</th><th>idle CPU-ms</th><th>other CPU-ms</th><th>unknown CPU-ms</th></tr>'+rows+'</table><p>额外任务级计数窗 [0,10) ms 的值为100；选区 [3,7) ms 的计数为N/A，不均摊。5ms样本保持离散点，两者均不改变核驻留；缺硬件活动源时硬件重叠保持unavailable。</p><p>已验证公开CLI导入、查询、HTML/CSV/Trace导出、原始文件与哈希、缺索引重建，以及篡改副本被拒绝。<code>missing-index.lxtrace</code> 和 <code>tampered.lxtrace</code> 是故意破坏的负例，请勿将其当成正常会话。</p><p>本例仅验证当前软件数学、证据边界与文件流程；所有输入为合成，不能证明实机连续驻留或硬件采集能力。</p></main></html>'
    (out/"README.html").write_text(body, encoding="utf-8")
    (out/"README.md").write_text("# 两核10ms独立手算验证\n\n入口：README.html；时间线：index.html；结果：validation.json。\n\n"+summary+"："+str(result["passed"])+"项通过，"+str(result["failed"])+"项失败，"+str(result["skipped"])+"项跳过。\n\n全部输入为synthetic，未使用demo_session，预期为独立硬编码。故意破坏的missing-index.lxtrace和tampered.lxtrace只作负例。\n", encoding="utf-8")
    print(json.dumps({"output": str(out), "passed": result["passed"], "failed": result["failed"], "skipped": result["skipped"], "entry": str(out/"README.html")}, ensure_ascii=False))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
