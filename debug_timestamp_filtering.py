"""
Debug script to verify timestamp filtering logic.
Checks why 4-day-old positions might be passing through the 12-hour filter.
"""

import sqlite3
from datetime import datetime, timedelta
import pytz

local_tz = pytz.timezone("Asia/Kolkata")

db_path = "db/database.db"

def main():
    print("=" * 80)
    print("TIMESTAMP FILTERING DEBUG")
    print("=" * 80)

    # Calculate cutoff timestamp (12 hours ago)
    now = datetime.now()
    max_age_hours = 12
    cutoff_datetime = now - timedelta(hours=max_age_hours)
    cutoff_timestamp = int(cutoff_datetime.timestamp())

    print(f"\nCurrent time: {now}")
    print(f"Cutoff datetime (12h ago): {cutoff_datetime}")
    print(f"Cutoff timestamp: {cutoff_timestamp}")

    # Connect to database
    conn = sqlite3.connect(db_path, timeout=10.0)
    cursor = conn.cursor()

    # Query trip 2995_13 positions (the problematic one)
    trip_id = "2995_13"
    route_id = "2995"

    print(f"\n{'=' * 80}")
    print(f"Query 1: ALL positions for trip {trip_id} (no time filter)")
    print(f"{'=' * 80}")

    cursor.execute('''
        SELECT vehicle_id, trip_id, route_id, lat, lon, timestamp
        FROM vehicle_positions
        WHERE trip_id = ? AND route_id = ?
        ORDER BY timestamp ASC
    ''', (trip_id, route_id))

    all_positions = cursor.fetchall()
    print(f"Total positions found: {len(all_positions)}")

    if all_positions:
        print(f"\nFirst 5 positions:")
        for pos in all_positions[:5]:
            vehicle_id, trip_id_db, route_id_db, lat, lon, ts = pos
            dt = datetime.fromtimestamp(ts, local_tz)
            age_hours = (now.timestamp() - ts) / 3600
            print(f"  Vehicle: {vehicle_id} | Timestamp: {ts} | DateTime: {dt} | Age: {age_hours:.1f}h")

        print(f"\nLast 5 positions:")
        for pos in all_positions[-5:]:
            vehicle_id, trip_id_db, route_id_db, lat, lon, ts = pos
            dt = datetime.fromtimestamp(ts, local_tz)
            age_hours = (now.timestamp() - ts) / 3600
            print(f"  Vehicle: {vehicle_id} | Timestamp: {ts} | DateTime: {dt} | Age: {age_hours:.1f}h")

    print(f"\n{'=' * 80}")
    print(f"Query 2: Positions WITH 12-hour filter (timestamp >= {cutoff_timestamp})")
    print(f"{'=' * 80}")

    cursor.execute('''
        SELECT vehicle_id, trip_id, route_id, lat, lon, timestamp
        FROM vehicle_positions
        WHERE trip_id = ? AND route_id = ? AND timestamp >= ?
        ORDER BY timestamp ASC
    ''', (trip_id, route_id, cutoff_timestamp))

    filtered_positions = cursor.fetchall()
    print(f"Positions after filter: {len(filtered_positions)}")

    if filtered_positions:
        print(f"\nAll filtered positions:")
        for pos in filtered_positions:
            vehicle_id, trip_id_db, route_id_db, lat, lon, ts = pos
            dt = datetime.fromtimestamp(ts, local_tz)
            age_hours = (now.timestamp() - ts) / 3600
            passed_filter = "✓ PASS" if ts >= cutoff_timestamp else "✗ FAIL"
            print(f"  {passed_filter} | Vehicle: {vehicle_id} | Timestamp: {ts} | DateTime: {dt} | Age: {age_hours:.1f}h")

    # Check a few other trips to see the pattern
    print(f"\n{'=' * 80}")
    print(f"Query 3: Sample of ALL trip_ids with position counts")
    print(f"{'=' * 80}")

    cursor.execute('''
        SELECT trip_id, COUNT(*) as count, MIN(timestamp) as oldest, MAX(timestamp) as newest
        FROM vehicle_positions
        GROUP BY trip_id
        ORDER BY count DESC
        LIMIT 10
    ''')

    trip_stats = cursor.fetchall()
    print(f"\nTop 10 trips by position count:")
    for trip_id_db, count, oldest_ts, newest_ts in trip_stats:
        oldest_dt = datetime.fromtimestamp(oldest_ts, local_tz)
        newest_dt = datetime.fromtimestamp(newest_ts, local_tz)
        oldest_age = (now.timestamp() - oldest_ts) / 3600
        newest_age = (now.timestamp() - newest_ts) / 3600
        span_hours = (newest_ts - oldest_ts) / 3600
        print(f"  Trip: {trip_id_db}")
        print(f"    Positions: {count}")
        print(f"    Oldest: {oldest_dt} (age: {oldest_age:.1f}h)")
        print(f"    Newest: {newest_dt} (age: {newest_age:.1f}h)")
        print(f"    Span: {span_hours:.1f}h")
        print()

    # Check if there are positions older than 24 hours
    print(f"\n{'=' * 80}")
    print(f"Query 4: Count of positions by age bucket")
    print(f"{'=' * 80}")

    cutoff_1h = int((now - timedelta(hours=1)).timestamp())
    cutoff_4h = int((now - timedelta(hours=4)).timestamp())
    cutoff_12h = int((now - timedelta(hours=12)).timestamp())
    cutoff_24h = int((now - timedelta(hours=24)).timestamp())
    cutoff_7d = int((now - timedelta(days=7)).timestamp())

    cursor.execute('SELECT COUNT(*) FROM vehicle_positions WHERE timestamp >= ?', (cutoff_1h,))
    count_1h = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM vehicle_positions WHERE timestamp >= ?', (cutoff_4h,))
    count_4h = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM vehicle_positions WHERE timestamp >= ?', (cutoff_12h,))
    count_12h = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM vehicle_positions WHERE timestamp >= ?', (cutoff_24h,))
    count_24h = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM vehicle_positions WHERE timestamp >= ?', (cutoff_7d,))
    count_7d = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM vehicle_positions')
    count_total = cursor.fetchone()[0]

    print(f"Positions in last 1 hour:  {count_1h:6d}")
    print(f"Positions in last 4 hours: {count_4h:6d}")
    print(f"Positions in last 12 hours: {count_12h:6d}")
    print(f"Positions in last 24 hours: {count_24h:6d}")
    print(f"Positions in last 7 days:  {count_7d:6d}")
    print(f"Total positions:           {count_total:6d}")
    print(f"\nPositions OLDER than 7 days: {count_total - count_7d}")

    conn.close()

    print(f"\n{'=' * 80}")
    print("ANALYSIS")
    print(f"{'=' * 80}")

    if all_positions and len(all_positions) > len(filtered_positions):
        print(f"\n✓ Filter IS working: {len(all_positions)} total → {len(filtered_positions)} after 12h filter")
        print(f"  Filtered out: {len(all_positions) - len(filtered_positions)} old positions")
    elif all_positions and len(all_positions) == len(filtered_positions):
        print(f"\n⚠ Filter had NO effect: All {len(all_positions)} positions passed the filter")
        print(f"  This means ALL positions for this trip are within 12 hours")
    else:
        print(f"\n⚠ No positions found for trip {trip_id}")

    if count_total - count_7d > 0:
        print(f"\n⚠ WARNING: Database contains {count_total - count_7d} positions older than 7 days")
        print(f"  These should have been cleaned up by the cleanup task")

if __name__ == "__main__":
    main()