"""Generate the frozen GFTRACE1 fixture; labels are software/logical, never CPU residency."""
import struct
from pathlib import Path

def generate():
    out = bytearray(struct.pack("<8sQii", b"GFTRACE1", 1000000000, 2, 0))
    buffers = [
        (0, -1, 0, [(1000000000,9,1,0),(1001000000,13,0,0),(1002000000,14,0,0),
                     (1002000000,15,0,0),(1008000000,16,0,0),
                     (1008000000,19,0,0),(1009000000,20,0,0),(1010000000,10,1,0)]),
        (0, 0, 0, [(1002000000,1,0,0),(1004000000,3,0,0),(1005000000,4,0,0),
                    (1008000000,2,0,0)])]
    for die, domain, thread, events in buffers:
        out += struct.pack("<iiii",die,domain,thread,len(events))
        for event in events:
            out += struct.pack("<QHHi",*event)
    Path(__file__).with_name("gftrace-demo.bin").write_bytes(out)

if __name__ == "__main__":
    generate()
