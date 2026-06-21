#!/usr/bin/env python3
"""Dev-only: dump raw SEGB structure to understand the framing before writing the decoder.

Not part of the shipped tool. Reads a .segb sample and prints header bytes, where the
'SEGB' magic sits, and the first readable ASCII strings (bundle ids) with the bytes
around them so we can read the protobuf field tags by eye.
"""
import sys, re, struct

path = sys.argv[1]
data = open(path, "rb").read()
print(f"file: {path}  size: {len(data)}")
print(f"first 16 bytes: {data[:16]!r}")

# Where does 'SEGB' appear?
offs = [m.start() for m in re.finditer(b"SEGB", data)]
print(f"'SEGB' magic at offsets: {offs[:10]}{' ...' if len(offs)>10 else ''}  (count={len(offs)})")

# Trailing magic: SEGB v1 has header at start; some variants have it at end.
print(f"last 16 bytes: {data[-16:]!r}")

# First readable strings + surrounding bytes
print("\n=== first 12 ASCII runs (len>=3) with context ===")
count = 0
for m in re.finditer(rb"[\x20-\x7e]{4,}", data):
    s = m.group()
    if not re.match(rb"(com|net|org|ai|io|app)\.", s):
        continue
    start, end = m.start(), m.end()
    before = data[max(0, start-8):start]
    after = data[end:end+12]
    print(f"@{start:6d} before={before!r:30} STR={s.decode():35} after={after!r}")
    count += 1
    if count >= 12:
        break
