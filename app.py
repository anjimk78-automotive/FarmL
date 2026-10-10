import hashlib
import hmac
import math
import os
import secrets
from datetime import datetime, timezone

import folium
import gspread
import pandas as pd
import requests
import streamlit as st
from folium.features import DivIcon
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from streamlit_folium import st_folium


st.set_page_config(
    page_title="Farm Route Optimizer",
    page_icon="🗺️",
    layout="wide",
    initial_sidebar_state="expanded",
)

OSRM_BASE_URL = "https://router.project-osrm.org"
REQUEST_TIMEOUT = 45
MAX_SELECTED_FARMS = 25
FARMS_HEADERS = ["Farm Name", "Latitude", "Longitude"]
USERS_HEADERS = ["Username", "Password Hash", "Role", "Active", "Created At"]


# ---------------------------- Secrets / Google Sheets ----------------------------

def secret_value(*keys, default=None):
    """Read a nested Streamlit secret using a sequence of keys."""
    value = st.secrets
    try:
        for key in keys:
            value = value[key]
        return value
    except Exception:
        return default


def get_gspread_client():
    """Authenticate with a Google service account configured in Streamlit Secrets."""
    try:
        service_account_info = dict(st.secrets["gcp_service_account"])
    except Exception as exc:
        raise RuntimeError(
            "Missing [gcp_service_account] credentials in Streamlit Secrets. "
            "See README.md for setup instructions."
        ) from exc

    try:
        return gspread.service_account_from_dict(service_account_info)
    except Exception as exc:
        raise RuntimeError(f"Could not authenticate with Google Sheets: {exc}") from exc


@st.cache_resource
def get_spreadsheet():
    sheet_id = secret_value("app", "spreadsheet_id")
    if not sheet_id:
        raise RuntimeError(
            "Missing app.spreadsheet_id in Streamlit Secrets. See README.md."
        )
    client = get_gspread_client()
    return client.open_by_key(str(sheet_id))


def get_or_create_worksheet(spreadsheet, title, headers, rows=1000, cols=10):
    try:
        worksheet = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)
        worksheet.append_row(headers, value_input_option="USER_ENTERED")
    values = worksheet.get_all_values()
    if not values:
        worksheet.append_row(headers, value_input_option="USER_ENTERED")
    return worksheet


def load_farms():
    spreadsheet = get_spreadsheet()
    worksheet = get_or_create_worksheet(
        spreadsheet, "Farms", FARMS_HEADERS, rows=2000, cols=6
    )
    records = worksheet.get_all_records()
    if not records:
        return pd.DataFrame(columns=FARMS_HEADERS)

    # Normalize header spacing/case while preserving the requested column names.
    normalized = {}
    for key in records[0].keys():
        normalized[str(key).strip().lower()] = key

    required = ["farm name", "latitude", "longitude"]
    if not all(key in normalized for key in required):
        raise RuntimeError(
            "The Farms sheet must have these exact headers in row 1: "
            "Farm Name, Latitude, Longitude."
        )

    data = []
    for row in records:
        name = str(row.get(normalized["farm name"], "")).strip()
        if not name:
            continue
        try:
            lat = float(row.get(normalized["latitude"]))
            lon = float(row.get(normalized["longitude"]))
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        data.append({"Farm Name": name, "Latitude": lat, "Longitude": lon})

    df = pd.DataFrame(data, columns=FARMS_HEADERS)
    if not df.empty:
        df = df.drop_duplicates(subset=["Farm Name"], keep="first").reset_index(drop=True)
    return df


def get_users_worksheet():
    spreadsheet = get_spreadsheet()
    return get_or_create_worksheet(
        spreadsheet, "Users", USERS_HEADERS, rows=500, cols=8
    )


# ---------------------------- Authentication ----------------------------

def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 240_000
    )
    return f"{salt}${derived.hex()}"


def verify_password(password, stored_hash):
    try:
        salt, expected = stored_hash.split("$", 1)
        actual = hash_password(password, salt).split("$", 1)[1]
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def bootstrap_admin_if_needed():
    ws = get_users_worksheet()
    rows = ws.get_all_records()
    if any(str(r.get("Role", "")).strip().lower() == "admin" for r in rows):
        return

    username = str(secret_value("app", "bootstrap_admin_username", default="") or "").strip()
    password = str(secret_value("app", "bootstrap_admin_password", default="") or "")
    if username and password:
        ws.append_row(
            [
                username,
                hash_password(password),
                "admin",
                "TRUE",
                datetime.now(timezone.utc).isoformat(),
            ],
            value_input_option="USER_ENTERED",
        )


def authenticate(username, password):
    ws = get_users_worksheet()
    for row in ws.get_all_records():
        saved_username = str(row.get("Username", "")).strip()
        active = str(row.get("Active", "TRUE")).strip().lower() in ("true", "yes", "1")
        if saved_username.lower() == username.strip().lower() and active:
            if verify_password(password, str(row.get("Password Hash", ""))):
                return {"username": saved_username, "role": str(row.get("Role", "user")).lower()}
    return None


def add_user(username, password, role="user"):
    username = username.strip()
    if len(username) < 3:
        raise ValueError("Username must contain at least 3 characters.")
    if len(password) < 10:
        raise ValueError("Use a password with at least 10 characters.")
    ws = get_users_worksheet()
    existing = [str(r.get("Username", "")).strip().lower() for r in ws.get_all_records()]
    if username.lower() in existing:
        raise ValueError("That username already exists.")
    ws.append_row(
        [
            username,
            hash_password(password),
            role,
            "TRUE",
            datetime.now(timezone.utc).isoformat(),
        ],
        value_input_option="USER_ENTERED",
    )


# ---------------------------- Routing and geometry ----------------------------

def haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def get_osrm_table(locations):
    """
    locations: list of (latitude, longitude), with the start at index 0.
    Returns driving-distance and driving-duration matrices.
    Uses the public OSRM demo service; do not send frequent/high-volume production traffic.
    """
    coordinate_string = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in locations)
    url = f"{OSRM_BASE_URL}/table/v1/driving/{coordinate_string}"
    response = requests.get(
        url,
        params={"annotations": "distance,duration"},
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "FarmRouteOptimizer/1.0 (Streamlit prototype)"},
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != "Ok":
        raise RuntimeError(f"Routing service error: {payload.get('message', payload.get('code'))}")
    distances = payload.get("distances")
    durations = payload.get("durations")
    if not distances or not durations:
        raise RuntimeError("The routing service did not return a complete distance matrix.")
    if any(value is None for row in distances for value in row):
        raise RuntimeError(
            "At least one selected farm could not be connected by the road-routing service. "
            "Check the coordinates and try again."
        )
    if any(value is None for row in durations for value in row):
        raise RuntimeError("Travel time is unavailable for one or more selected locations.")
    return distances, durations


def optimize_open_route(cost_matrix):
    """
    Return indices in visiting order for an open route:
    starts at matrix index 0 and ends after the final selected farm,
    without returning to the start.
    A dummy end node lets OR-Tools model an open path.
    """
    n = len(cost_matrix)
    if n <= 1:
        return []

    # Add a dummy end node. Travel from any real node to the dummy is free.
    extended = [list(map(int, row)) + [0] for row in cost_matrix]
    extended.append([0] * (n + 1))

    manager = pywrapcp.RoutingIndexManager(n + 1, 1, [0], [n])
    routing = pywrapcp.RoutingModel(manager)

    def cost_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return extended[from_node][to_node]

    callback_index = routing.RegisterTransitCallback(cost_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(callback_index)
    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    search_parameters.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    search_parameters.time_limit.seconds = 3
    solution = routing.SolveWithParameters(search_parameters)
    if solution is None:
        raise RuntimeError("Could not find a route. Try fewer farms or check coordinates.")

    ordered = []
    index = routing.Start(0)
    while not routing.IsEnd(index):
        node = manager.IndexToNode(index)
        if node != 0 and node < n:
            ordered.append(node)
        index = solution.Value(routing.NextVar(index))
    return ordered


def get_osrm_route(locations):
    """Get road-following GeoJSON geometry and route totals for the optimized sequence."""
    coordinate_string = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in locations)
    url = f"{OSRM_BASE_URL}/route/v1/driving/{coordinate_string}"
    response = requests.get(
        url,
        params={"overview": "full", "geometries": "geojson", "steps": "false"},
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "FarmRouteOptimizer/1.0 (Streamlit prototype)"},
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise RuntimeError(f"Could not retrieve route geometry: {payload.get('message', payload.get('code'))}")
    route = payload["routes"][0]
    # GeoJSON coordinates are [longitude, latitude]; Folium needs [latitude, longitude].
    geometry = [[lat, lon] for lon, lat in route["geometry"]["coordinates"]]
    return geometry, float(route["distance"]) / 1000, float(route["duration"]) / 60


def point_near_route_km(lat, lon, route_points):
    # Route geometry is detailed; minimum distance to a route vertex is a practical
    # lightweight recommendation filter for this prototype.
    if not route_points:
        return float("inf")
    return min(haversine_km(lat, lon, rlat, rlon) for rlat, rlon in route_points)


def make_route_map(farms, start_name, start_coord, selected_order, recommended_names, route_geometry):
    center = start_coord
    fmap = folium.Map(location=center, zoom_start=10, control_scale=True, tiles="OpenStreetMap")

    # Road route first so markers remain visible above it.
    if route_geometry and len(route_geometry) >= 2:
        folium.PolyLine(
            route_geometry,
            color="#1769ff",
            weight=5,
            opacity=0.85,
            tooltip="Optimized driving route",
        ).add_to(fmap)

    # Starting marker
    folium.Marker(
        location=start_coord,
        tooltip=f"Start: {start_name}",
        popup=folium.Popup(f"<b>START</b><br>{start_name}", max_width=260),
        icon=folium.Icon(color="green", icon="flag"),
    ).add_to(fmap)

    selected_names = {row["Farm Name"] for row in selected_order}
    recommended_names = set(recommended_names)
    start_farm_name = start_name if start_name in set(farms["Farm Name"]) else None

    # Other farms: recommended orange, everything else red; never numbered.
    for _, farm in farms.iterrows():
        name = farm["Farm Name"]
        if name == start_farm_name or name in selected_names:
            continue
        if name in recommended_names:
            color, label = "orange", "Recommended farm"
        else:
            color, label = "red", "Not selected"
        folium.Marker(
            location=[farm["Latitude"], farm["Longitude"]],
            tooltip=f"{label}: {name}",
            popup=folium.Popup(
                f"<b>{name}</b><br>{label}", max_width=260
            ),
            icon=folium.Icon(color=color, icon="info-sign"),
        ).add_to(fmap)

    # Selected farms are numbered according to route order.
    for visit_no, farm in enumerate(selected_order, start=1):
        folium.Marker(
            location=[farm["Latitude"], farm["Longitude"]],
            tooltip=f"{visit_no}. {farm['Farm Name']}",
            popup=folium.Popup(
                f"<b>Visit {visit_no}</b><br>{farm['Farm Name']}<br>Selected by user",
                max_width=260,
            ),
            icon=DivIcon(
                icon_size=(30, 30),
                icon_anchor=(15, 15),
                html=(
                    '<div style="background:#1769ff;color:white;border:2px solid white;'
                    'border-radius:50%;width:29px;height:29px;display:flex;'
                    'align-items:center;justify-content:center;font-weight:700;'
                    'font-size:13px;box-shadow:0 1px 4px #666;">'
                    f"{visit_no}</div>"
                ),
            ),
        ).add_to(fmap)

    # Ensure all markers and route fit in the visible map area.
    bounds = [start_coord]
    bounds.extend([[f["Latitude"], f["Longitude"]] for f in selected_order])
    if route_geometry:
        bounds.extend(route_geometry[:: max(1, len(route_geometry) // 50)])
    if len(bounds) > 1:
        fmap.fit_bounds(bounds, padding=(25, 25))
    return fmap


# ---------------------------- Login screen ----------------------------

def show_login():
    st.title("🗺️ Farm Route Optimizer")
    st.caption("Sign in to plan efficient farm visits.")
    _, center, _ = st.columns([1, 1.2, 1])
    with center:
        with st.container(border=True):
            st.subheader("Login")
            with st.form("login_form"):
                username = st.text_input("Username", autocomplete="username")
                password = st.text_input("Password", type="password", autocomplete="current-password")
                submitted = st.form_submit_button("Login", type="primary", use_container_width=True)
            if submitted:
                if not username or not password:
                    st.error("Enter your username and password.")
                else:
                    try:
                        user = authenticate(username, password)
                        if user:
                            st.session_state["user"] = user
                            st.rerun()
                        else:
                            st.error("Invalid username/password, or the account is inactive.")
                    except Exception as exc:
                        st.error(f"Login could not be completed: {exc}")
        st.caption("The first admin account is configured through Streamlit Secrets. See README.md.")


# ---------------------------- Main app ----------------------------

def main():
    st.sidebar.title("🗺️ Farm Route Optimizer")
    user = st.session_state.get("user")
    st.sidebar.caption(f"Signed in as **{user['username']}** ({user['role']})")
    if st.sidebar.button("Log out", use_container_width=True):
        st.session_state.pop("user", None)
        st.rerun()

    if user["role"] == "admin":
        with st.sidebar.expander("⚙️ User management", expanded=False):
            st.caption("Create a login for another team member.")
            with st.form("create_user_form", clear_on_submit=True):
                new_username = st.text_input("New username")
                new_password = st.text_input("Temporary password (10+ characters)", type="password")
                new_role = st.selectbox("Role", ["user", "admin"])
                create_user = st.form_submit_button("Create user", use_container_width=True)
            if create_user:
                try:
                    add_user(new_username, new_password, new_role)
                    st.success(f"User '{new_username.strip()}' created.")
                except Exception as exc:
                    st.error(str(exc))

    st.title("Farm Route Planner")
    st.write("Select farms to visit. Nearby unselected farms will be suggested separately and will not be added to your route.")

    try:
        farms = load_farms()
    except Exception as exc:
        st.error(str(exc))
        st.info("Check your Google Sheets setup and the README.md deployment instructions.")
        st.stop()

    if farms.empty:
        st.warning(
            "No valid farms found. Add farm records to the Farms worksheet with columns "
            "'Farm Name', 'Latitude', and 'Longitude'."
        )
        st.stop()

    # Use a sidebar control panel; map and results are displayed in the main area.
    st.sidebar.subheader("Route settings")
    farm_names = farms["Farm Name"].tolist()
    start_mode = st.sidebar.radio(
        "Starting location",
        ["Registered farm", "Custom coordinates"],
        help="A custom start can be your office, warehouse, or another known GPS location.",
    )

    if start_mode == "Registered farm":
        start_name = st.sidebar.selectbox("Start from farm/location", farm_names)
        start_row = farms.loc[farms["Farm Name"] == start_name].iloc[0]
        start_coord = (float(start_row["Latitude"]), float(start_row["Longitude"]))
    else:
        start_name = st.sidebar.text_input("Starting location name", value="Office / Warehouse")
        start_lat = st.sidebar.number_input("Starting latitude", min_value=-90.0, max_value=90.0, value=7.8731, format="%.6f")
        start_lon = st.sidebar.number_input("Starting longitude", min_value=-180.0, max_value=180.0, value=80.7718, format="%.6f")
        start_coord = (float(start_lat), float(start_lon))

    selectable_farms = [
        name for name in farm_names
        if not (start_mode == "Registered farm" and name == start_name)
    ]
    selected_names = st.sidebar.multiselect(
        "Select farms to visit",
        options=selectable_farms,
        help=f"Select up to {MAX_SELECTED_FARMS} farms for one route.",
    )
    objective_label = st.sidebar.radio(
        "Optimization objective",
        ["Shortest driving distance", "Shortest driving time"],
    )
    recommendation_radius = st.sidebar.slider(
        "Recommend farms near route (km)",
        min_value=0.5,
        max_value=10.0,
        value=2.0,
        step=0.5,
        help="Nearby unselected farms appear in a separate section. They are not added to the route.",
    )
    search_clicked = st.sidebar.button(
        "🔎 Search & Optimize Route",
        type="primary",
        use_container_width=True,
    )

    st.sidebar.divider()
    st.sidebar.caption("Map legend")
    st.sidebar.markdown(
        "🟢 **Start**  \n"
        "🔵 **Numbered:** selected farms in visit order  \n"
        "🟠 **Orange:** recommended, not in route  \n"
        "🔴 **Red:** other farms, not in route"
    )

    # The map is generated only after the user presses Search.
    if search_clicked:
        if not selected_names:
            st.warning("Select at least one farm to visit.")
            st.stop()
        if len(selected_names) > MAX_SELECTED_FARMS:
            st.error(f"Please select no more than {MAX_SELECTED_FARMS} farms per route.")
            st.stop()

        selected_df = farms[farms["Farm Name"].isin(selected_names)].copy()
        # Keep a stable mapping between requested names and returned rows.
        selected_df["__selection_order"] = selected_df["Farm Name"].apply(selected_names.index)
        selected_df = selected_df.sort_values("__selection_order").drop(columns="__selection_order")

        locations = [start_coord] + [
            (float(row["Latitude"]), float(row["Longitude"]))
            for _, row in selected_df.iterrows()
        ]

        try:
            with st.spinner("Getting road distances and optimizing the route..."):
                distance_matrix, duration_matrix = get_osrm_table(locations)
                cost_matrix = distance_matrix if objective_label == "Shortest driving distance" else duration_matrix
                order_indices = optimize_open_route(cost_matrix)
                ordered_rows = []
                for idx in order_indices:
                    row = selected_df.iloc[idx - 1]
                    ordered_rows.append(
                        {
                            "Farm Name": row["Farm Name"],
                            "Latitude": float(row["Latitude"]),
                            "Longitude": float(row["Longitude"]),
                        }
                    )
                route_locations = [start_coord] + [
                    (farm["Latitude"], farm["Longitude"]) for farm in ordered_rows
                ]
                route_geometry, total_km, total_minutes = get_osrm_route(route_locations)

                # Recommend farms near the road route, but do NOT add them to the route order.
                ordered_names = {farm["Farm Name"] for farm in ordered_rows}
                start_farm = start_name if start_mode == "Registered farm" else None
                recommended = []
                for _, farm in farms.iterrows():
                    name = farm["Farm Name"]
                    if name in ordered_names or name == start_farm:
                        continue
                    distance_to_route = point_near_route_km(
                        float(farm["Latitude"]), float(farm["Longitude"]), route_geometry
                    )
                    if distance_to_route <= recommendation_radius:
                        recommended.append(
                            {
                                "Farm Name": name,
                                "Latitude": float(farm["Latitude"]),
                                "Longitude": float(farm["Longitude"]),
                                "Distance to route (km)": distance_to_route,
                            }
                        )
                recommended.sort(key=lambda item: item["Distance to route (km)"])

                st.session_state["route_result"] = {
                    "farms": farms,
                    "start_name": start_name.strip() or "Starting location",
                    "start_coord": start_coord,
                    "selected_order": ordered_rows,
                    "recommended": recommended,
                    "route_geometry": route_geometry,
                    "total_km": total_km,
                    "total_minutes": total_minutes,
                    "objective": objective_label,
                    "recommendation_radius": recommendation_radius,
                }
        except requests.RequestException as exc:
            st.error(f"Could not reach the public routing service. Try again later. Details: {exc}")
            st.stop()
        except Exception as exc:
            st.error(f"Could not calculate the route: {exc}")
            st.stop()

    result = st.session_state.get("route_result")
    if not result:
        st.info(
            "Choose your starting location, select the farms you want to visit, choose an objective, "
            "then press **Search & Optimize Route**."
        )
        st.subheader("Farm locations")
        preview_map = folium.Map(
            location=[float(farms["Latitude"].mean()), float(farms["Longitude"].mean())],
            zoom_start=9,
            control_scale=True,
        )
        for _, farm in farms.iterrows():
            folium.Marker(
                [farm["Latitude"], farm["Longitude"]],
                tooltip=farm["Farm Name"],
                popup=farm["Farm Name"],
                icon=folium.Icon(color="red", icon="info-sign"),
            ).add_to(preview_map)
        st_folium(preview_map, use_container_width=True, height=560, returned_objects=[])
        st.caption(f"Loaded {len(farms)} farm locations from Google Sheets.")
        return

    # Result view: visit order and recommendations are deliberately separate.
    result_farms = result["farms"]
    result_start_farm = result["start_name"] if result["start_name"] in set(result_farms["Farm Name"]) else None
    ordered_names = {farm["Farm Name"] for farm in result["selected_order"]}
    recommended_names = {farm["Farm Name"] for farm in result["recommended"]}

    metric1, metric2, metric3 = st.columns(3)
    metric1.metric("Selected farms to visit", len(result["selected_order"]))
    metric2.metric("Driving distance", f"{result['total_km']:.1f} km")
    metric3.metric("Estimated driving time", f"{result['total_minutes']:.0f} min")

    map_col, side_col = st.columns([2.2, 1], gap="large")
    with map_col:
        st.subheader("Optimized route map")
        fmap = make_route_map(
            result_farms,
            result["start_name"],
            result["start_coord"],
            result["selected_order"],
            recommended_names,
            result["route_geometry"],
        )
        st_folium(fmap, use_container_width=True, height=650, returned_objects=[])

    with side_col:
        st.subheader("1. Visit Order")
        st.caption("Only farms you selected are included in this route.")
        visit_df = pd.DataFrame(
            [
                {"No.": i, "Farm Name": farm["Farm Name"]}
                for i, farm in enumerate(result["selected_order"], start=1)
            ]
        )
        st.dataframe(visit_df, hide_index=True, use_container_width=True)

        st.subheader("2. Recommended Farms")
        st.caption(
            f"Unselected farms within approximately {result['recommendation_radius']:.1f} km "
            "of the displayed route. These are suggestions only."
        )
        if result["recommended"]:
            rec_df = pd.DataFrame(
                [
                    {
                        "Farm Name": farm["Farm Name"],
                        "Approx. distance to route (km)": round(farm["Distance to route (km)"], 2),
                    }
                    for farm in result["recommended"]
                ]
            )
            st.dataframe(rec_df, hide_index=True, use_container_width=True)
        else:
            st.write("No additional farms found within the selected recommendation radius.")

        st.subheader("3. Other Farms")
        other_farms = [
            str(name) for name in result_farms["Farm Name"].tolist()
            if name not in ordered_names
            and name not in recommended_names
            and name != result_start_farm
        ]
        if other_farms:
            st.write(", ".join(other_farms))
        else:
            st.write("No other farms.")

    st.caption(
        f"Optimization objective: {result['objective']}. The route starts at "
        f"{result['start_name']} and finishes at the final selected farm; it does not return to the start. "
        "Road distances and times are estimates from the routing service, not live traffic guarantees."
    )
    if st.button("Clear route results"):
        st.session_state.pop("route_result", None)
        st.rerun()


def run_app():
    try:
        # Create the Users tab if needed and add the initial admin only if configured and none exists.
        bootstrap_admin_if_needed()
    except Exception as exc:
        st.title("🗺️ Farm Route Optimizer")
        st.error(f"Google Sheets connection setup failed: {exc}")
        st.info("Follow the Google Sheets and Streamlit Secrets setup in README.md.")
        st.stop()

    if not st.session_state.get("user"):
        show_login()
    else:
        main()


if __name__ == "__main__":
    run_app()
