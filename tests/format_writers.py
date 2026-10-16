"""
Writers for the file formats the importers read, used only by the tests.

Each takes the normalised channels of a session (the `logger` golden session)
and writes them in one format, so every importer can be checked against the
same known answer. The writers follow the published layout of each format
where there is one (iRacing SDK, VBOX, DBC, Vector ASCII, candump, PEAK), and
the layout documented by the open-source ldparser project for MoTeC .ld.
"""

from __future__ import annotations

import struct

import numpy as np

from veltrix_engine.importers import motec

EXT_ID = 0x18FF01FE
EARTH_RADIUS = 6_371_000.0

DBC_TEXT = f'''VERSION ""

NS_ :

BS_:

BU_: ECU

BO_ 256 Dynamics: 8 ECU
 SG_ Speed : 0|16@1+ (0.01,0) [0|400] "km/h" Vector__XXX
 SG_ RPM : 16|16@1+ (0.25,0) [0|16000] "rpm" Vector__XXX
 SG_ Steered_Angle : 39|16@0- (0.1,0) [-800|800] "deg" Vector__XXX
 SG_ Gear : 48|4@1+ (1,0) [0|8] "" Vector__XXX
 SG_ Lap_Number : 56|8@1+ (1,0) [0|255] "" Vector__XXX

BO_ 257 Pedals: 4 ECU
 SG_ Throttle_Pos : 0|8@1+ (0.5,0) [0|100] "%" Vector__XXX
 SG_ Brake_Pres_Front : 15|16@0+ (0.01,0) [0|200] "bar" Vector__XXX

BO_ {EXT_ID | 0x80000000} Accel: 8 ECU
 SG_ G_Force_Lat : 0|16@1- (0.001,0) [-8|8] "g" Vector__XXX
 SG_ G_Force_Long : 16|16@1- (0.001,0) [-8|8] "g" Vector__XXX

VAL_ 256 Gear 0 "N" 1 "1st" ;
'''


def can_frames(ch) -> list:
    """The session as CAN frames: [(time, id, extended, payload)]. Pedals are
    sent at half the rate of the rest, and one identifier is not in the DBC."""
    frames = []
    for i, r in enumerate(ch.itertuples()):
        dyn = (struct.pack("<HH", round(r.speed / 0.01), round(r.rpm / 0.25))
               + struct.pack(">h", round(r.steering / 0.1)) + bytes([int(r.gear) & 0xF, int(r.lap) & 0xFF]))
        frames.append((r.time, 0x100, False, dyn))
        frames.append((r.time + 0.001, EXT_ID, True, struct.pack("<hh", round(r.acc_lat / 0.001), round(r.acc_long / 0.001)) + bytes(4)))
        if i % 2 == 0:
            frames.append((r.time + 0.002, 0x101, False,
                           bytes([min(max(round(r.throttle / 0.5), 0), 200)])
                           + struct.pack(">H", max(round(r.brake / 0.01), 0)) + b"\0"))
        if i % 20 == 0:
            frames.append((r.time + 0.003, 0x7AA, False, bytes(8)))
    return frames


def _hex(payload: bytes) -> str:
    return " ".join(f"{b:02X}" for b in payload)


def candump(frames) -> str:
    return "\n".join(f"({1_700_000_000 + t:.6f}) can0 {i:08X}#{p.hex().upper()}" if x else
                     f"({1_700_000_000 + t:.6f}) can0 {i:03X}#{p.hex().upper()}" for t, i, x, p in frames) + "\n"


def asc(frames) -> str:
    head = ["date Thu Oct 8 10:59:30.000 am 2026", "base hex  timestamps absolute", "internal events logged",
            "// version 13.0.0", "Begin Triggerblock Thu Oct 8 10:59:30.000 am 2026", "   0.000000 Start of measurement"]
    body = [f"{t:11.6f} 1  {i:X}{'x' if x else ''}             Rx   d {len(p)} {_hex(p)}  Length = 230000 BitCount = 119 ID = {i}"
            for t, i, x, p in frames]
    return "\n".join(head + body + ["End TriggerBlock"]) + "\n"


def trc_v1(frames) -> str:
    head = [";$FILEVERSION=1.1", ";$STARTTIME=46303.4579861111", ";", ";   Message Number", ";---+--   ----+----  --+--  ----+---  +  -+ -- -- --"]
    body = [f"{n + 1:6d}) {t * 1000:11.1f}  Rx     {i:08X}  {len(p)}  {_hex(p)}" if x else
            f"{n + 1:6d}) {t * 1000:11.1f}  Rx         {i:04X}  {len(p)}  {_hex(p)}" for n, (t, i, x, p) in enumerate(frames)]
    return "\n".join(head + body) + "\n"


def trc_v2(frames) -> str:
    head = [";$FILEVERSION=2.0", ";$STARTTIME=46303.4579861111", ";$COLUMNS=N,O,T,I,d,l,D", ";"]
    body = [f"{n + 1:7d} {t * 1000:13.3f} DT {i:08X} Rx {len(p)}  {_hex(p)}" if x else
            f"{n + 1:7d} {t * 1000:13.3f} DT     {i:04X} Rx {len(p)}  {_hex(p)}" for n, (t, i, x, p) in enumerate(frames)]
    return "\n".join(head + body) + "\n"


def can_csv(frames) -> str:
    rows = ["Time Stamp,ID,Extended,Dir,Bus,LEN,D1,D2,D3,D4,D5,D6,D7,D8"]
    for t, i, x, p in frames:
        cells = [f"{b:02X}" for b in p] + [""] * (8 - len(p))
        rows.append(f"{round(t * 1e6)},{i:08X},{'true' if x else 'false'},Rx,0,{len(p)}," + ",".join(cells))
    return "\n".join(rows) + "\n"


# ── VBOX ──────────────────────────────────────────────────────────────────────

def vbo(ch, lap_length: float, crossings=()) -> str:
    """A .vbo of the session driven round a circle of the lap's length. The
    car starts on the stored start/finish line and is back on it at each of
    the `crossings` times."""
    lat0, lon0, radius = 45.0, 9.0, lap_length / (2 * np.pi)
    k = np.cos(np.radians(lat0))
    to_lat = lambda y: lat0 + np.degrees(y / EARTH_RADIUS)
    to_lon = lambda x: lon0 + np.degrees(x / (EARTH_RADIUS * k))
    t, d = ch["time"].to_numpy(), ch["distance"].to_numpy()
    marks_t = np.r_[t[0], crossings]
    marks_d = np.interp(marks_t, t, d)
    marks_t, marks_d = np.r_[marks_t, t[-1] + 1e-9], np.r_[marks_d, max(marks_d[-1] + lap_length, d[-1] + 1.0)]
    lap = np.clip(np.searchsorted(marks_t, t, side="right") - 1, 0, len(marks_t) - 2)
    angle = 2 * np.pi * (lap + (d - marks_d[lap]) / (marks_d[lap + 1] - marks_d[lap]))
    lat, lon = to_lat(radius * np.sin(angle)), to_lon(radius * np.cos(angle))
    clock = 10 * 3600 + 59 * 60 + 30 + ch["time"].to_numpy()

    def stamp(s):
        h, rest = divmod(s, 3600)
        m, sec = divmod(rest, 60)
        return f"{int(h):02d}{int(m):02d}{sec:05.2f}"

    gate = (to_lon(radius - 15), to_lat(0.0), to_lon(radius + 15), to_lat(0.0))
    lines = ["File created on 08/10/2026 at 10:59:30", "", "[header]", "satellites", "time", "latitude", "longitude",
             "velocity kmh", "heading", "height", "vertical velocity kmh", "Engine_Speed", "Steering_Angle", "",
             "[channel units]", "rpm", "deg", "", "[comments]", "(c) 2001 - 2019 Racelogic", "VBOX 3i",
             "Log Rate (Hz) : 20.00", "", "[laptiming]",
             f"Start   {-gate[0] * 60:+013.6f} {gate[1] * 60:+012.6f} {-gate[2] * 60:+013.6f} {gate[3] * 60:+012.6f} ¬ Start / Finish",
             "", "[column names]", "sats time lat long velocity heading height vert-vel Engine_Speed Steering_Angle", "", "[data]"]
    for i, r in enumerate(ch.itertuples()):
        lines.append(f"009 {stamp(clock[i])} {lat[i] * 60:+012.6f} {-lon[i] * 60:+013.6f} {r.speed:07.3f} 000.00 "
                     f"+0100.00 +000.000 {r.rpm:.2f} {r.steering:+.3f}")
    return "\n".join(lines) + "\n"


# ── MoTeC ─────────────────────────────────────────────────────────────────────

def _s(text: str, n: int) -> bytes:
    return text.encode().ljust(n, b"\0")


def ld(channels: list, venue="Synthetic Circuit", driver="Test Driver", vehicle="GT3") -> bytes:
    """A .ld file. channels: [(name, unit, rate_hz, values, encoding)], with
    encoding "f4", ("i2", decimals) or ("i4", decimals)."""
    meta_size = motec._CHANNEL.size + 40
    meta_ptr = motec._HEAD.size
    data_ptr = meta_ptr + meta_size * len(channels)
    blocks, metas, at = [], [], data_ptr
    for k, (name, unit, rate, values, enc) in enumerate(channels):
        if enc == "f4":
            kind, size, dec, block = 0x07, 4, 0, np.asarray(values, "<f4").tobytes()
        else:
            size, dec = int(enc[0][1]), enc[1]
            kind = 0x03 if size == 2 else 0x05
            block = np.round(np.asarray(values) * 10 ** dec).astype(f"<i{size}").tobytes()
        prev = meta_ptr + meta_size * (k - 1) if k else 0
        nxt = meta_ptr + meta_size * (k + 1) if k + 1 < len(channels) else 0
        metas.append(motec._CHANNEL.pack(prev, nxt, at, len(values), 0x2EE1 + k, kind, size, rate, 0, 1, 1, dec,
                                         _s(name, 32), _s(name[:7], 8), _s(unit, 12)) + bytes(40))
        blocks.append(block)
        at += len(block)
    head = motec._HEAD.pack(motec.LD_MARKER, meta_ptr, data_ptr, 0, 1, 0x4240, 0xF, 12007, _s("ADL", 8), 420, 0xADB0,
                            len(channels), _s("08/10/2026", 16), _s("10:59:30", 16), _s(driver, 64), _s(vehicle, 64),
                            _s(venue, 64), 0xC81A4, _s("test log", 64))
    return head + b"".join(metas) + b"".join(blocks)


def ldx(beacons) -> str:
    markers = "\n".join(f'    <Marker Version="100" ClassName="BCN" Name="Manual.{i + 1}" Flags="77" Time="{b * 1e6:.6e}"/>'
                        for i, b in enumerate(beacons))
    return f'''<?xml version="1.0"?>
<LDXFile Locale="English_United States.1252" DefaultLocale="C" Version="1.6">
 <Layers>
  <Layer>
   <MarkerBlock>
    <MarkerGroup Name="Beacons" Index="3">
{markers}
    </MarkerGroup>
   </MarkerBlock>
   <RangeBlock/>
  </Layer>
 </Layers>
 <Details>
  <String Id="Total Laps" Value="{len(beacons) + 1}"/>
 </Details>
</LDXFile>
'''


# ── iRacing ───────────────────────────────────────────────────────────────────

IBT_YAML = """---
WeekendInfo:
 TrackName: synthetic gp
 TrackDisplayName: Synthetic Circuit
 TrackConfigName: Grand Prix
DriverInfo:
 DriverCarIdx: 3
 Drivers:
 - CarIdx: 0
   UserName: Pace Car
   CarScreenName: Safety Car
 - CarIdx: 3
   UserName: Test Driver
   CarScreenName: GT3 Car
SessionInfo:
 Sessions:
 - SessionNum: 0
   SessionType: Practice
CarSetup:
 UpdateCount: 6
 Chassis:
  Front:
   ArbSetting: 3
   NoseWeight: 53.2%
  LeftFront:
   CornerWeight: 3632 N
   SpringRate: 61 N/mm
  LeftRear:
   CornerWeight: 3206 N
   SpringRate: 53 N/mm
  InCarDials:
   AbsSetting: 2 (ABS)
  RightFront:
   CornerWeight: 3462 N
   SpringRate: 61 N/mm
  RightRear:
   CornerWeight: 3029 N
   SpringRate: 53 N/mm
...
"""
_IBT_TYPES = {"bool": (1, "?", 1), "int": (2, "<i4", 4), "float": (4, "<f4", 4), "double": (5, "<f8", 8)}


def ibt(variables: list, tick_rate: int) -> bytes:
    """An .ibt file. variables: [(name, type, unit, values)]; a 2-D `values`
    writes an array variable (one value per column)."""
    yaml = IBT_YAML.encode()
    n = len(variables[0][3])
    offset, headers, columns = 0, [], []
    for name, kind, unit, values in variables:
        code, fmt, size = _IBT_TYPES[kind]
        values = np.asarray(values)
        count = 1 if values.ndim == 1 else values.shape[1]
        headers.append(struct.pack("<iii?3x32s64s32s", code, offset, count, False, _s(name, 32), _s(name, 64), _s(unit, 32)))
        columns.append((offset, values.astype(fmt).reshape(n, count)))
        offset += size * count
    buf_len = offset
    var_off = 144
    info_off = var_off + 144 * len(variables)
    buf_off = info_off + len(yaml)
    rows = np.zeros((n, buf_len), np.uint8)
    for at, values in columns:
        raw = values.view(np.uint8).reshape(n, -1)
        rows[:, at:at + raw.shape[1]] = raw
    head = struct.pack("<12i", 2, 1, tick_rate, 0, len(yaml), info_off, len(variables), var_off, 1, buf_len, 0, 0)
    head += struct.pack("<4i", n, buf_off, 0, 0) + bytes(48)
    head += struct.pack("<qddii", 1_791_457_170, 0.0, n / tick_rate, 3, n)
    return head + b"".join(headers) + yaml + rows.tobytes()
