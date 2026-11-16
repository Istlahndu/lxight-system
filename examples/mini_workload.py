"""A small ordinary workload: two Python threads, integer work and short sleeps.

There are no profiler imports or annotations.  Python's GIL means these two
threads do not prove simultaneous hardware computation on two cores.
"""
import threading
import time


def worker(index, results):
    state = 0x12345678 + index
    checksum = 0
    for block in range(28):
        for step in range(18000):
            state = (state * 1664525 + 1013904223 + (step + 1) * (index + 1)) & 0xFFFFFFFF
            checksum = (checksum + (state ^ (state >> 13))) & 0xFFFFFFFFFFFFFFFF
        # The two workers alternate the shorter and longer pause.
        time.sleep(.025 if (block + index) % 2 else .045)
    results[index] = checksum


def main():
    results = [None, None]
    threads = [threading.Thread(target=worker, args=(index, results), name="mini-worker-%d" % index)
               for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if any(value is None for value in results):
        raise RuntimeError("worker did not complete")
    checksum = (results[0] ^ ((results[1] << 1) & 0xFFFFFFFFFFFFFFFF)) & 0xFFFFFFFFFFFFFFFF
    print("workload=mini-integer-v1 workers=2 blocks=28 steps=18000", flush=True)
    print("checksum=%016x" % checksum, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
