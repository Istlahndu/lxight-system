# 调度 switch JSONL 导入合同 v1

此接口接收由获准来源导出的事件流，不解码未知版本 perf.data，不自行探测其他作业。

```json
{"timestamp_ns":1000000,"cpu":2,"event":"switch","prev_instance":"idle:2","next_instance":"boot:pid7:start123:tid7","prev_tid":0,"next_tid":7,"raw_ref":"raw/schedule/events.jsonl:1"}
{"timestamp_ns":4000000,"cpu":2,"event":"switch","prev_instance":"boot:pid7:start123:tid7","next_instance":"idle:2","prev_tid":7,"next_tid":0}
{"timestamp_ns":5000000,"cpu":2,"event":"loss","loss_begin_ns":4500000}
```

metadata JSON 至少包含 `session_id, run_id, time_range, resources, allocation, clocks, target_instances`。`resources` 为 v2 资源图；logical_cpu 项含整数 CPU ID。`target_instances` 是完整进程/线程实例标识，不能仅用可复用 PID。`allocation.status` 必须披露来源，用户自填不构成真实服务授权。文件导入本身保持 data_origin=imported。

事件按 timestamp_ns 排序，CPU 必须在 metadata 范围内。首条 switch 前保持 unknown，切出期间不会把线程延长到旧核；prev_instance 不匹配会使前段未知。loss 缺位置时从上个可证明边界起保守未知，下个 switch 才重建状态。`reliable_stop=true` 仅允许生产者有协议证据证明无丢失、持续到采集结束时设置；否则尾段 `end_ns=null`。其他作业只保留匿名 other，占用实例ID不输出。

原始引用、时钟变换/误差和来源字典由生产者保留；这些 OS 区间不证明矩阵/向量执行单元 busy。调度归一化单元测试不等于已实现获准实时调度采集服务。
