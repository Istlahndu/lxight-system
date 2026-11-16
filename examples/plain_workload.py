"""Ordinary uninstrumented workload: no SDK, annotations, or profiler imports."""
import math
import threading
import time


def worker(index, results):
    total = 0.0
    for block in range(18):
        total += sum(math.sqrt((i+index)%1021+1) for i in range(60000))
        if block % 3 == 0:
            time.sleep(0.008)
    results[index] = total


if __name__ == '__main__':
    results = [0.0]*3
    threads = [threading.Thread(target=worker,args=(i,results),name='worker-%d'%i) for i in range(3)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    print('checksum=%.6f' % sum(results),flush=True)
