"""Loss-aware reconstruction of an explicitly supplied scheduler event stream.

Input is a documented JSONL interchange, not a guessed perf binary decoder.
The adapter never reads a shared account's running processes or other jobs.
"""
import json
from pathlib import Path


def reconstruct_switches(events, cpu_resources, time_range, target_instances, source_id='schedule', reliable_stop=False):
    """Turn switch events into OS residency, preserving unknown start/loss/tail.

    Events: {timestamp_ns,cpu,event:'switch'|'loss',prev_instance,next_instance,
    prev_tid?,next_tid?,raw_ref?}. Stable instance IDs must come from the producer.
    A record with next_tid=0 is idle; other workloads are anonymized.
    A mismatched prev_instance invalidates the preceding segment.
    """
    start, stop = time_range
    if stop <= start:
        raise ValueError('scheduler time range must be positive')
    targets = set(target_instances)
    states = {int(cpu): dict(at=start, instance=None, tid=None, ref=None) for cpu in cpu_resources}
    records, unknown = [], []

    def emit(cpu, state, end, force_unknown=False):
        begin = state['at']
        if end is not None and end <= begin:
            return
        instance = state['instance']
        kind = 'unknown' if force_unknown or instance is None else ('idle' if state['tid'] == 0 else ('target' if instance in targets else 'other'))
        record = dict(record_id='sched-%d' % len(records), resource_id=cpu_resources[cpu], source_id=source_id,
                      category='schedule', layer='os', representation='interval', begin_ns=begin, end_ns=end,
                      state=kind, thread_instance_id=instance if kind == 'target' else None,
                      boundary_semantics='OS context-switch residency; not execution-unit busy',
                      raw_refs=[state['ref']] if state['ref'] else [], uncertainty_ns=0,
                      status='incomplete' if end is None else 'complete')
        records.append(record)
        if kind == 'unknown' and end is not None:
            unknown.append(dict(resource_id=cpu_resources[cpu], begin_ns=begin, end_ns=end, reason='scheduler_state_unproven'))

    previous = start
    for i, event in enumerate(events):
        t = event['timestamp_ns']
        cpu = int(event['cpu'])
        if t < previous:
            raise ValueError('scheduler stream must be globally time ordered')
        previous = t
        if cpu not in states:
            raise ValueError('scheduler event outside supplied resource scope')
        if not start <= t <= stop:
            raise ValueError('scheduler timestamp outside capture range')
        s = states[cpu]
        if event['event'] == 'loss':
            # Loss start is explicit when available, otherwise invalidate since last proof.
            loss_start = event.get('loss_begin_ns', s['at'])
            if not s['at'] <= loss_start <= t:
                raise ValueError('invalid scheduler loss window')
            emit(cpu, s, loss_start)
            if t > loss_start:
                emit(cpu, dict(at=loss_start, instance=None, tid=None, ref=event.get('raw_ref')), t)
            states[cpu] = dict(at=t, instance=None, tid=None, ref=event.get('raw_ref'))
        elif event['event'] == 'switch':
            if 'next_instance' not in event and event.get('next_tid') != 0:
                raise ValueError('switch requires stable next_instance (or idle next_tid=0)')
            mismatch = s['instance'] is not None and s['instance'] != event.get('prev_instance')
            emit(cpu, s, t, force_unknown=mismatch)
            states[cpu] = dict(at=t, instance=event.get('next_instance') or ('idle:cpu:%d' % cpu if event.get('next_tid') == 0 else None),
                               tid=event.get('next_tid'), ref=event.get('raw_ref', 'raw/schedule/events.jsonl:%d' % (i+1)))
        else:
            raise ValueError('unsupported scheduler interchange event')
    for cpu, state in states.items():
        if state['at'] < stop:
            emit(cpu, state, stop if reliable_stop else None)
    # A stable thread instance cannot run simultaneously on two logical CPUs.
    # Mask the contradictory overlap on both CPUs. Checking complete intervals
    # also handles switch-out/in events sharing a timestamp in either order.
    by_instance = {}
    for record in records:
        if record.get('thread_instance_id') and record.get('end_ns') is not None:
            by_instance.setdefault(record['thread_instance_id'],[]).append(record)
    protocol_errors = []
    for instance, spans in by_instance.items():
        active = []
        for record in sorted(spans,key=lambda r:r['begin_ns']):
            active = [r for r in active if r['end_ns'] > record['begin_ns']]
            for prior in active:
                begin,end = max(prior['begin_ns'],record['begin_ns']),min(prior['end_ns'],record['end_ns'])
                if prior['resource_id'] != record['resource_id'] and begin < end:
                    for r in (prior,record):
                        unknown.append(dict(resource_id=r['resource_id'],begin_ns=begin,end_ns=end,
                                            reason='contradictory_simultaneous_thread_instance'))
                    protocol_errors.append(dict(thread_instance_id=instance,begin_ns=begin,end_ns=end,
                                                reason='thread_instance_on_multiple_cpus'))
            active.append(record)
    return records, dict(unknown_windows=unknown, reliable_stop=bool(reliable_stop),
                         protocol_errors=protocol_errors,
                         initial_state='unknown_until_first_switch', reconstruction_version='switch-jsonl-v1')


def import_schedule(path, metadata):
    """Import a producer-scoped stream; metadata must carry topology and scope."""
    from .resource import validate_session
    from .core import sha256
    meta = json.loads(Path(metadata).read_text(encoding='utf-8-sig'))
    rows = [json.loads(line) for line in Path(path).read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    cpu_map = {r['logical_cpu']: r['resource_id'] for r in meta['resources'] if r['type'] == 'logical_cpu'}
    records, quality = reconstruct_switches(rows, cpu_map, meta['time_range'], meta.get('target_instances', []),
                                            reliable_stop=meta.get('reliable_stop', False))
    session = dict(meta, schema_version='lxtrace.resource-session.v2', data_origin='imported', records=records,
                   sources=[dict(source_id='schedule', source_layer='os', scope='producer_verified_subset',
                                 clock_id=meta.get('clock_id','monotonic'), native_format='lxtrace.sched-switch-jsonl.v1',
                                 loss=0, boundary_support='context_switch_only')],
                   quality=quality, provenance=[dict(path=str(path),sha256=sha256(path),decoder='switch-jsonl-v1')],
                   raw_files={'schedule/events.jsonl': Path(path).read_text(encoding='utf-8-sig')})
    return validate_session(session)
