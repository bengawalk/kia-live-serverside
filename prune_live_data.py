import json
import os
import sqlite3
from math import radians, sin, cos, sqrt, atan2
from collections import defaultdict
from typing import Dict, List, Tuple, Optional


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IN_DIR = os.path.join(BASE_DIR, "in")
DB_IN_PATH = os.path.join(BASE_DIR, "db", "live_data_from_srv.db")
DB_OUT_PATH = os.path.join(BASE_DIR, "db", "live_data_pruned.db")


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c


def parse_hhmmss(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    try:
        parts = s.split(":")
        h, m, sec = int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
        return h * 3600 + m * 60 + sec
    except Exception:
        return None


def build_routeid_to_stopseq(in_dir: str) -> Dict[str, List[Tuple[str, float, float]]]:
    """
    Returns mapping: route_id (as string) -> ordered list of (stop_id, lat, lon)
    """
    routes_children = load_json(os.path.join(in_dir, "routes_children_ids.json"))
    client_stops = load_json(os.path.join(in_dir, "client_stops.json"))

    # invert: id -> key (keys like "KIA-9 UP")
    id_to_key = {str(v): k for k, v in routes_children.items()}

    routeid_to_seq: Dict[str, List[Tuple[str, float, float]]] = {}
    for route_id, route_key in id_to_key.items():
        stops_info = client_stops.get(route_key, {}).get("stops") or []
        # Expect stops sorted already by distance; if not, sort by 'distance' when available
        def sort_key(stop):
            d = stop.get("distance")
            try:
                return float(d)
            except Exception:
                return float("inf")

        if stops_info and not _is_sorted_by_distance(stops_info):
            stops_info = sorted(stops_info, key=sort_key)

        seq: List[Tuple[str, float, float]] = []
        for s in stops_info:
            sid = s.get("stop_id")
            loc = s.get("loc") or []
            if sid is None or len(loc) != 2:
                continue
            lat, lon = float(loc[0]), float(loc[1])
            seq.append((str(sid), lat, lon))
        if seq:
            routeid_to_seq[route_id] = seq
    return routeid_to_seq


def _is_sorted_by_distance(stops: List[dict]) -> bool:
    prev = -float("inf")
    for s in stops:
        d = s.get("distance")
        try:
            f = float(d)
        except Exception:
            return False
        if f < prev:
            return False
        prev = f
    return True


def trip_is_impossible(rows: List[dict], stop_order: Dict[str, int], stop_coords: Dict[str, Tuple[float, float]], max_kmh: float) -> bool:
    # Sort by stop sequence order; unknown stops go to the end in original order
    def seq_index(r):
        sid = r["stop_id"]
        return stop_order.get(sid, 10**9)

    rows_sorted = sorted(rows, key=seq_index)

    # Build time sequence (seconds) choosing actual departure then arrival
    times: List[Optional[int]] = []
    for r in rows_sorted:
        t = parse_hhmmss(r.get("actual_departure"))
        if t is None:
            t = parse_hhmmss(r.get("actual_arrival"))
        times.append(t)

    # Make cumulative non-decreasing times by adding 24h when needed
    cum_times: List[Optional[int]] = []
    rollover = 0
    prev: Optional[int] = None
    for t in times:
        if t is None:
            cum_times.append(None)
            continue
        if prev is None:
            cum_t = t
        else:
            cum_t = t + rollover
            if t is not None and prev is not None and t + rollover < prev:
                # add 24h until non-decreasing
                while t + rollover < prev:
                    rollover += 24 * 3600
                cum_t = t + rollover
        cum_times.append(cum_t)
        prev = cum_t

    # Evaluate segment speeds and minimum headway (>= 60s between consecutive stops)
    prev_sid: Optional[str] = None
    prev_time: Optional[int] = None
    for r, t in zip(rows_sorted, cum_times):
        sid = r["stop_id"]
        if sid not in stop_coords:
            prev_sid, prev_time = sid, t
            continue
        if prev_sid is None or prev_sid not in stop_coords:
            prev_sid, prev_time = sid, t
            continue
        if t is None or prev_time is None:
            # missing time for a segment → treat as impossible if distance > 0
            lat1, lon1 = stop_coords[prev_sid]
            lat2, lon2 = stop_coords[sid]
            if haversine_km(lat1, lon1, lat2, lon2) > 0.05:  # >50m with missing time is suspect
                return True
            prev_sid, prev_time = sid, t
            continue
        dt = t - prev_time
        lat1, lon1 = stop_coords[prev_sid]
        lat2, lon2 = stop_coords[sid]
        dist_km = haversine_km(lat1, lon1, lat2, lon2)
        if dist_km <= 0:
            prev_sid, prev_time = sid, t
            continue
        if dt <= 0:
            # non-positive time difference with distance
            return True
        if dt < 60:
            # less than 60 seconds between consecutive stops -> prune trip
            return True
        speed_kmh = dist_km / (dt / 3600.0)
        if speed_kmh > max_kmh:
            return True
        prev_sid, prev_time = sid, t
    return False


def main():
    max_speed_kmh = 95.0

    routeid_to_seq = build_routeid_to_stopseq(IN_DIR)
    # Convenient maps for lookup
    routeid_to_order: Dict[str, Dict[str, int]] = {}
    routeid_to_coords: Dict[str, Dict[str, Tuple[float, float]]] = {}
    for rid, seq in routeid_to_seq.items():
        routeid_to_order[rid] = {sid: idx for idx, (sid, _lat, _lon) in enumerate(seq)}
        routeid_to_coords[rid] = {sid: (lat, lon) for sid, lat, lon in seq}

    # Prepare output DB
    if os.path.exists(DB_OUT_PATH):
        os.remove(DB_OUT_PATH)
    out_conn = sqlite3.connect(DB_OUT_PATH)
    out_cur = out_conn.cursor()
    out_cur.execute(
        """
        CREATE TABLE IF NOT EXISTS completed_stop_times (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stop_id TEXT,
            trip_id TEXT,
            route_id TEXT,
            date TEXT,
            actual_arrival TEXT,
            actual_departure TEXT,
            scheduled_arrival TEXT,
            scheduled_departure TEXT,
            UNIQUE(stop_id, trip_id, date)
        );
        """
    )
    out_conn.commit()

    # Read input DB and group by (route_id, trip_id, date)
    in_conn = sqlite3.connect(DB_IN_PATH)
    in_conn.row_factory = sqlite3.Row
    in_cur = in_conn.cursor()

    in_cur.execute(
        "SELECT stop_id, trip_id, route_id, date, actual_arrival, actual_departure, scheduled_arrival, scheduled_departure FROM completed_stop_times"
    )

    groups: Dict[Tuple[str, str, str], List[dict]] = defaultdict(list)
    for row in in_cur:
        d = {k: row[k] for k in row.keys()}
        key = (str(d["route_id"]), d["trip_id"], d["date"])
        groups[key].append(d)

    kept = 0
    dropped = 0
    to_insert: List[Tuple] = []

    for (route_id, trip_id, date), rows in groups.items():
        order = routeid_to_order.get(route_id)
        coords = routeid_to_coords.get(route_id)
        if not order or not coords:
            # If we don't know the stop order/coords for this route, keep the data as-is
            for r in rows:
                to_insert.append(
                    (
                        r["stop_id"],
                        r["trip_id"],
                        route_id,
                        r["date"],
                        r.get("actual_arrival"),
                        r.get("actual_departure"),
                        r.get("scheduled_arrival"),
                        r.get("scheduled_departure"),
                    )
                )
            kept += len(rows)
            continue

        is_bad = trip_is_impossible(rows, order, coords, max_speed_kmh)
        if is_bad:
            dropped += len(rows)
            continue
        for r in rows:
            to_insert.append(
                (
                    r["stop_id"],
                    r["trip_id"],
                    route_id,
                    r["date"],
                    r.get("actual_arrival"),
                    r.get("actual_departure"),
                    r.get("scheduled_arrival"),
                    r.get("scheduled_departure"),
                )
            )
        kept += len(rows)

    if to_insert:
        out_cur.executemany(
            """
            INSERT OR IGNORE INTO completed_stop_times (
                stop_id, trip_id, route_id, date,
                actual_arrival, actual_departure,
                scheduled_arrival, scheduled_departure
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            to_insert,
        )
        out_conn.commit()

    print(f"Kept rows: {kept}")
    print(f"Dropped rows: {dropped}")

    in_conn.close()
    out_conn.close()


if __name__ == "__main__":
    main()


