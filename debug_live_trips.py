"""
Debug script to analyze live GTFS-RT feed from running service.
Validates temporal ordering and identifies issues in trip predictions.
"""

import requests
from google.transit import gtfs_realtime_pb2
from datetime import datetime
import pytz

local_tz = pytz.timezone("Asia/Kolkata")

# Service endpoint
GTFS_RT_URL = "http://localhost:59966/gtfs-rt.proto"

def format_timestamp(ts):
    """Convert unix timestamp to readable time"""
    if ts:
        dt = datetime.fromtimestamp(ts, local_tz)
        return dt.strftime("%H:%M:%S")
    return "N/A"

def analyze_trip_temporal_order(trip_update):
    """Analyze a single trip for temporal ordering issues and pattern classification"""
    issues = []
    stop_times = []

    trip_id = trip_update.trip.trip_id
    route_id = trip_update.trip.route_id

    prev_arrival = None
    prev_departure = None
    prev_seq = None

    for i, stu in enumerate(trip_update.stop_time_update):
        stop_id = stu.stop_sequence if stu.stop_sequence else i

        arrival_time = stu.arrival.time if stu.HasField('arrival') and stu.arrival.time else None
        departure_time = stu.departure.time if stu.HasField('departure') and stu.departure.time else None

        arrival_delay = stu.arrival.delay if stu.HasField('arrival') and stu.arrival.HasField('delay') else None
        departure_delay = stu.departure.delay if stu.HasField('departure') and stu.departure.HasField('delay') else None

        # Check temporal violations
        if arrival_time:
            if prev_departure and arrival_time < prev_departure:
                issues.append({
                    'type': 'arrival_before_prev_departure',
                    'stop_seq': i + 1,
                    'stop_id': stu.stop_id,
                    'arrival': arrival_time,
                    'prev_departure': prev_departure,
                    'delta_seconds': arrival_time - prev_departure,
                    'delta_minutes': (arrival_time - prev_departure) / 60
                })

            if prev_arrival and arrival_time < prev_arrival:
                issues.append({
                    'type': 'arrival_before_prev_arrival',
                    'stop_seq': i + 1,
                    'stop_id': stu.stop_id,
                    'arrival': arrival_time,
                    'prev_arrival': prev_arrival,
                    'delta_seconds': arrival_time - prev_arrival,
                    'delta_minutes': (arrival_time - prev_arrival) / 60
                })

        if departure_time and arrival_time:
            if departure_time < arrival_time:
                issues.append({
                    'type': 'departure_before_arrival',
                    'stop_seq': i + 1,
                    'stop_id': stu.stop_id,
                    'arrival': arrival_time,
                    'departure': departure_time,
                    'delta_seconds': departure_time - arrival_time,
                    'delta_minutes': (departure_time - arrival_time) / 60
                })

        stop_times.append({
            'seq': i + 1,
            'stop_id': stu.stop_id,
            'arrival': arrival_time,
            'departure': departure_time,
            'arrival_delay': arrival_delay,
            'departure_delay': departure_delay
        })

        if arrival_time:
            prev_arrival = arrival_time
        if departure_time:
            prev_departure = departure_time
        prev_seq = i

    # Analyze temporal progression pattern
    deltas = []
    backwards_count = 0
    forwards_count = 0
    large_jumps = []

    for i in range(1, len(stop_times)):
        prev = stop_times[i-1]
        curr = stop_times[i]

        if prev['arrival'] and curr['arrival']:
            delta = curr['arrival'] - prev['arrival']
            deltas.append(delta)

            if delta < 0:
                backwards_count += 1
                # Track large backwards jumps (> 5 minutes)
                if delta < -300:
                    large_jumps.append({
                        'from_seq': i,
                        'to_seq': i + 1,
                        'from_stop': prev['stop_id'],
                        'to_stop': curr['stop_id'],
                        'from_time': prev['arrival'],
                        'to_time': curr['arrival'],
                        'delta_minutes': delta / 60
                    })
            else:
                forwards_count += 1

    # Determine pattern type
    backwards_percentage = (backwards_count / len(deltas)) * 100 if deltas else 0

    if backwards_percentage > 75:
        pattern_type = "ENTIRE_TRIP_BACKWARDS"
    elif backwards_percentage > 10:
        pattern_type = "MIXED_WITH_JUMPS"
    else:
        pattern_type = "MOSTLY_FORWARD"

    return {
        'trip_id': trip_id,
        'route_id': route_id,
        'stop_count': len(stop_times),
        'stops': stop_times,
        'issues': issues,
        'pattern_type': pattern_type,
        'backwards_percentage': backwards_percentage,
        'total_deltas': len(deltas),
        'backwards_count': backwards_count,
        'forwards_count': forwards_count,
        'large_jumps': large_jumps
    }

def main():
    print("="*80)
    print("GTFS-RT Feed Temporal Validation")
    print("="*80)

    # Fetch the GTFS-RT feed
    try:
        print(f"\nFetching feed from: {GTFS_RT_URL}")
        response = requests.get(GTFS_RT_URL, timeout=10)
        response.raise_for_status()
        print(f"✓ Feed fetched successfully ({len(response.content)} bytes)")
    except Exception as e:
        print(f"✗ Failed to fetch feed: {e}")
        return

    # Parse the protobuf
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(response.content)
        print(f"✓ Feed parsed successfully")
        print(f"  Feed timestamp: {format_timestamp(feed.header.timestamp)}")
        print(f"  Total entities: {len(feed.entity)}")
    except Exception as e:
        print(f"✗ Failed to parse feed: {e}")
        return

    # Analyze each trip
    print(f"\n{'='*80}")
    print("Analyzing trips...")
    print(f"{'='*80}\n")

    trip_analyses = []
    trips_with_issues = []

    for entity in feed.entity:
        if entity.HasField('trip_update'):
            analysis = analyze_trip_temporal_order(entity.trip_update)
            trip_analyses.append(analysis)

            if analysis['issues']:
                trips_with_issues.append(analysis)

    # Categorize trips by pattern
    backwards_trips = []
    jump_trips = []
    clean_trips = []

    for analysis in trip_analyses:
        if analysis.get('pattern_type') == "ENTIRE_TRIP_BACKWARDS":
            backwards_trips.append(analysis)
        elif analysis.get('pattern_type') == "MIXED_WITH_JUMPS":
            jump_trips.append(analysis)
        else:
            clean_trips.append(analysis)

    # Print summary
    print(f"SUMMARY:")
    print(f"  Total trips analyzed: {len(trip_analyses)}")
    print(f"  Trips with temporal issues: {len(trips_with_issues)}")
    print(f"  Clean trips: {len(clean_trips)}")
    print(f"\nPATTERN BREAKDOWN:")
    print(f"  Case 1 - Entire trip backwards: {len(backwards_trips)}")
    print(f"  Case 2 - Forward with jumps: {len(jump_trips)}")
    print(f"  No violations: {len(clean_trips)}")

    # Case 1: Entire trip backwards
    if backwards_trips:
        print(f"\n{'='*80}")
        print("CASE 1: ENTIRE TRIP RUNNING BACKWARDS")
        print(f"{'='*80}")
        print("These trips appear to be progressing from last stop to first stop.")
        print("This suggests a route direction mismatch or reverse trip assignment.\n")

        for analysis in backwards_trips[:5]:  # Show first 5
            print(f"Trip: {analysis['trip_id']} | Route: {analysis['route_id']}")
            print(f"  Pattern: {analysis['backwards_percentage']:.1f}% backwards ({analysis['backwards_count']}/{analysis['total_deltas']} deltas)")
            print(f"  Stops: {analysis['stop_count']} | Issues: {len(analysis['issues'])}")

            print(f"\n  Time Progression (first 15 stops):")
            for stop in analysis['stops'][:15]:
                arr_str = format_timestamp(stop['arrival']) if stop['arrival'] else "N/A"
                print(f"    {stop['seq']:<3} | {stop['stop_id']:<7} | {arr_str:<10}")

            print(f"\n{'-'*80}\n")

        if len(backwards_trips) > 5:
            print(f"... and {len(backwards_trips) - 5} more backwards trips\n")

    # Case 2: Forward with jumps
    if jump_trips:
        print(f"{'='*80}")
        print("CASE 2: FORWARD TRIP WITH TEMPORAL JUMPS")
        print(f"{'='*80}")
        print("These trips are generally progressing forward but have large backwards jumps.")
        print("This suggests prediction/forward-fill issues at specific stops.\n")

        for analysis in jump_trips[:5]:  # Show first 5
            print(f"Trip: {analysis['trip_id']} | Route: {analysis['route_id']}")
            print(f"  Pattern: {analysis['backwards_percentage']:.1f}% backwards ({analysis['backwards_count']}/{analysis['total_deltas']} deltas)")
            print(f"  Stops: {analysis['stop_count']} | Large jumps (>5min): {len(analysis['large_jumps'])}")

            if analysis['large_jumps']:
                print(f"\n  Large Jump Locations:")
                for jump in analysis['large_jumps'][:3]:  # Show first 3 jumps
                    print(f"    Stop {jump['from_seq']} ({jump['from_stop']}) at {format_timestamp(jump['from_time'])}")
                    print(f"      → Stop {jump['to_seq']} ({jump['to_stop']}) at {format_timestamp(jump['to_time'])}")
                    print(f"      JUMP: {jump['delta_minutes']:.1f} minutes backwards")

                # Show timeline around first jump
                first_jump = analysis['large_jumps'][0]
                jump_seq = first_jump['from_seq']

                print(f"\n  Timeline around first jump:")
                start_idx = max(0, jump_seq - 4)
                end_idx = min(len(analysis['stops']), jump_seq + 4)
                for stop in analysis['stops'][start_idx:end_idx]:
                    arr_str = format_timestamp(stop['arrival']) if stop['arrival'] else "N/A"
                    marker = " ← JUMP HERE" if stop['seq'] == jump_seq + 1 else ""
                    print(f"    {stop['seq']:<3} | {stop['stop_id']:<7} | {arr_str:<10}{marker}")

            print(f"\n{'-'*80}\n")

        if len(jump_trips) > 5:
            print(f"... and {len(jump_trips) - 5} more trips with jumps\n")

    if not trips_with_issues:
        print(f"\n✅ No temporal violations found! All trips have properly ordered stop times.")

    # Recommendations
    if backwards_trips or jump_trips:
        print(f"{'='*80}")
        print("RECOMMENDATIONS")
        print(f"{'='*80}\n")

        if backwards_trips:
            print(f"Case 1 - Entire Trip Backwards ({len(backwards_trips)} trips):")
            print(f"  → Check route direction assignment in trip_scheduler.py")
            print(f"  → Verify route_id mapping to direction (UP/DOWN)")
            print(f"  → Ensure vehicle-to-trip matching considers direction")
            print()

        if jump_trips:
            print(f"Case 2 - Forward with Jumps ({len(jump_trips)} trips):")
            print(f"  → Fix forward-fill logic in stop_time_detector.py")
            print(f"  → Add temporal validation to prevent backwards jumps")
            print(f"  → Improve prediction fallback logic")
            print()

    # Find the specific trip mentioned by user (1864_58)
    print(f"\n{'='*80}")
    print("Searching for trip 1864_58...")
    print(f"{'='*80}\n")

    found = False
    for analysis in trip_analyses:
        if '1864' in analysis['trip_id'] and '58' in analysis['trip_id']:
            found = True
            print(f"Found trip: {analysis['trip_id']}")
            print(f"  Route: {analysis['route_id']}")
            print(f"  Stops: {analysis['stop_count']}")
            print(f"  Issues: {len(analysis['issues'])}")

            print(f"\n  Complete stop timeline:")
            for stop in analysis['stops']:
                arr_str = format_timestamp(stop['arrival']) if stop['arrival'] else "N/A"
                dep_str = format_timestamp(stop['departure']) if stop['departure'] else "N/A"
                print(f"    {stop['seq']:<3} | {stop['stop_id']:<7} | Arr: {arr_str:<10} | Dep: {dep_str:<10}")

            if analysis['issues']:
                print(f"\n  Issues found:")
                for issue in analysis['issues']:
                    print(f"    {issue}")
            else:
                print(f"\n  ✅ No issues found in this trip")
            break

    if not found:
        print("  Trip 1864_58 not found in current feed")

if __name__ == "__main__":
    main()