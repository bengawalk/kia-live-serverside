import datetime
import os
import json
import sys

import requests
import polyline
import urllib.parse
import geopy.distance

from src.model.universal_prediction import UniversalPredictionEngine

# Directory setup
GENERATED_IN_DIR = 'generated_in'
if not os.path.exists(GENERATED_IN_DIR):
    os.makedirs(GENERATED_IN_DIR)

# API endpoints and headers
SEARCH_ROUTE_URL = "https://bmtcmobileapi.karnataka.gov.in/WebAPI/SearchRoute_v2/"
ROUTE_DETAILS_URL = "https://bmtcmobileapi.karnataka.gov.in/WebAPI/SearchByRouteDetails_v4/"
GET_ALL_ROUTES_URL = "https://bmtcmobileapi.karnataka.gov.in/WebAPI/GetAllRouteList/"
ROUTE_POINTS_URL = "https://bmtcmobileapi.karnataka.gov.in/WebAPI/RoutePoints/"
SCHEDULE_URL = "https://bmtcmobileapi.karnataka.gov.in/WebAPI/GetTimetableByRouteid_v3/"
HEADERS = {
    'Accept': '*/*',
    'User-Agent': 'BrowsePy/1.0',
    'deviceType': 'WEB',
    'lan': 'en',
    'Content-Type': 'application/json'
}

# Function to query the API and return JSON data
def query_api(url, data=None):
    headers = HEADERS if data is not None else {'Content-Length': '0', **HEADERS}
    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 200:
        return response.json()
    else:
        print(data)
        print(url)
        print(headers)
        response.raise_for_status()
def save_child_routes():
    # Query the GetAllRouteList endpoint and filter for routes with routeno starting with "KIA-"
    all_routes_data = query_api(GET_ALL_ROUTES_URL)
    kia_routes = [route for route in all_routes_data['data'] if route['routeno'].startswith('KIA-')]

    # Save route children IDs to a JSON file
    route_children_ids = {route['routeno']: route['routeid'] for route in kia_routes}
    with open(os.path.join(GENERATED_IN_DIR, 'route_children_ids.json'), 'w') as f:
        json.dump(route_children_ids, f, indent=4)
    print("Route children IDs have been saved to JSON file.")

def save_route_parent_ids():
    # Read the 'in/routes_parent_ids.json' file
    with open('in/routes_parent_ids.json', 'r') as f:
        routes_parent_ids = json.load(f)

    # Read the 'generated_in/route_children_ids.json' file
    with open(os.path.join(GENERATED_IN_DIR, 'route_children_ids.json'), 'r') as f:
        route_children_ids = json.load(f)

    # Query the provided curl endpoint
    data = {"routetext": "KIA-"}
    response = query_api(SEARCH_ROUTE_URL, data=data)
    routes_data = response['data']

    # Create a dictionary to store the generated routeparentids
    generated_routeparentids = {}

    # Iterate over the routes_data and check for the presence of routeparentids
    for route in routes_data:
        routeno = route['routeno']
        routeparentid = route['routeparentid']

        # Add the required directions to the generated routeparentids
        for direction in [' UP', ' DOWN']:
            route_with_direction = routeno + direction
            if route_with_direction in route_children_ids:
                generated_routeparentids[route_with_direction] = routeparentid
            else:
                print(f"Warning: Route {route_with_direction} does not have a children id, skipping saving routeparentid.")

    # Save the generated routeparentids to a JSON file
    with open(os.path.join(GENERATED_IN_DIR, 'route_parent_ids.json'), 'w') as f:
        json.dump(generated_routeparentids, f, indent=4)

    print("Generated routeparentids have been saved to JSON file.")
def generate_client_stops():
    if '-s' not in sys.argv:
        print("Not saving stops, as -s flag is missing.")
        return
    # Read route IDs
    with open(os.path.join(GENERATED_IN_DIR, 'route_parent_ids.json'), 'r') as f:
        route_parent_ids = json.load(f)
    with open(os.path.join(GENERATED_IN_DIR, 'route_children_ids.json'), 'r') as f:
        route_children_ids = json.load(f)
    route_children = {y: x for x, y in route_children_ids.items()}
    # Read existing stops
    with open(os.path.join('in', 'client_stops.json'), 'r', encoding='utf-8') as f:
        client_stops_orig = json.load(f)
    orig_stop_loc = {stop['stop_id']: stop['loc'] for dist_stops in client_stops_orig.values() for stop in dist_stops['stops']}
    orig_kn = {stop['stop_id']: stop['name_kn'] for dist_stops in client_stops_orig.values() for stop in dist_stops['stops']}
    orig_phone = {}
    for dist_stops in client_stops_orig.values():
        for stop in dist_stops['stops']:
            if 'phone' in stop.keys() and not stop['phone'] == '':
                orig_phone[stop['stop_id']] = stop['phone']
            if 'contact' in stop.keys() and not stop['contact'] == '':
                orig_phone[stop['stop_id']] = stop['contact']
            elif stop['stop_id'] in orig_stop_loc:
                orig_stop_loc.pop(stop['stop_id'])

    client_stops = {}
    seen = set()
    for route, routeid in route_children_ids.items():
        routeparent = route_parent_ids[route]
        if routeparent in seen:
            continue
        seen.add(routeparent)
        data = {"routeid": routeparent, "servicetypeid": 0}
        api_response = query_api(ROUTE_DETAILS_URL, data)
        total_entries = []
        for d in ['up', 'down']:
            total_entries.extend(api_response[d]['data'])
        entries = {}
        for e in total_entries:
            if e['routeid'] not in entries:
                entries[e['routeid']] = []
            entries[e['routeid']].append(e)
        for k, v in entries.items():
            entries[k] = sorted(v, key=lambda x: x['distance_on_station'])
        # print(entries)
        for ch, stop_list in entries.items():
            total_distance = 0.0
            stops = []
            for stop_data in stop_list:
                if "FLY" in stop_data['stationname']: # Skip "FLY" stops, as these are flyover stops
                    continue
                stop = {
                    "distance": stop_data['distance_on_station'],
                    "name": stop_data['stationname'],
                    "name_kn": orig_kn[stop_data['stationid']] if stop_data['stationid'] in orig_kn else '',
                    "loc": orig_stop_loc[stop_data['stationid']] if stop_data['stationid'] in orig_stop_loc else [stop_data['centerlat'], stop_data['centerlong']],
                    "phone": orig_phone[stop_data['stationid']] if stop_data['stationid'] in orig_phone else '',
                    "stop_id": stop_data['stationid']
                }
                stops.append(stop)
                total_distance = max(total_distance, stop_data['distance_on_station'])
            client_stops[route_children[ch]] = {
                "stops": stops,
                "totalDistance": total_distance
            }
    with open(os.path.join(GENERATED_IN_DIR, 'client_stops.json'), 'w', encoding='utf-8') as f:
        json.dump(client_stops, f, indent=4, ensure_ascii=False)

    print("Client stops have been generated and saved to JSON file.")

def generate_google_polylines():
    if '-r' not in sys.argv:
        print('Skipping routelines.json, as -r flag is not provided.')
        return
    print("Saving routelines")
    with open(os.path.join(GENERATED_IN_DIR, 'route_children_ids.json'), 'r') as f:
        route_children_ids = json.load(f)
    polylines = {}
    for route, route_id in route_children_ids.items():
        response = query_api(ROUTE_POINTS_URL, {"routeid": route_id})
        if 'data' not in response:
            print(f"Skipping {route}")
            continue
        # Convert to float (lat, lon) tuples
        latlng = [(float(p["latitude"]), float(p["longitude"])) for p in response['data']]

        # Encode to polyline
        encoded_polyline = polyline.encode(latlng, geojson=True, precision=5)

        # Percent-encode the polyline
        percent_encoded = urllib.parse.quote(encoded_polyline, safe='')
        polylines[route] = percent_encoded

    with open(os.path.join(GENERATED_IN_DIR, 'routelines.json'), 'w') as f:
        json.dump(polylines, f, indent=4, ensure_ascii=False)

    print("Saved polylines to routelines.json")


def populate_distances():
    if '-r' not in sys.argv or '-s' not in sys.argv:
        return
    print("Populating missing distances with polyline + stop matches")


    def haversine(lat1, lon1, lat2, lon2):
        coords_1 = (lat1, lon1)
        coords_2 = (lat2, lon2)
        return geopy.distance.great_circle(coords_1, coords_2).km


    # Function to calculate the distance from a stop to the nearest point on the polyline
    def find_nearest_point_index(stop_lat, stop_lon, polyline):
        min_distance = float('inf')
        nearest_point_index = -1
        # Find the closest polyline point by calculating distance to each polyline point
        for i, (point_lat, point_lon) in enumerate(polyline):
            distance = haversine(stop_lat, stop_lon, point_lat, point_lon)
            if distance < min_distance:
                min_distance = distance
                nearest_point_index = i

        return nearest_point_index


    with open(os.path.join(GENERATED_IN_DIR, 'client_stops.json'), 'r') as f:
        client_stops = json.load(f)
    with open(os.path.join(GENERATED_IN_DIR, 'routelines.json'), 'r') as f:
        routelines = json.load(f)
    for routename, route in client_stops.items():
        if route['totalDistance'] != 0.0:
            continue
        if routename not in routelines:
            continue
        stops = route['stops']
        line = polyline.decode(urllib.parse.unquote(routelines[routename], encoding='utf-8', errors='replace'))
        distances = [0]  # Initialize first stop's distance as 0
        total_distance = 0
        for i in range(1, len(stops)):
            stop_a, stop_b = stops[i-1], stops[i]
            for j in range(find_nearest_point_index(stop_a['loc'][1], stop_a['loc'][0], line), find_nearest_point_index(stop_b['loc'][1], stop_b['loc'][0], line)):
                point_a = line[j]
                point_b = line[j + 1]

                # Calculate the distance between consecutive polyline points
                total_distance += haversine(point_a[0], point_a[1], point_b[0], point_b[1])

            distances.append(total_distance)  # Append the cumulative distance to the stop

        for i, stop in enumerate(stops):
            stop["distance"] = distances[i]
        route['totalDistance'] = distances[-1]
    with open(os.path.join(GENERATED_IN_DIR, 'client_stops.json'), 'w') as f:
        json.dump(client_stops, f, indent=4, ensure_ascii=False)
    print("Updated client_stops.json")



def generate_timings_tsv():
    if '-t' not in sys.argv:
        print('Skipping timings.tsv generation, as -t flag is not provided.')
        return
    print("Generating timings.tsv")
    with open(os.path.join(GENERATED_IN_DIR, 'route_children_ids.json'), 'r') as f:
        route_children_ids = json.load(f)
    time_rows = []
    for route, route_id in route_children_ids.items():
        times = {}
        times_maxed_duration = {'times': []}
        tmr = (datetime.datetime.now() + datetime.timedelta(days=1)).strftime('%Y-%m-%d')
        response = query_api(SCHEDULE_URL, {
            "routeid": route_id,
            "starttime": f"{tmr} 00:01",
            "endtime": f"{tmr} 23:59",
            "current_date": f"{tmr}"
        })
        if 'data' in response and len(response['data']) > 0:
            trips = response['data'][0]['tripdetails']
            for trip in trips:
                start = datetime.timedelta(hours=int(trip['starttime'].split(':')[0]), minutes=int(trip['starttime'].split(':')[1]))
                end = datetime.timedelta(hours=int(trip['endtime'].split(':')[0]), minutes=int(trip['endtime'].split(':')[1]))
                if end < start:
                    end += datetime.timedelta(days=1)

                duration = f"{(((end.days * 86400) + (end - start).seconds) // 3600):02}:{((((end.days * 86400) + (end - start).seconds) // 60) % 60):02}"
                duration_time = datetime.timedelta(hours=(((end.days * 86400) + (end - start).seconds) // 3600), minutes=((((end.days * 86400) + (end - start).seconds) // 60) % 60))
                times_maxed_duration['duration'] = max(duration_time, times_maxed_duration.get('duration', datetime.timedelta(seconds=0)))
                times_maxed_duration['times'].append(f"{(start.seconds // 3600):02}:{((start.seconds // 60) % 60):02}")
                # if duration not in times:
                #     times[duration] = []
                # times[duration].append(f"{(start.seconds // 3600):02}:{((start.seconds // 60) % 60):02}")
            times[f"{(times_maxed_duration['duration'].seconds // 3600):02}:{(((times_maxed_duration['duration'].seconds // 60) % 60)):02}"] = times_maxed_duration['times']
            for duration, starts in times.items():
                time_str = ""
                for start in starts:
                    time_str += f' {start}'
                time_rows.append({"duration": duration, "time": time_str.strip(), "route_no": route})
    with open(os.path.join(GENERATED_IN_DIR, 'timings.tsv'), 'w') as f:
        f.write("route_no\ttime\tduration\t\n") # Write headers
        for row in time_rows:
            f.write(f"{row['route_no']}\t{row['time']}\t{row['duration']}\t\n")
    print('Wrote timings from API to TSV file.')

def generate_times_json():
    if '-tdb' not in sys.argv:
        print("Skipping times.json generation, -tdb flag is not provided. Ensure universal model is trained before running with flag.")
        return

    print("Generating times.json using universal predictive model...")

    # Load required data
    with open(os.path.join(GENERATED_IN_DIR, 'client_stops.json'), 'r') as f:
        client_stops = json.load(f)
    with open(os.path.join(GENERATED_IN_DIR, 'route_children_ids.json'), 'r') as f:
        route_children_ids = json.load(f)
    with open(os.path.join(GENERATED_IN_DIR, 'timings.tsv'), 'r') as f:
        f.readline()  # Skip headers
        timings = {row.split('\t')[0]: [r for r in row.split('\t')[1].split(' ')] for row in f.readlines(-1)}

    # Filter timings for valid routes
    [timings.pop(r) if r not in route_children_ids else '' for r in timings.copy().keys()]
    [timings.pop(r) if r not in client_stops else '' for r in timings.copy().keys()]

    # Initialize universal prediction engine
    engine = UniversalPredictionEngine(model_dir="models")

    # Load model and route data
    success = engine.load_model_and_data(
        stops_data_path=os.path.join(GENERATED_IN_DIR, 'client_stops.json'),
        route_mapping_path=os.path.join(GENERATED_IN_DIR, 'route_children_ids.json')
    )

    if not success:
        print("ERROR: Failed to load universal model. Please train the model first using:")
        print("  poetry run python -m src.model.cli train-universal --vehicle-positions db/vehicle_positions.csv --stops-data in/client_stops.json")
        return

    # Get Wednesday's date for predictions (day of week = 2)
    # Start from today and find the next Wednesday
    today = datetime.datetime.now()
    days_until_wednesday = (2 - today.weekday()) % 7
    if days_until_wednesday == 0:
        # If today is Wednesday, use today
        wednesday_date = today
    else:
        wednesday_date = today + datetime.timedelta(days=days_until_wednesday)

    times = {}
    routename_by_id = {v: k for k, v in route_children_ids.items()}

    # Generate predictions for each route and trip start time
    for route_name, trip_starts in timings.items():
        route_id = route_children_ids[route_name]
        times[route_name] = []

        for trip_start_str in trip_starts:
            # Parse trip start time (HH:MM format)
            hour, minute = map(int, trip_start_str.split(':'))

            # Create timestamp for Wednesday at this time
            trip_datetime = wednesday_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
            trip_start_timestamp = trip_datetime.timestamp()

            # Predict trip times using universal model
            predictions = engine.predict_trip_from_start(
                route_id=route_id,
                start_time=trip_start_timestamp,
                start_position=None
            )

            if not predictions:
                print(f"Warning: No predictions generated for route {route_name} at {trip_start_str}")
                continue

            # Calculate duration (difference between last and first stop)
            start_timestamp = trip_start_timestamp
            end_timestamp = predictions[-1]['predicted_arrival_time']
            duration_seconds = end_timestamp - start_timestamp
            duration_minutes = duration_seconds / 60

            # Convert predictions to required format
            stops_output = []

            # First stop (start of trip)
            first_stop = client_stops[route_name]['stops'][0]
            stops_output.append({
                'stop_id': first_stop['stop_id'],
                'stop_loc': first_stop['loc'],
                'stop_time': int(f"{hour:02d}{minute:02d}"),  # HHMM format
                'confidence': None
            })

            # Remaining stops from predictions
            for pred in predictions:
                pred_datetime = datetime.datetime.fromtimestamp(pred['predicted_arrival_time'])
                stop_time_hhmm = int(f"{pred_datetime.hour:02d}{pred_datetime.minute:02d}")

                stops_output.append({
                    'stop_id': pred['stop_id'],
                    'stop_loc': [pred['latitude'], pred['longitude']],
                    'stop_time': stop_time_hhmm,
                    'confidence': None  # Universal model doesn't provide confidence scores
                })

            # Build output structure
            output = {
                'route_id': route_id,
                'duration': duration_minutes,
                'start': int(f"{hour:02d}{minute:02d}"),
                'stops': stops_output
            }
            times[route_name].append(output)

    # Write to file
    with open(os.path.join(GENERATED_IN_DIR, 'times.json'), 'w') as f:
        json.dump(times, f, indent=4, ensure_ascii=False)

    print(f'Written out times to times.json (predictions for Wednesday {wednesday_date.strftime("%Y-%m-%d")})')

# Remove routes that do not have timings information or are missing partial data
def clean_files():
    if '-s' not in sys.argv and '-t' not in sys.argv and '-tdb' not in sys.argv:
        print("Skipping clean, as no data files were generated.")
        return

    print("Cleaning files to remove routes with incomplete data...")

    # Load all generated files
    files_to_check = []
    timings_tsv_exists = False

    if os.path.exists(os.path.join(GENERATED_IN_DIR, 'client_stops.json')):
        files_to_check.append('client_stops.json')
    if os.path.exists(os.path.join(GENERATED_IN_DIR, 'route_children_ids.json')):
        files_to_check.append('route_children_ids.json')
    if os.path.exists(os.path.join(GENERATED_IN_DIR, 'route_parent_ids.json')):
        files_to_check.append('route_parent_ids.json')
    if os.path.exists(os.path.join(GENERATED_IN_DIR, 'routelines.json')):
        files_to_check.append('routelines.json')
    if os.path.exists(os.path.join(GENERATED_IN_DIR, 'times.json')):
        files_to_check.append('times.json')
    if os.path.exists(os.path.join(GENERATED_IN_DIR, 'timings.tsv')):
        timings_tsv_exists = True

    if not files_to_check and not timings_tsv_exists:
        print("No files to clean.")
        return

    # Load data from files
    data = {}
    for filename in files_to_check:
        with open(os.path.join(GENERATED_IN_DIR, filename), 'r', encoding='utf-8') as f:
            data[filename] = json.load(f)

    # Load timings.tsv if it exists and times.json doesn't exist (meaning -t was used but not -tdb)
    timings_from_tsv = {}
    if timings_tsv_exists and 'times.json' not in data:
        with open(os.path.join(GENERATED_IN_DIR, 'timings.tsv'), 'r') as f:
            f.readline()  # Skip header
            for line in f.readlines():
                parts = line.strip().split('\t')
                if len(parts) >= 1:
                    route_no = parts[0]
                    timings_from_tsv[route_no] = True

    # Determine which routes have complete data
    # A route is valid if it has:
    # 1. A child ID
    # 2. A parent ID
    # 3. Client stops with valid total distance
    # 4. Timing information (if times.json exists)

    valid_routes = set()
    if 'route_children_ids.json' in data:
        valid_routes = set(data['route_children_ids.json'].keys())

    # Filter by routes that have parent IDs
    if 'route_parent_ids.json' in data:
        valid_routes &= set(data['route_parent_ids.json'].keys())

    # Filter by routes that have stops with valid distances
    if 'client_stops.json' in data:
        routes_with_valid_stops = {
            route for route, stops_data in data['client_stops.json'].items()
            if stops_data.get('totalDistance', 0) > 0 and len(stops_data.get('stops', [])) > 0
        }
        valid_routes &= routes_with_valid_stops

    # Filter by routes that have timing information
    # Use times.json if it exists (when -tdb was used), otherwise use timings.tsv (when -t was used)
    if 'times.json' in data:
        routes_with_times = {
            route for route, times_list in data['times.json'].items()
            if len(times_list) > 0
        }
        valid_routes &= routes_with_times
    elif timings_from_tsv:
        # Use timings from TSV file when times.json doesn't exist
        routes_with_timings = set(timings_from_tsv.keys())
        valid_routes &= routes_with_timings

    # Remove invalid routes from all files
    removed_count = 0
    for filename, file_data in data.items():
        if filename == 'route_children_ids.json':
            original_count = len(file_data)
            data[filename] = {k: v for k, v in file_data.items() if k in valid_routes}
            removed_count += original_count - len(data[filename])
        elif filename == 'route_parent_ids.json':
            original_count = len(file_data)
            data[filename] = {k: v for k, v in file_data.items() if k in valid_routes}
            removed_count += original_count - len(data[filename])
        elif filename == 'client_stops.json':
            original_count = len(file_data)
            data[filename] = {k: v for k, v in file_data.items() if k in valid_routes}
            removed_count += original_count - len(data[filename])
        elif filename == 'routelines.json':
            original_count = len(file_data)
            data[filename] = {k: v for k, v in file_data.items() if k in valid_routes}
            removed_count += original_count - len(data[filename])
        elif filename == 'times.json':
            original_count = len(file_data)
            data[filename] = {k: v for k, v in file_data.items() if k in valid_routes}
            removed_count += original_count - len(data[filename])

    # Write cleaned data back to files
    for filename, file_data in data.items():
        with open(os.path.join(GENERATED_IN_DIR, filename), 'w', encoding='utf-8') as f:
            json.dump(file_data, f, indent=4, ensure_ascii=False)

    print(f"Cleaned files: removed {removed_count} incomplete route entries across all files.")
    print(f"Valid routes remaining: {len(valid_routes)}")

def copy_files_to_in():
    if '-c' not in sys.argv:
        print("Not copying / overwriting files to in directory, as -c flag is not provided.")
        return

    print("Copying generated files to in/ directory...")

    import shutil

    # Ensure destination directories exist
    os.makedirs('in/helpers/construct_stops', exist_ok=True)
    os.makedirs('in/helpers/construct_timings', exist_ok=True)

    files_copied = 0

    # client_stops.json -> in/client_stops.json && in/helpers/construct_stops/client_stops.json
    if os.path.exists(os.path.join(GENERATED_IN_DIR, 'client_stops.json')):
        shutil.copy2(
            os.path.join(GENERATED_IN_DIR, 'client_stops.json'),
            'in/client_stops.json'
        )
        shutil.copy2(
            os.path.join(GENERATED_IN_DIR, 'client_stops.json'),
            'in/helpers/construct_stops/client_stops.json'
        )
        files_copied += 1
        print("  ✓ Copied client_stops.json to in/ and in/helpers/construct_stops/")

    # route_children_ids.json -> in/routes_children_ids.json
    if os.path.exists(os.path.join(GENERATED_IN_DIR, 'route_children_ids.json')):
        shutil.copy2(
            os.path.join(GENERATED_IN_DIR, 'route_children_ids.json'),
            'in/routes_children_ids.json'
        )
        files_copied += 1
        print("  ✓ Copied route_children_ids.json to in/routes_children_ids.json")

    # route_parent_ids.json -> in/routes_parent_ids.json
    if os.path.exists(os.path.join(GENERATED_IN_DIR, 'route_parent_ids.json')):
        shutil.copy2(
            os.path.join(GENERATED_IN_DIR, 'route_parent_ids.json'),
            'in/routes_parent_ids.json'
        )
        files_copied += 1
        print("  ✓ Copied route_parent_ids.json to in/routes_parent_ids.json")

    # routelines.json -> in/routelines.json
    if os.path.exists(os.path.join(GENERATED_IN_DIR, 'routelines.json')):
        shutil.copy2(
            os.path.join(GENERATED_IN_DIR, 'routelines.json'),
            'in/routelines.json'
        )
        files_copied += 1
        print("  ✓ Copied routelines.json to in/routelines.json")

    # times.json -> in/times.json
    if os.path.exists(os.path.join(GENERATED_IN_DIR, 'times.json')):
        shutil.copy2(
            os.path.join(GENERATED_IN_DIR, 'times.json'),
            'in/times.json'
        )
        files_copied += 1
        print("  ✓ Copied times.json to in/times.json")

    # timings.tsv -> in/helpers/construct_timings/timings.tsv
    if os.path.exists(os.path.join(GENERATED_IN_DIR, 'timings.tsv')):
        shutil.copy2(
            os.path.join(GENERATED_IN_DIR, 'timings.tsv'),
            'in/helpers/construct_timings/timings.tsv'
        )
        files_copied += 1
        print("  ✓ Copied timings.tsv to in/helpers/construct_timings/timings.tsv")

    print(f"\nSuccessfully copied {files_copied} file(s) to in/ directory.")



# Save child route ids
save_child_routes()

# Save parent route ids
save_route_parent_ids()

# generate updated client_stops
generate_client_stops()

# generate polylines
generate_google_polylines()

# Populate distances where distances are missing from API response
populate_distances()

# Generate timings.tsv file
generate_timings_tsv()

# Generate times.json with stop-by-stop timings (in future
generate_times_json()

# Clean entries without timing info
clean_files()

# Copy / Overwrite relevant files in /in folder
copy_files_to_in()
