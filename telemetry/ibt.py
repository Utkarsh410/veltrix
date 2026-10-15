"""Minimal iRacing .ibt telemetry reader."""
import struct
import numpy as np
import yaml

TYPES = {0: 'S1', 1: '?', 2: '<i4', 3: '<u4', 4: '<f4', 5: '<f8'}


class IBT:
    def __init__(self, path):
        with open(path, 'rb') as f:
            self.raw = f.read()
        r = self.raw
        (self.ver, _status, self.tick_rate, _siu, si_len, si_off, nvars,
         vh_off, _nbuf, self.buf_len) = struct.unpack_from('<10i', r, 0)
        buf_off = struct.unpack_from('<i', r, 52)[0]
        start_date, self.start_t, self.end_t, self.lap_count, self.records = \
            struct.unpack_from('<qddii', r, 112)
        self.vars = {}
        for i in range(nvars):
            o = vh_off + i * 144
            typ, off, cnt = struct.unpack_from('<3i', r, o)
            name = r[o + 16:o + 48].split(b'\0')[0].decode()
            desc = r[o + 48:o + 112].split(b'\0')[0].decode(errors='replace')
            unit = r[o + 112:o + 144].split(b'\0')[0].decode(errors='replace')
            self.vars[name] = dict(type=typ, offset=off, count=cnt, desc=desc, unit=unit)
        si = r[si_off:si_off + si_len].split(b'\0')[0].decode('latin-1')
        self.session_info = yaml.safe_load(si.replace('\t', ' '))
        n = (len(r) - buf_off) // self.buf_len
        self.records = min(self.records or n, n)
        self.block = np.frombuffer(r, dtype=np.uint8, count=self.records * self.buf_len,
                                   offset=buf_off).reshape(self.records, self.buf_len)

    def get(self, name, idx=0):
        v = self.vars[name]
        dt = np.dtype(TYPES[v['type']])
        o = v['offset'] + idx * dt.itemsize
        return self.block[:, o:o + dt.itemsize].copy().view(dt).ravel()
