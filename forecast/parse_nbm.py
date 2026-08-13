#!/usr/bin/env python3
"""Parse NBM text collectives (NBH/NBS/NBE) into tidy per-station rows.

Column layout is derived from each block's FHR row token spans, so the same
parser handles all three products. Units normalized here, once:
  vis_sm   statute miles (source: tenths, 100 = 10 mi cap)
  ceil_ft  feet (source: hundreds; -88 = unlimited -> None)
  ifc/ifv  percent probabilities as given
"""
import re
from pathlib import Path

HEADER_RE = re.compile(
    r"^ ([A-Z0-9]{3,5})\s+NBM V[\d.]+ (NBH|NBS|NBE) GUIDANCE\s+"
    r"(\d+)/(\d+)/(\d{4})\s+(\d{4}) UTC")
ROWS = ("FHR", "UTC", "TMP", "DPT", "WSP", "CIG", "IFC", "VIS", "IFV", "LIC", "LIV")


def parse_collective(path: Path, stations: set[str] | None = None):
    """Yield dicts: {icao, product, cycle_iso, fhr, tmp, dpt, wsp, vis_sm, ceil_ft, ifc, ifv}"""
    block_icao = None
    rows: dict[str, str] = {}
    meta = None

    def flush():
        nonlocal rows, meta
        if not meta or ("FHR" not in rows and "UTC" not in rows):
            rows = {}; meta = None
            return
        icao, product, mo, dy, yr, hhmm = meta
        # NBE separates days with '|' — blank them uniformly so column spans align
        rows = {k: v[:4] + v[4:].replace("|", " ") for k, v in rows.items()}
        axis = rows.get("FHR") or rows["UTC"]
        spans = [(m.start(), m.end()) for m in re.finditer(r"\S+", axis[4:])]
        def vals(key):
            line = rows.get(key)
            if line is None:
                return [None] * len(spans)
            body = line[4:]
            out = []
            for a, b in spans:
                tok = body[max(0, a - 2):b].split()
                out.append(tok[-1] if tok else None)
            return out
        cols = {k: vals(k) for k in ROWS}
        cycle = f"{yr}-{int(mo):02d}-{int(dy):02d}T{hhmm[:2]}:00:00"
        # NBH has no FHR row — derive lead hours from the UTC row (2-digit
        # hours, rolling past midnight)
        fhrs: list[int | None] = []
        if rows.get("FHR"):
            for v in cols["FHR"]:
                try: fhrs.append(int(v))
                except (TypeError, ValueError): fhrs.append(None)
        else:
            cyc_hr, prev, day = int(hhmm[:2]), None, 0
            for v in cols["UTC"]:
                try: h = int(v)
                except (TypeError, ValueError): fhrs.append(None); continue
                if prev is not None and h < prev: day += 1
                prev = h
                fhrs.append(h + 24 * day - cyc_hr if h + 24 * day >= cyc_hr else h + 24 * (day + 1) - cyc_hr)
        for i in range(len(spans)):
            fhr = fhrs[i]
            if fhr is None:
                continue
            def num(key):
                v = cols[key][i]
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
            vis = num("VIS")
            cig = num("CIG")
            yield_rows.append({
                "icao": icao, "product": product, "cycle": cycle, "fhr": fhr,
                "tmp": num("TMP"), "dpt": num("DPT"), "wsp": num("WSP"),
                "vis_sm": None if vis is None else vis / 10.0,
                "ceil_ft": None if cig is None or cig < 0 else cig * 100.0,
                "ifc": num("IFC"), "ifv": num("IFV"),
                "lic": num("LIC"), "liv": num("LIV"),
            })
        rows = {}; meta = None

    yield_rows: list[dict] = []
    for line in open(path, encoding="latin-1"):
        m = HEADER_RE.match(line)
        if m:
            flush()
            icao = m.group(1)
            if stations is None or icao in stations:
                meta = m.groups()
                block_icao = icao
            else:
                meta = None
                block_icao = None
            continue
        if meta and block_icao:
            key = line[1:4].strip() if len(line) > 4 else ""
            key4 = line[1:5].strip()
            k = key4 if key4 in ROWS else key if key in ROWS else None
            if k:
                rows[k] = line.rstrip("\n")
    flush()
    return yield_rows


if __name__ == "__main__":
    import json, sys
    stations = set(json.load(open(Path(__file__).parent / "stations.json")))
    out = parse_collective(Path(sys.argv[1]), stations)
    print(f"{len(out)} rows from {len({r['icao'] for r in out})} stations")
    for r in out[:3]:
        print(r)
