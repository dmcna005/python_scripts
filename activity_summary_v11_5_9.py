# to install the required modules :  sudo -H python3 -m pip install -r activity_summary_requirements.txt

import requests
from requests.auth import HTTPDigestAuth
import csv
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from prettytable import PrettyTable
from datetime import datetime, timedelta
from cryptography.fernet import Fernet
import json
import os
import time
from collections import defaultdict
import sys
import concurrent.futures
import multiprocessing
import asyncio

# Get current working dirctory and working file path
script_dir = os.path.dirname(os.path.abspath(__file__))
file_path = os.path.join(script_dir, 'atlas_config.json')
# Load configuration from JSON file
with open(file_path) as config_file:
    config = json.load(config_file)

# Define a lambda function to decrypt credentials
decrypt_credential = lambda enc_cred, key: Fernet(key.encode()).decrypt(enc_cred.encode()).decode()

# Define your MongoDB Atlas API Key and Secret
API_KEY = decrypt_credential(config['api_key'], config['encryption_key'])
API_SECRET = decrypt_credential(config['api_secret'], config['encryption_key'])
# API_KEY = os.getenv('API_KEY')
# API_SECRET = os.getenv('API_SECRET')

# Define the base URL for the Atlas API
BASE_URL = config['base_url']
HEADERS = config['headers']
# Define the organization ID
ORG_ID = config['organization_id']

# Email settings
SMTP_SERVER = config['smtp_server']
SMTP_PORT = config['smtp_port']
EMAIL_SENDER = config['email_sender']
EMAIL_RECEIVER = 'dwayne.mcnab@macys.com' #config['email_receiver']
EMAIL_SUBJECT = config['email_subject']

# Fieldnames for CSV and HTML tables
FIELDNAMES = config['fieldnames']

# Number of days to filter new clusters
NUM_DAYS_NEW_CLUSTERS = config['num_days_new_clusters']
NUM_DAYS_ACTIVITY = config['num_days_activity']

# Disk increase threshold
DISK_UTILIZATION_THRESHOLD = config['disk_utilization_threshold']

# Number of days to check for CPU utilization
NUM_DAYS_CPU_UTILIZATION = config['num_days_cpu_utilization']

# Number of days to retian saved files
FILE_RETENTION_DAYS = config['file_retention_days']

# List of events to include
INCLUDED_EVENTS = config.get('included_events', [])
if not INCLUDED_EVENTS:
    print("Warning: 'included_events' is missing or empty in the configuration file.")

# Events that require raw payload for enhanced parsing/classification
RAW_REQUIRED_EVENTS = {
    'CLUSTER_UPDATE_SUBMITTED',              # instance size old/new + status
    'COMPUTE_AUTO_SCALE_INITIATED_BASE',     # system vs user via raw.source
    'DISK_AUTO_SCALE_INITIATED'              # system vs user via raw.source
}

def _fetch_raw_events_for_projects(projects_info, event_type, days):
    """Fetch raw-included events of a single type across all projects in parallel."""
    return fetch_activity_feed_parallel(
        projects_info,
        [event_type],
        days,
        include_raw=True,
        max_workers=12
    )

def ensure_raw_payloads(projects_info, events_data, days):
    """Ensure events of RAW_REQUIRED_EVENTS have raw payloads.

    Strategy:
    1. Identify which configured events (present in INCLUDED_EVENTS) are in RAW_REQUIRED_EVENTS.
    2. For each, determine which already-collected events lack a 'raw' key.
    3. Fetch raw versions only once per event type (Atlas returns recent events) and replace/augment list preferring raw-bearing copies.
    """
    if not events_data:
        return events_data
    needed = [e for e in RAW_REQUIRED_EVENTS if e in INCLUDED_EVENTS]
    if not needed:
        return events_data
    # Index existing events by (type,id) retaining whether they already have raw
    by_key = {}
    for ev in events_data:
        et = ev.get('eventTypeName')
        eid = ev.get('id') or ev.get('_id') or ev.get('eventId')
        has_raw = isinstance(ev.get('raw'), dict) and ev.get('raw')
        if (et, eid) not in by_key or (has_raw and not isinstance(by_key[(et, eid)].get('raw'), dict)):
            by_key[(et, eid)] = ev
    # Fetch raw versions per needed event type
    for et in needed:
        # If all current events of this type already have raw, skip
        missing = any(ev.get('eventTypeName') == et and not isinstance(ev.get('raw'), dict) for ev in by_key.values())
        if not missing:
            continue
        raw_events = _fetch_raw_events_for_projects(projects_info, et, days)
        for rev in raw_events:
            if rev.get('eventTypeName') != et:
                continue
            eid = rev.get('id') or rev.get('_id') or rev.get('eventId')
            key = (et, eid)
            # Always prefer raw version
            by_key[key] = rev
    # Return merged list
    return list(by_key.values())


# Utilitiy Functions

def make_api_request(url, api_key, api_secret, error_message):
    """Generic function to make API requests with error handling."""
    try:
        response = requests.get(url, verify=False, headers=HEADERS, auth=HTTPDigestAuth(api_key, api_secret), timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"{error_message}: {str(e)}")
        return None

def fetch_activity_feed_parallel(projects, included_events, days, include_raw=False, max_workers=10):
    """Fetch activity feeds for multiple projects in parallel to reduce total runtime.
    Returns consolidated list of events.
    """
    results = []
    if not projects:
        return results
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_proj = {
            executor.submit(
                fetch_activity_feed,
                API_KEY,
                API_SECRET,
                proj.get('id'),
                included_events,
                days,
                include_raw
            ): proj for proj in projects if proj.get('id')
        }
        for future in concurrent.futures.as_completed(future_to_proj):
            try:
                evs = future.result()
                if evs:
                    results.extend(evs)
            except Exception as e:
                proj = future_to_proj[future]
                print(f"[WARN] Failed fetching events for project {proj.get('id')}: {e}")
    return results

# Utility function to safely delete files that ends with .csv or .json and older than 7 days
def delete_old_files(directory="activity_output_folder", days=FILE_RETENTION_DAYS):
    import os, time, glob
    now = time.time()
    cutoff = now - (days * 86400)
    for ext in ("*.csv", "*.json", "*.txt"):
        for file_path in glob.glob(os.path.join(directory, ext)):
            if os.path.isfile(file_path) and os.path.getmtime(file_path) < cutoff:
                try:
                    os.remove(file_path)
                    print(f"Deleted old file: {file_path}")
                except Exception as e:
                    print(f"Failed to delete {file_path}: {e}")

def bytes_to_gb(bytes_value):
    """Convert bytes to gigabytes."""
    try:
        return float(bytes_value) / (1024 ** 3) if bytes_value is not None else None
    except (ValueError, TypeError):
        print(f"Invalid bytes value: {bytes_value}")
        return None

def send_email(html_content, attachment_paths):
    MAX_ATTACHMENT_SIZE = 5 * 1024 * 1024  # 5MB
    skipped_files = []
    msg = MIMEMultipart()
    msg['From'] = EMAIL_SENDER
    msg['To'] = EMAIL_RECEIVER
    msg['Subject'] = EMAIL_SUBJECT

    msg.attach(MIMEText(html_content, 'html'))

    for attachment_path in filter(None, attachment_paths):
        try:
            if not os.path.exists(attachment_path):
                continue
            file_size = os.path.getsize(attachment_path)
            if file_size > MAX_ATTACHMENT_SIZE:
                skipped_files.append(os.path.basename(attachment_path))
                continue
            attachment = MIMEBase('application', 'octet-stream')
            with open(attachment_path, 'rb') as attachment_file:
                attachment.set_payload(attachment_file.read())
            encoders.encode_base64(attachment)
            attachment.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(attachment_path)}"')
            msg.attach(attachment)
        except Exception as e:
            print(f"Failed to attach {attachment_path}: {str(e)}")
            sys.exit(1)

    # If any files were skipped, add a note to the email body
    if skipped_files:
        note = f"<p><b>Note:</b> The following files were not attached because they exceed 5MB:<br>" + ", ".join(skipped_files) + "</p>"
        msg.attach(MIMEText(note, 'html'))

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.send_message(msg)
        print(f"Email sent to {EMAIL_RECEIVER}")
    except Exception as e:
        print(f"Failed to send email: {str(e)}")
        sys.exit(1)

def error_exit(message):
    print(message)
    sys.exit(1)


def initialize_tables():
    """Initialize PrettyTables; event tables created only if present in INCLUDED_EVENTS (config-driven)."""
    tables = {}
    # Static non-event tables
    static_tables = {
        'new_clusters': FIELDNAMES,
        'disk_utilization': [
            "Project Name", "Cluster Name", "Process ID", "Partition Name",
            "Free Space (GB)", "Used Space (GB)", "Increase in Used Space (GB)", "Utilization (%)", "Timestamp"
        ],
        'cpu_utilization': [
            "Project Name", "Cluster Name", "Process ID",
            "Normalized CPU Utilization (%)", "Timestamp"
        ],
        'summary': ["Metric", "Count"],
        'atlas_versions': ["MongoDB Version", "Count"]
    }
    for name, fields in static_tables.items():
        t = PrettyTable(); t.field_names = fields; tables[name] = t
    # Event specific schemas
    for ev in INCLUDED_EVENTS:
        table_name = ev.upper()
        if table_name in tables:
            continue
        if ev == 'CLUSTER_UPDATE_SUBMITTED':
            schema = ["Project Name", "Cluster Name", "Username", "Timestamp", "Instance Type Old", "Instance Type New", "Status"]
        elif ev == 'CLUSTER_DELETE_SUBMITTED':
            schema = ["Project Name", "Cluster Name", "Username", "Timestamp"]
        elif ev == 'COMPUTE_AUTO_SCALE_INITIATED_BASE':
            schema = ["Project Name", "Cluster Name", "System Count", "User Count"]
        elif ev == 'DISK_AUTO_SCALE_INITIATED':
            schema = ["Project Name", "Cluster Name", "System Count", "User Count"]
        elif ev == 'COMPUTE_AUTO_SCALE_MAX_INSTANCE_SIZE_FAIL_BASE':
            schema = ["Project Name", "Cluster Name", "Count"]
        else:
            # Generic event table
            schema = ["Project Name", "Cluster Name", "Event Type", "Timestamp"]
        t = PrettyTable(); t.field_names = schema; tables[table_name] = t
    return tables


def save_to_csv(data, fieldnames, filename):
    """Generic function to save a list of dicts to CSV."""
    try:
        with open(filename, mode='w', newline='') as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(data)
        print(f"Data saved to {filename}")
    except Exception as e:
        error_exit(f"Failed to save data to CSV: {str(e)}")


def save_to_json(data, filename):
    """Generic function to save data to JSON."""
    try:
        with open(filename, 'w') as json_file:
            json.dump(data, json_file, indent=4)
        print(f"Data saved to {filename}")
    except Exception as e:
        error_exit(f"Failed to save data to JSON: {str(e)}")

def list_projects(api_key, api_secret, org_id):
    """List all projects in the organization with pagination."""
    projects = []
    page_num = 1
    items_per_page = 100

    while True:
        url = f"{BASE_URL}/orgs/{ORG_ID}/groups?pageNum={page_num}&itemsPerPage={items_per_page}"
        response = make_api_request(url, api_key, api_secret, "Error fetching projects")
        if not response:
            print(f"No response received for activity feed API call: {url}")
            break
        if not response.get('results'):
            print(f"No results found in activity feed response: {response}")
            break

        projects.extend(response['results'])

        # Check if we've fetched all projects
        if len(response['results']) < items_per_page:
            break

        page_num += 1

    return projects

def process_replication_specs(cluster):
    """Extract and format replication specs from a cluster."""
    replication_specs = cluster.get('replicationSpecs', [])
    region_names = []

    for spec in replication_specs:
        regions_config = spec.get('regionsConfigs', {})
        for region, config in regions_config.items():
            region_info = f"{region} (Electable: {config.get('electableNodes', 0)}, Analytics: {config.get('analyticsNodes', 0)}, Read-Only: {config.get('readOnlyNodes', 0)})"
            region_names.append(region_info)

    return "; ".join(region_names) if region_names else "N/A"

def list_clusters(api_key, api_secret, project_id):
    """Stream all clusters in a project using a generator."""
    page_num = 1
    items_per_page = 100

    while True:
        url = f"{BASE_URL}/groups/{project_id}/clusters?pageNum={page_num}&itemsPerPage={items_per_page}"
        response = make_api_request(url, api_key, api_secret, f"Error fetching clusters for project {project_id}")

        if not response or not response.get('results'):
            break

        for cluster in response['results']:
            yield cluster

        # Check if we've fetched all clusters
        if len(response['results']) < items_per_page:
            break

        page_num += 1


def list_processes(api_key, api_secret, project_id):
    """Stream all processes in a project using a generator."""
    url = f"{BASE_URL}/groups/{project_id}/processes"
    response = make_api_request(url, api_key, api_secret, f"Error fetching processes for project {project_id}")

    if response and response.get('results'):
        for process in response['results']:
            yield process
    else:
        print(f"No processes found for project {project_id}")


def list_partition_names(api_key, api_secret, project_id, process_id):
    """Stream disk partitions for a process using a generator."""
    url = f"{BASE_URL}/groups/{project_id}/processes/{process_id}/disks"
    response = make_api_request(url, api_key, api_secret, f"Error fetching disks for process {process_id}")

    if response and response.get('results'):
        for partition in response['results']:
            yield partition
    else:
        print(f"No partitions found for process {process_id}")

def fetch_activity_feed(api_key, api_secret, project_id, included_events, days=NUM_DAYS_ACTIVITY, include_raw=False):
    """Fetch activity feed for a project, querying only included events. Optionally include raw payloads (includeRaw=true).
    included_events can be a list of event names or a comma-separated string.
    """
    # Normalize included_events to a comma-separated string
    if isinstance(included_events, (list, tuple)):
        included_events_query = ",".join(included_events)
    else:
        included_events_query = included_events or ""

    if not included_events_query:
        print("No included events specified. Please check the configuration.")
    else:
        print(f"Fetching activity feed with included events: {included_events_query}")

    page_num = 1
    events = []  # Initialize events as an empty list

    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=days)  # Calculate start_date based on end_date and days

    include_raw_param = "&includeRaw=true" if include_raw else ""

    while True:
        url = (
            f"{BASE_URL}/groups/{project_id}/events?"
            f"pageNum={page_num}&itemsPerPage=100"
            f"&minDate={start_date.isoformat()}Z&maxDate={end_date.isoformat()}Z"
            f"&eventTypeName={included_events_query}"
            f"{include_raw_param}"
        )
        response = make_api_request(url, api_key, api_secret, f"Error fetching activity feed for project {project_id}")

        if not response or not response.get('results'):
            break

        for event in response['results']:
            # Ensure project_name is included in each event
            event['projectName'] = event.get('projectName', 'N/A')
            events.append(event)

        # Check if we have more pages
        if len(response['results']) < 100:
            break
        page_num += 1

    return events

def get_disk_utilization_data(api_key, api_secret, project_id, process_id, partition_name):
    """Get disk utilization data for a specific partition."""
    metrics = [
        'DISK_PARTITION_SPACE_FREE',
        'DISK_PARTITION_SPACE_USED',
        'DISK_PARTITION_SPACE_PERCENT_USED'
    ]

    disk_data = fetch_utilization_data(
        api_key, api_secret, project_id,
        'disk', process_id, 1, metrics, partition_name
    )

    if not disk_data:
        return None

    disk_metrics = {}
    percent_used = None
    for measurement in disk_data.get('measurements', []):
        metric_name = measurement.get('name')
        data_points = [dp for dp in measurement.get('dataPoints', []) if dp.get('value') is not None]

        if data_points:
            # Get the most recent data point
            recent_point = sorted(data_points, key=lambda x: x['timestamp'], reverse=True)[0]
            disk_metrics[metric_name] = (recent_point['value'], recent_point['timestamp'])
            if metric_name == 'DISK_PARTITION_SPACE_PERCENT_USED':
                percent_used = recent_point['value']

    # Only return disk_metrics if utilization > 50%
    if percent_used is not None and percent_used > 50:
        return disk_metrics
    else:
        return None

def fetch_utilization_data(api_key, api_secret, project_id, resource_type, resource_id, days, metrics, partition_name=None):
    """Generic function to fetch utilization data (CPU or Disk)."""
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=days)

    if resource_type == 'process':
        base_url = f"{BASE_URL}/groups/{project_id}/processes/{resource_id}/measurements"
    elif resource_type == 'disk' and partition_name:
        base_url = f"{BASE_URL}/groups/{project_id}/processes/{resource_id}/disks/{partition_name}/measurements"
    else:
        return None

    url = f"{base_url}?granularity=PT1H&start={start_date.isoformat()}Z&end={end_date.isoformat()}Z"

    # Add metrics to URL
    url += "&m=" + "&m=".join(metrics)

    return make_api_request(url, api_key, api_secret,
                          f"Error fetching {metrics} for {resource_type} {resource_id}")


def process_disk_utilization(api_key, api_secret, project_id, project_name, cluster, process_id, tables):
    """Process disk utilization and report partitions with significant usage increase."""

    disks_info = list_partition_names(api_key, api_secret, project_id, process_id)
    if not disks_info:
        print(f"No partitions found for process {process_id}")
        return

    for disk in disks_info:
        partition_name = disk.get('partitionName')
        if not partition_name:
            continue

        disk_metrics = fetch_utilization_data(
            api_key, api_secret, project_id,
            'disk', process_id, NUM_DAYS_CPU_UTILIZATION,
            [
                'DISK_PARTITION_SPACE_FREE',
                'DISK_PARTITION_SPACE_USED',
                'DISK_PARTITION_SPACE_PERCENT_USED'
            ],
            partition_name
        )

        if not disk_metrics or 'measurements' not in disk_metrics:
            continue

        used_measurement = next(
            (m for m in disk_metrics['measurements'] if m.get('name') == 'DISK_PARTITION_SPACE_USED'), None
        )
        if not used_measurement:
            continue

        data_points = used_measurement.get('dataPoints', [])
        if len(data_points) < 2:
            continue

        # Sort data points by timestamp
        data_points.sort(key=lambda dp: dp['timestamp'])

        try:
            # Calculate historical usage based on NUM_DAYS_CPU_UTILIZATION
            historical_point = next(
                (dp for dp in data_points if
                 datetime.utcnow() - datetime.strptime(dp['timestamp'], "%Y-%m-%dT%H:%M:%SZ") >= timedelta(days=NUM_DAYS_CPU_UTILIZATION)),
                data_points[0]  # Fallback to the oldest point if no match
            )
            latest_point = data_points[-1]

            historical_used_gb = bytes_to_gb(historical_point['value'])
            current_used_gb = bytes_to_gb(latest_point['value'])

            if historical_used_gb == 0:
                percent_increase = None
            elif current_used_gb is None or historical_used_gb is None:
                percent_increase = None
            else:
                percent_increase = ((current_used_gb - historical_used_gb) / historical_used_gb) * 100

            # Extract other metrics
            free_bytes = next(
                (m.get('dataPoints', [{}])[-1].get('value')
                 for m in disk_metrics.get('measurements', [])
                 if m.get('name') == 'DISK_PARTITION_SPACE_FREE'), None)

            utilization = next(
                (m.get('dataPoints', [{}])[-1].get('value')
                 for m in disk_metrics.get('measurements', [])
                 if m.get('name') == 'DISK_PARTITION_SPACE_PERCENT_USED'), None)

            free_gb = bytes_to_gb(free_bytes) if free_bytes is not None else None

            row = [
                project_name,
                cluster['name'],
                process_id,
                partition_name,
                f"{free_gb:.2f}" if free_gb is not None else 'N/A',
                f"{current_used_gb:.2f}" if current_used_gb is not None else 'N/A',
                f"{percent_increase:.2f}%" if percent_increase is not None else 'N/A',
                f"{utilization:.2f}" if utilization is not None else 'N/A',
                latest_point['timestamp']
            ]

            # Only add rows where both utilization > 50 and percent_increase > 50
            if utilization is not None and utilization > 50 and percent_increase is not None and percent_increase > 50:
                tables['disk_utilization'].add_row(row)

        except Exception as e:
            print(f"Error processing partition {partition_name}: {e}")
            continue


def process_cpu_utilization(api_key, api_secret, project_id, project_name, cluster, process_id, tables):
    """Process CPU utilization data for a process."""
    cpu_data = fetch_utilization_data(
        api_key, api_secret, project_id,
        'process', process_id, NUM_DAYS_ACTIVITY,
        ['PROCESS_NORMALIZED_CPU_USER']
    )

    if not cpu_data or not cpu_data.get('measurements'):
        return

    cpu_rows = []  # Collect qualifying rows

    for measurement in cpu_data['measurements']:
        if measurement.get('name') == 'PROCESS_NORMALIZED_CPU_USER':
            for data_point in measurement.get('dataPoints', []):
                timestamp = data_point.get('timestamp', 'N/A')
                value = data_point.get('value', None)
                if value is not None:
                    # Directly use the CPU utilization value
                    if value >= 75:  # Only include values greater than 75
                        cpu_rows.append([
                            project_name,
                            cluster.get('name', 'N/A'),
                            process_id,
                            safe_float(value),  # Ensure value is numeric and robust
                            timestamp
                        ])

    # Sort by CPU utilization (index 3), descending
    cpu_rows.sort(key=lambda row: safe_float(row[3]), reverse=True)

    for row in cpu_rows:
        if isinstance(row[3], float):
            row[3] = f"{row[3]:.2f}"  # Format CPU for display
        tables['cpu_utilization'].add_row(row)

    # Modify CPU Utilization table to show one line item per cluster
    unique_clusters = {}
    for row in tables['cpu_utilization']._rows:
        cluster_name = row[1]
        if cluster_name not in unique_clusters:
            unique_clusters[cluster_name] = row

    # Clear the table and re-add unique rows
    tables['cpu_utilization'].clear_rows()
    for row in unique_clusters.values():
        tables['cpu_utilization'].add_row(row)

def process_events(events_data, event_type, table, row_builder, cluster_to_project=None):
    """Generic function to process events and update a table, always using cluster_to_project if provided."""
    if not events_data:
        print(f"No events to process for event type: {event_type}")
        return
    for event in events_data:
        if event.get('eventTypeName') == event_type:
            cluster_name = event.get('clusterName', 'N/A')
            project_id = event.get('groupId') or event.get('projectId')
            project_name = None
            if cluster_to_project and project_id and cluster_name:
                project_name = cluster_to_project.get((project_id, cluster_name))
                if not project_name:
                    print(f"[WARN] Could not resolve project name for cluster '{cluster_name}' in project '{project_id}' for event type '{event_type}'")
            if not project_name:
                project_name = event.get('projectName', 'N/A')
            event = dict(event)  # Copy to avoid mutating original
            event['projectName'] = project_name
            event['clusterName'] = cluster_name
            row = row_builder(event)
            if row:
                table.add_row(row)

def process_host_rollback_events(events_data, tables, cluster_to_project):
    """Process HOST_ROLLBACK events and update the summary and HOST_ROLLBACK tables."""
    def row_builder(event):
        cluster_name = event.get('clusterName', 'N/A')
        project_id = event.get('groupId') or event.get('projectId')
        project_name = None
        if cluster_to_project and project_id and cluster_name:
            project_name = cluster_to_project.get((project_id, cluster_name))
            if not project_name:
                print(f"[WARN] Could not resolve project name for cluster '{cluster_name}' in project '{project_id}' for event type 'HOST_ROLLBACK'")
        if not project_name:
            project_name = event.get('projectName', 'N/A')
        return [project_name, cluster_name, event.get('eventTypeName', 'N/A'), event.get('created', 'N/A')]
    # Update the summary table
    process_events(events_data, 'HOST_ROLLBACK', tables['summary'], lambda e: [f"Host Rollback ({e.get('clusterName', 'N/A')})", 1], cluster_to_project)
    # Update the HOST_ROLLBACK table
    if 'HOST_ROLLBACK' not in tables:
        from prettytable import PrettyTable
        tables['HOST_ROLLBACK'] = PrettyTable()
        tables['HOST_ROLLBACK'].field_names = ["Project Name", "Cluster Name", "Event Type", "Timestamp"]
    tables['HOST_ROLLBACK'].clear_rows()
    for event in events_data:
        if event.get('eventTypeName') == 'HOST_ROLLBACK':
            cluster_name = event.get('clusterName', 'N/A')
            project_id = event.get('groupId') or event.get('projectId')
            project_name = None
            if cluster_to_project and project_id and cluster_name:
                project_name = cluster_to_project.get((project_id, cluster_name))
                if not project_name:
                    print(f"[WARN] Could not resolve project name for cluster '{cluster_name}' in project '{project_id}' for event type 'HOST_ROLLBACK'")
            if not project_name:
                project_name = event.get('projectName', 'N/A')
            row = [
                project_name,
                cluster_name,
                event.get('eventTypeName', 'N/A'),
                event.get('created', 'N/A')
            ]
            tables['HOST_ROLLBACK'].add_row(row)
    sort_table_descending(tables['HOST_ROLLBACK'])

def process_compute_auto_scale_events(events_data, project_name, tables):
    """Process COMPUTE_AUTO_SCALE events and update the summary table."""
    def row_builder(event):
        cluster_name = event.get('clusterName', 'N/A')
        return [f"Compute Auto Scale ({cluster_name})", 1]

    process_events(events_data, 'COMPUTE_AUTO_SCALE', tables['summary'], row_builder)

def process_cluster_update_submitted_events(events_data, tables, cluster_to_project):
    """Process CLUSTER_UPDATE_SUBMITTED events, adding instance type old/new columns sourced from
    raw.clusterDescriptionDiff.params[].param entries referencing instance size.

    Columns: Project Name | Cluster Name | Username | Timestamp | Instance Type Old | Instance Type New
    """
    # Ensure table has desired schema
    expected_fields = ["Project Name", "Cluster Name", "Username", "Timestamp", "Instance Type Old", "Instance Type New", "Status"]
    if 'CLUSTER_UPDATE_SUBMITTED' not in tables:
        tables['CLUSTER_UPDATE_SUBMITTED'] = PrettyTable()
    if tables['CLUSTER_UPDATE_SUBMITTED'].field_names != expected_fields:
        tables['CLUSTER_UPDATE_SUBMITTED'].field_names = expected_fields
    # Always clear to avoid duplicates
    tables['CLUSTER_UPDATE_SUBMITTED'].clear_rows()

    if not events_data:
        return

    for event in events_data:
        if event.get('eventTypeName') != 'CLUSTER_UPDATE_SUBMITTED':
            continue
        username = event.get('username')
        if not username or username in ('N/A', 'CLUSTER_UPDATE_SUBMITTED'):
            continue
        cluster_name = event.get('clusterName', 'N/A')
        project_id = event.get('groupId') or event.get('projectId')
        project_name = event.get('projectName', 'N/A')
        if cluster_to_project and project_id and cluster_name:
            resolved = cluster_to_project.get((project_id, cluster_name))
            if resolved:
                project_name = resolved
        created_ts = event.get('created', 'N/A')

        inst_old = 'N/A'
        inst_new = 'N/A'
        status = 'N/A'
        raw = event.get('raw') or {}
        if isinstance(raw, dict):
            cdd = raw.get('clusterDescriptionDiff') or {}
            params = cdd.get('params') or []
            # Some events may use clusterDescriptionDiff.diff or changes
            if not params and isinstance(cdd, dict):
                alt_params = cdd.get('diff') or cdd.get('changes') or []
                if isinstance(alt_params, list):
                    params = alt_params
            if isinstance(params, list):
                for p in params:
                    try:
                        param_name = (p.get('param') or p.get('name') or '').lower()
                        display = (p.get('display') or '').lower()
                        if any(k in param_name for k in ('instancesize', 'instance_size')) or 'instance size' in display:
                            old_val = p.get('old') if p.get('old') is not None else p.get('previous')
                            new_val = p.get('new') if p.get('new') is not None else p.get('current')
                            if old_val is not None:
                                inst_old = old_val
                            if new_val is not None:
                                inst_new = new_val
                            # Don't break immediately; prefer a param explicitly named instanceSize over others
                            if param_name == 'instanceSize'.lower() or 'instancesize' in param_name:
                                break
                    except Exception as ex:
                        print(f"[DEBUG] Param parse error in CLUSTER_UPDATE_SUBMITTED: {ex}")
                        continue
            # Prefer status from clusterDescriptionDiff
            if isinstance(cdd, dict):
                cdd_status = cdd.get('status') or cdd.get('state')
                if isinstance(cdd_status, str) and cdd_status:
                    status = cdd_status
            # Fallback status keys at raw root
            if status == 'N/A':
                for key in ('status', 'state', 'result', 'outcome'):
                    val = raw.get(key)
                    if isinstance(val, str) and val:
                        status = val
                        break
        # Final sanity: if both old/new are still N/A attempt direct top-level diff keys
        if inst_old == 'N/A' and inst_new == 'N/A':
            direct_old = raw.get('oldInstanceSize') or raw.get('previousInstanceSize')
            direct_new = raw.get('newInstanceSize') or raw.get('currentInstanceSize')
            if direct_old: inst_old = direct_old
            if direct_new: inst_new = direct_new
        # Log when still missing to aid debugging (can be toggled off if noisy)
        if inst_old == 'N/A' and inst_new == 'N/A':
            print(f"[INFO] No instance size diff found for event {event.get('id','?')} cluster={cluster_name}")

        tables['CLUSTER_UPDATE_SUBMITTED'].add_row([
            project_name,
            cluster_name,
            username,
            created_ts,
            inst_old,
            inst_new,
            status
        ])

    try:
        sort_table_descending(tables['CLUSTER_UPDATE_SUBMITTED'], column_name='Timestamp')
    except Exception:
        try:
            sort_table_descending(tables['CLUSTER_UPDATE_SUBMITTED'])
        except Exception:
            pass

def process_cluster_delete_submitted_events(events_data, tables, cluster_to_project, project_id_to_name=None):
    """Process CLUSTER_DELETE_SUBMITTED events and update the table, using project_id_to_name for project name resolution."""
    def row_builder(event):
        cluster_name = event.get('clusterName', 'N/A')
        username = event.get('username', None)
        if username is None or username == 'N/A' or username == 'CLUSTER_DELETE_SUBMITTED':
            return None
        project_id = event.get('groupId') or event.get('projectId', 'N/A')
        project_name = None
        # Prefer project_id_to_name if available
        if project_id_to_name and project_id in project_id_to_name:
            project_name = project_id_to_name[project_id]
        # Fallback to cluster_to_project mapping if available
        if not project_name and cluster_to_project and project_id and cluster_name:
            project_name = cluster_to_project.get((project_id, cluster_name))
        # Fallback to event['projectName']
        if not project_name or project_name == 'N/A':
            project_name = event.get('projectName', 'N/A')
        if not project_name or project_name == 'N/A':
            project_name = 'Unknown Project'
        return [project_name, cluster_name, username, event.get('created', 'N/A')]
    process_events(events_data, 'CLUSTER_DELETE_SUBMITTED', tables['CLUSTER_DELETE_SUBMITTED'], row_builder, cluster_to_project)
    sort_table_descending(tables['CLUSTER_DELETE_SUBMITTED'])

def process_host_has_index_suggestions_events(api_key, api_secret, projects_info, tables, days=NUM_DAYS_ACTIVITY):
    """Iterate projects and clusters, aggregate HOST_HAS_INDEX_SUGGESTIONS per cluster and populate table.

    - Only include events whose timestamp falls within the last `days` days (relative to now).
    - Timestamp displayed in the table is the execution time (now) for all rows.
    """
    from collections import defaultdict

    if not isinstance(projects_info, (list, tuple)) or not projects_info:
        print("No projects provided to process_host_has_index_suggestions_events")
        return

    counts = defaultdict(lambda: {"count": 0, "timestamps": set()})

    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=days)

    def parse_event_timestamp(e):
        # Prefer top-level created then timestamp, ts, then raw.createdAt
        ts_val = e.get('created') or e.get('timestamp') or e.get('ts') or (e.get('raw') or {}).get('createdAt')
        if not ts_val:
            return None
        # Accept common ISO formats with or without fractional seconds
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
            try:
                return datetime.strptime(ts_val, fmt)
            except Exception:
                continue
        # Last resort: try to parse date portion only
        try:
            return datetime.fromisoformat(ts_val.replace('Z', '+00:00')).replace(tzinfo=None)
        except Exception:
            return None

    for project in projects_info:
        if not isinstance(project, dict):
            continue
        project_id = project.get('id')
        project_name = project.get('name') or project.get('projectName') or 'N/A'
        if not project_id:
            continue

        # Fetch all HOST_HAS_INDEX_SUGGESTIONS events for the project and include raw payloads
        events = fetch_activity_feed(api_key, api_secret, project_id, ['HOST_HAS_INDEX_SUGGESTIONS'], days=days, include_raw=True)
        if not events:
            continue

        for e in events:
            if e.get('eventTypeName') != 'HOST_HAS_INDEX_SUGGESTIONS':
                continue

            # Parse event timestamp and filter by date range
            event_dt = parse_event_timestamp(e)
            if not event_dt:
                # If timestamp can't be parsed, skip the event to avoid including stale data
                continue
            # event_dt is naive UTC (we treated Z as UTC); compare with start/end
            if event_dt < start_date or event_dt > end_date:
                # outside desired window
                continue

            # Resolve cluster name: top-level -> raw -> target -> 'N/A'
            cluster = e.get('clusterName') or (e.get('raw') or {}).get('clusterName') or (e.get('target') or {}).get('clusterName') or 'N/A'

            key = (project_name or 'N/A', cluster or 'N/A')
            counts[key]['count'] += 1
            counts[key]['timestamps'].add(event_dt.isoformat() + 'Z')

    # Ensure table exists and has required columns
    if 'HOST_HAS_INDEX_SUGGESTIONS' not in tables:
        from prettytable import PrettyTable
        tables['HOST_HAS_INDEX_SUGGESTIONS'] = PrettyTable()
        tables['HOST_HAS_INDEX_SUGGESTIONS'].field_names = [
            'Project Name', 'Cluster Name', 'Count', 'Timestamp'
        ]

    tables['HOST_HAS_INDEX_SUGGESTIONS'].clear_rows()

    exec_ts = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

    if not counts:
        tables['HOST_HAS_INDEX_SUGGESTIONS'].add_row(['N/A', 'N/A', 0, exec_ts])
    else:
        # Sort by count desc and output per-cluster breakdown
        for (project_name, cluster_name), info in sorted(counts.items(), key=lambda x: x[1]['count'], reverse=True):
            tables['HOST_HAS_INDEX_SUGGESTIONS'].add_row([project_name, cluster_name, info['count'], exec_ts])

    # Attempt to sort by Count column if helper exists
    try:
        sort_table_descending(tables['HOST_HAS_INDEX_SUGGESTIONS'], column_name='Count')
    except Exception:
        pass

def process_disk_auto_scale_initiated_events(events_data, tables, cluster_to_project):
    """Process DISK_AUTO_SCALE_INITIATED events and separate counts by source (SYSTEM vs USER)."""
    from collections import defaultdict
    counts = defaultdict(lambda: {'system': 0, 'user': 0})  # key: (project_name, cluster_name)
    def classify_source(ev):
        raw = ev.get('raw') or {}
        # Primary source field
        source = (raw.get('source') or ev.get('source') or '').strip().upper()
        # Heuristic: if no explicit source but userId/accountUserId present, treat as USER
        user_id = raw.get('userId') or raw.get('accountUserId') or ev.get('userId')
        if not source:
            if user_id:
                source = 'USER'
            elif raw.get('isMmsAdmin') is True:
                source = 'SYSTEM'
        if source not in ('SYSTEM','USER'):
            # Default anything unknown to USER unless admin flag present
            source = 'SYSTEM' if raw.get('isMmsAdmin') else 'USER'
        return source
    for event in events_data:
        if event.get('eventTypeName') != 'DISK_AUTO_SCALE_INITIATED':
            continue
        cluster_name = event.get('clusterName', 'N/A')
        project_id = event.get('groupId') or event.get('projectId')
        project_name = None
        if cluster_to_project and project_id and cluster_name:
            project_name = cluster_to_project.get((project_id, cluster_name))
        if not project_name:
            project_name = event.get('projectName', 'N/A')
        if not project_name:
            project_name = 'N/A'
        source = classify_source(event)
        key = (project_name, cluster_name)
        if source == 'SYSTEM':
            counts[key]['system'] += 1
        else:
            counts[key]['user'] += 1
    table = tables['DISK_AUTO_SCALE_INITIATED']
    table.clear_rows()
    table.field_names = ["Project Name", "Cluster Name", "System Count", "User Count"]
    for (project_name, cluster_name), c in counts.items():
        table.add_row([project_name, cluster_name, c['system'], c['user']])
    try:
        idx_sys = table.field_names.index('System Count')
        idx_user = table.field_names.index('User Count')
        table._rows.sort(key=lambda r: (int(r[idx_sys]), int(r[idx_user])), reverse=True)
    except Exception:
        pass

def process_generic_events(events_data, tables, cluster_to_project, project_id_to_name=None):
    """Process all events from INCLUDED_EVENTS (excluding special events) and update the corresponding tables, using cluster_to_project for correct mapping.
    For COMPUTE_AUTO_SCALE_MAX_INSTANCE_SIZE_FAIL_BASE and COMPUTE_AUTO_SCALE_INITIATED_BASE, aggregate by count."""
    special_events = {
        'HOST_ROLLBACK',
        'CLUSTER_UPDATE_SUBMITTED',
        'CLUSTER_DELETE_SUBMITTED',
        'HOST_HAS_INDEX_SUGGESTIONS',
        'DISK_AUTO_SCALE_INITIATED'  # Prevent generic processing of this event
    }
    count_events = {
        'COMPUTE_AUTO_SCALE_MAX_INSTANCE_SIZE_FAIL_BASE'
    }
    # Separate structure for auto scale initiated system/user counts
    auto_scale_initiated_counts = defaultdict(lambda: defaultdict(lambda: {'system': 0, 'user': 0}))
    def classify_source(ev):
        raw = ev.get('raw') or {}
        source = (raw.get('source') or ev.get('source') or '').strip().upper()
        user_id = raw.get('userId') or raw.get('accountUserId') or ev.get('userId')
        if not source:
            if user_id:
                source = 'USER'
            elif raw.get('isMmsAdmin') is True:
                source = 'SYSTEM'
        if source not in ('SYSTEM','USER'):
            source = 'SYSTEM' if raw.get('isMmsAdmin') else 'USER'
        return source
    event_counts = {etype: defaultdict(lambda: defaultdict(int)) for etype in count_events}
    for event_type in INCLUDED_EVENTS:
        if event_type in special_events:
            continue
        table_name = event_type.upper()
        if table_name not in tables:
            if event_type in count_events:
                tables[table_name] = PrettyTable(); tables[table_name].field_names = ["Project Name", "Cluster Name", "Count"]
            else:
                tables[table_name] = PrettyTable(); tables[table_name].field_names = ["Project Name", "Cluster Name", "Event Type", "Timestamp"]
    for event in events_data:
        event_type = event.get('eventTypeName', 'N/A')
        if event_type in special_events:
            continue
        cluster_name = event.get('clusterName', 'N/A')
        project_id = event.get('groupId') or event.get('projectId')
        project_name = None
        # Try full key (project_id, cluster_name) if mapping uses tuple
        if project_id and cluster_name:
            project_name = cluster_to_project.get((project_id, cluster_name)) if cluster_to_project else None
        # Fallback: direct cluster_name key (older mapping style)
        if not project_name and cluster_to_project:
            project_name = cluster_to_project.get(cluster_name)
        # Fallback: event provided name
        if not project_name:
            project_name = event.get('projectName') or 'N/A'
        # Fallback: project_id_to_name mapping
        if (not project_name or project_name == 'N/A') and project_id_to_name and project_id:
            project_name = project_id_to_name.get(project_id, 'N/A')
        if not project_name or project_name == 'N/A':
            print(f"[WARN] Could not resolve project name for cluster '{cluster_name}' (project_id={project_id}) in event type '{event_type}'")
        # Always update the event dict for downstream code
        event = dict(event)
        event['projectName'] = project_name
        event['clusterName'] = cluster_name
        if event_type == 'COMPUTE_AUTO_SCALE_INITIATED_BASE':
            source = classify_source(event)
            key_proj = project_name
            key_cluster = cluster_name
            if source == 'SYSTEM':
                auto_scale_initiated_counts[key_proj][key_cluster]['system'] += 1
            else:
                auto_scale_initiated_counts[key_proj][key_cluster]['user'] += 1
        elif event_type in count_events:
            event_counts[event_type][project_name][cluster_name] += 1
        elif event_type in INCLUDED_EVENTS:
            table_name = event_type.upper()
            expected_cols = len(tables[table_name].field_names)
            row = [
                project_name,
                cluster_name,
                event_type,
                event.get('created', 'N/A')
            ][:expected_cols]
            # Add row if project is present, and cluster is present if required
            if project_name != 'N/A' and (cluster_name != 'N/A' or (cluster_name == 'N/A' and 'Cluster Name' in tables[table_name].field_names)):
                tables[table_name].add_row(row)
            elif project_name != 'N/A' and cluster_name == 'N/A':
                # For project-level events, allow missing cluster
                tables[table_name].add_row(row)
            else:
                print(f"[WARN] Skipping row for event type '{event_type}' due to missing project/cluster name: {row}")
    for event_type in count_events:
        table_name = event_type.upper()
        for project_name, clusters in event_counts[event_type].items():
            for cluster_name, count in clusters.items():
                if project_name != 'N/A' and cluster_name != 'N/A':
                    tables[table_name].add_row([project_name, cluster_name, count])
                elif project_name != 'N/A' and cluster_name == 'N/A':
                    tables[table_name].add_row([project_name, cluster_name, count])
                else:
                    print(f"[WARN] Skipping count row for event type '{event_type}' due to missing project/cluster name: {project_name}, {cluster_name}, {count}")
        sort_table_descending(tables[table_name])
    # Populate COMPUTE_AUTO_SCALE_INITIATED_BASE grouped table
    if 'COMPUTE_AUTO_SCALE_INITIATED_BASE' in INCLUDED_EVENTS:
        table_name = 'COMPUTE_AUTO_SCALE_INITIATED_BASE'
        table = tables.get(table_name)
        if table:
            table.clear_rows()
            for project_name, clusters in auto_scale_initiated_counts.items():
                for cluster_name, counts in clusters.items():
                    table.add_row([
                        project_name,
                        cluster_name,
                        counts['system'],
                        counts['user']
                    ])
            # Sort by System Count then User Count
            try:
                idx_sys = table.field_names.index('System Count')
                idx_user = table.field_names.index('User Count')
                table._rows.sort(key=lambda r: (int(r[idx_sys]), int(r[idx_user])), reverse=True)
            except Exception:
                pass
    # Sort all other event tables
    for event_type in INCLUDED_EVENTS:
        if event_type in special_events or event_type in count_events:
            continue
        table_name = event_type.replace(' ', '_').upper()
        sort_table_descending(tables[table_name])

def filter_and_generate_html(tables):
    """Filter events and generate HTML content based on included_events only."""
    if not os.path.exists('project_activity.csv'):
        return ""

    grouped_events = defaultdict(list)  # Use defaultdict to group events by type
    try:
        with open('project_activity.csv', mode='r') as file:
            reader = csv.DictReader(file)
            for row in reader:
                # Ensure row is a dictionary before accessing 'get' method
                if isinstance(row, dict):
                    event_type = row.get('eventTypeName', 'N/A')
                else:
                    event_type = 'N/A'  # Default value if row is not a dictionary
                if event_type in INCLUDED_EVENTS:  # Only include events from the config
                    grouped_events[event_type].append(row)
    except Exception as e:
        print(f"Error reading activity CSV: {str(e)}")
        return ""

    # Process host rollback events and update the summary table
    # Flatten grouped events for processing
    flattened_events = [row for rows in grouped_events.values() for row in rows] if grouped_events else []

    # # Process host rollback events and update the summary table
    # flattened_events = process_host_rollback_events(flattened_events, tables) if flattened_events else []

    # Process compute auto scale events and update the summary table
    flattened_events = process_compute_auto_scale_events(flattened_events, tables) if flattened_events else []

    # Re-group events after processing specific events
    regrouped_events = defaultdict(list)
    for row in flattened_events:
        # Ensure row is a dictionary before accessing 'get' method
        if isinstance(row, dict):
            event_type = row.get('eventTypeName', 'N/A')
        else:
            event_type = 'N/A'  # Default value if row is not a dictionary
        regrouped_events[event_type].append(row)

    if not regrouped_events:
        return ""

    html_content = """
    <html>
    <head>
        <style>
            table { width: auto; max-width: 100%; border-collapse: collapse; margin-bottom: 20px; }
            th { background-color: #4CAF50; color: white; border: 1px solid #ddd; padding: 8px; }
            td { border: 1px solid #ddd; padding: 8px; }
            tr:nth-child(even) { background-color: #f2f2f2; }
            tr:hover { background-color: #ddd; }
        </style>
    </head>
    <body>
    """

    for event_type, rows in regrouped_events.items():
        table = PrettyTable()
        table.field_names = config['activity_fieldnames']
        for row in rows:
            table.add_row([row.get(field, 'N/A') for field in config['activity_fieldnames']])

        html_content += f"<h2>{event_type}</h2>"
        html_content += table.get_html_string()

    html_content += "</body></html>"
    return html_content

def process_cluster_data(api_key, api_secret, project, tables, version_counts, unique_projects, version_to_clusters, all_clusters_list=None):
    """Process data for all clusters in a single project."""
    project_id = project['id']
    project_name = project['name']
    unique_projects.add(project_name)

    print(f"Processing project: {project_name} ({project_id})")

    # Initialize a counter for the number of clusters
    cluster_count = 0

    # Iterate over the generator returned by list_clusters
    clusters = list(list_clusters(api_key, api_secret, project_id))  # Fully consume the generator
    cluster_count = len(clusters)  # Update cluster count based on the number of clusters
    for cluster in clusters:

        # Process basic cluster info for each cluster
        process_basic_cluster_info(cluster, project_name, tables, version_counts, version_to_clusters, all_clusters_list)

        # Process processes and their metrics for this cluster
        process_cluster_processes(api_key, api_secret, project_id, project_name, cluster, tables)

    print(f"Processed {cluster_count} clusters in project {project_name}")


def process_basic_cluster_info(cluster, project_name, tables, version_counts, version_to_clusters, all_clusters_list=None):
    """Process basic cluster information for a single cluster."""
    try:
        # Get connection strings - default to empty string if not found
        srv_connection_string = next(
            (ep.get('srvConnectionString', 'N/A')
             for ep in cluster.get('connectionStrings', {}).get('privateEndpoint', [])),
            'N/A'
        )

        # Get region names
        replication_specs = cluster.get('replicationSpecs', [])
        region_names = []
        instance_size_name = "N/A"
        disk_size_gb = "N/A"
        num_shards = "1"

        for spec in replication_specs:
            region_configs = spec.get('regionConfigs', [])
            for region_config in region_configs:
                # Extract region name
                region_name = region_config.get('regionName', 'N/A')
                if region_name != 'N/A':
                    region_names.append(region_name)

                # Extract instance size name from electableSpecs
                instance_size_name = region_config.get('electableSpecs', {}).get('instanceSize', 'N/A')

                # Extract disk size
                disk_size_gb = region_config.get('electableSpecs', {}).get('diskSizeGB', 'N/A')

                # Extract number of shards
                num_shards = len(replication_specs)

                # Extract min and max instance sizes
                min_instance_size = region_config.get('autoScaling', {}).get('compute', {}).get('minInstanceSize', 'N/A')
                max_instance_size = region_config.get('autoScaling', {}).get('compute', {}).get('maxInstanceSize', 'N/A')

                # Extract auto-scaling
                auto_scaling = region_config.get('autoScaling', {}).get('compute', {}).get('enabled', 'N/A')

        region_names_str = ":".join(region_names) if region_names else 'N/A'

        # Prepare the row with all required fields
        row = {
            'project_name': project_name,
            'cluster_name': cluster.get('name', 'N/A'),
            'regionNames': region_names_str,
            'mongoDBMajorVersion': cluster.get('mongoDBMajorVersion', 'N/A'),
            'mongoDBVersion': cluster.get('mongoDBVersion', 'N/A'),
            'srvConnectionString': srv_connection_string,
            'createDate': cluster.get('createDate', 'N/A'),
            'instanceSizeName': instance_size_name,
            'minInstanceSize': min_instance_size,
            'maxInstanceSize': max_instance_size,
            'paused': cluster.get('paused', 'N/A'),
            'diskSizeGB': disk_size_gb,
            'providerBackupEnabled': str(cluster.get('backupEnabled', 'N/A')),
            'pitEnabled': cluster.get('pitEnabled', 'N/A'),
            'numShards': num_shards,
            'terminationProtectionEnabled': cluster.get('terminationProtectionEnabled', 'N/A'),
            'autoScaling': auto_scaling
        }

        # Ensure all fields are present in the row
        row['project_name'] = project_name  # Explicitly add project_name to the row
        for field in FIELDNAMES:
            if field not in row:
                row[field] = 'FIELD ABSENT'

        # Add to all_clusters_list if provided (always add ALL clusters)
        if all_clusters_list is not None:
            all_clusters_list.append(dict(row))

        # Add to new_clusters table if created within the last NUM_DAYS_NEW_CLUSTERS
        create_date_str = cluster.get('createDate', '')
        if create_date_str and create_date_str != 'N/A':
            try:
                create_date = datetime.strptime(create_date_str, "%Y-%m-%dT%H:%M:%SZ")
                if create_date >= datetime.now() - timedelta(days=NUM_DAYS_NEW_CLUSTERS):
                    tables['new_clusters'].add_row([row.get(field, 'N/A') for field in FIELDNAMES])
                    print(f"New cluster found: {project_name}/{cluster['name']} created on {create_date_str}")
            except ValueError as e:
                print(f"Error parsing create date for cluster {cluster['name']}: {str(e)}")
        # Update version counts (distinct clusters per version)
        mongoDBVersion = cluster.get('mongoDBVersion')
        cluster_name = cluster.get('name')
        if mongoDBVersion and mongoDBVersion != 'N/A' and cluster_name:
            if mongoDBVersion not in version_to_clusters:
                version_to_clusters[mongoDBVersion] = set()
            version_to_clusters[mongoDBVersion].add((project_name, cluster_name))

    except Exception as e:
        print(f"Error processing cluster {cluster.get('name', 'unknown')} in project {project_name}: {str(e)}")


def process_cluster_processes(api_key, api_secret, project_id, project_name, cluster, tables):
    """Process all processes for a cluster."""
    processes_info = list_processes(api_key, api_secret, project_id)
    if not processes_info: #or not processes_info.get('results'):
        return

    for process in processes_info:#['results']:
        process_id = process['id']
        # Process CPU utilization
        process_cpu_utilization(api_key, api_secret, project_id, project_name, cluster, process_id, tables)
        # Process disk utilization
        process_disk_utilization(api_key, api_secret, project_id, project_name, cluster, process_id, tables)

def create_output_folder():
    """Create a folder named 'activity_output_folder' in the current directory."""
    folder_name = "activity_output_folder"
    if not os.path.exists(folder_name):
        os.makedirs(folder_name)
    return folder_name

# Use the output folder for saving files
output_folder = create_output_folder()

# Update file paths to save in the output folder
# cluster_info_filename = os.path.join(output_folder, "cluster_data.csv")
unique_projects_filename = os.path.join(output_folder, "unique_projects.txt")
activity_csv_filename = os.path.join(output_folder, f"project_activity_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
activity_json_filename = os.path.join(output_folder, f"project_activity_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")

def save_cluster_data_to_csv(tables, filename):
    """Save new cluster data to CSV file with a timestamp using the generic helper."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename_with_timestamp = f"{filename.split('.')[0]}_{timestamp}.csv"
    # Save only new clusters
    data = [dict(zip(FIELDNAMES, row)) for row in tables['new_clusters']._rows]
    save_to_csv(data, FIELDNAMES, filename_with_timestamp)

def save_all_clusters_info_csv(all_clusters_list, output_folder):
    """Save all clusters info to all_clusters_info_<timestamp>.csv in the output folder and return the file path."""
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"all_clusters_info_{timestamp}.csv"
    output_path = os.path.join(output_folder, filename)
    save_to_csv(all_clusters_list, FIELDNAMES, output_path)
    print(f"All clusters info saved to {output_path}")
    return output_path

def save_activity_data(projects_info, activity_fieldnames):
    """Save activity data to CSV and JSON files with a timestamp."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    activity_csv_filename = f"project_activity_{timestamp}.csv"
    activity_json_filename = f"project_activity_{timestamp}.json"

    activity_rows = []
    raw_activity_data = []

    for project in projects_info:
        project_id = project['id']
        project_name = project['name']
        activity_feed = fetch_activity_feed(API_KEY, API_SECRET, project_id, INCLUDED_EVENTS, days=NUM_DAYS_ACTIVITY)
        if not activity_feed:
            continue
        raw_activity_data.extend(activity_feed)
        for event in activity_feed:
            if event.get('eventTypeName') in INCLUDED_EVENTS:
                activity_rows.append({
                    'project_name': project_name,
                    'clusterName': event.get('clusterName', 'N/A'),
                    'eventTypeName': event.get('eventTypeName', 'N/A'),
                    'created': event.get('created', 'N/A'),
                    'username': event.get('username', 'N/A')
                })
    if activity_rows:
        save_to_csv(activity_rows, activity_fieldnames, activity_csv_filename)
    if raw_activity_data:
        save_to_json(raw_activity_data, activity_json_filename)

def save_unique_projects(unique_projects):
    """Save unique projects to a text file with a timestamp and return the filename."""
    """Save unique projects to a text file with a timestamp."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_projects_filename = f"unique_projects_{timestamp}.txt"

    try:
        with open(unique_projects_filename, 'w') as file:
            file.write("\n".join(sorted(unique_projects)))
    except Exception as e:
        print(f"Failed to save unique projects list: {str(e)}")
    return None

    return unique_projects_filename

def get_total_drop_index_suggestions(api_key, api_secret, projects_info):
    """Fetch and aggregate the total number of drop index suggestions across all clusters in all projects."""
    total_drop_index_suggestions = 0
    for project in projects_info:
        if not isinstance(project, dict):
            continue  # Skip if not a dict (defensive)
        project_id = project.get('id')
        if not project_id:
            continue
        clusters = list(list_clusters(api_key, api_secret, project_id))
        for cluster in clusters:
            cluster_name = cluster.get('name')
            if not cluster_name:
                continue
            url = f"{BASE_URL}/groups/{project_id}/clusters/{cluster_name}/performanceAdvisor/dropIndexSuggestions"
            # Use make_api_request with correct argument order
            response = make_api_request(url, api_key, api_secret, f"Error fetching drop index suggestions for {project_id}/{cluster_name}")
            if response and 'totalCount' in response:
                total_drop_index_suggestions += response['totalCount']
            elif response and 'results' in response:
                total_drop_index_suggestions += len(response['results'])
    return total_drop_index_suggestions

def generate_html_content(tables, version_counts, unique_projects, events_data, total_clusters, drop_indexes_count=None):
    """Generate the complete HTML content for the email with a dynamic summary table."""
    tables['summary'].clear_rows()  # Clear existing rows to avoid duplicates
    summary_rows = []
    summary_rows.append(["Projects", len(unique_projects)])
    summary_rows.append(["Clusters", total_clusters])
    summary_rows.append(["Disk Utilization Alerts", len(tables['disk_utilization']._rows)])
    summary_rows.append(["High CPU Utilization Alerts", len(tables['cpu_utilization']._rows)])
    # Sum the 'Count' column for Index Suggestions in the table
    host_has_index_suggestions_count = sum(
        int(row[2]) if isinstance(row[2], (int, float, str)) and str(row[2]).isdigit() else 0
        for row in tables['HOST_HAS_INDEX_SUGGESTIONS']._rows
    )
    summary_rows.append(["Index Suggestions", host_has_index_suggestions_count])
    # Add Drop Indexes metric if provided
    if drop_indexes_count is not None:
        summary_rows.append(["Drop Indexes", drop_indexes_count])
    # Add underutilized count to summary
    underutilized_count = len(get_underutilized_instances(tables))
    summary_rows.append(["Underutilized Instances", underutilized_count])
    # Only add rows with count > 0 (except Projects, Clusters, Underutilized Instances, Drop Indexes)
    for metric, count in summary_rows:
        if metric in ("Projects", "Clusters", "Underutilized Instances", "Drop Indexes") or count > 0:
            tables['summary'].add_row([metric, count])
    # Update versions table
    for version, count in sorted(version_counts.items(), key=lambda x: x[1], reverse=True):
        tables['atlas_versions'].add_row([version, count])

    # Sort CPU Utilization table in reverse order by CPU Utilization (column index 3)
    tables['cpu_utilization']._rows.sort(key=lambda row: safe_float(row[3]), reverse=True)

    # Sort Disk Utilization table in reverse order by Utilization (column index 7)
    tables['disk_utilization']._rows.sort(key=lambda row: safe_float(row[7]), reverse=True)

    html_content = f"""
    <html>
    <head>
        <style>
            body {{
                font-family: Arial, sans-serif;
                background-color: #f4f4f4;
                margin: 20px;
                padding: 20px;
            }}
            h2 {{
                color: #2C3E50;
                border-bottom: 2px solid #3498DB;
                padding-bottom: 5px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                background: #ffffff;
                border-radius: 5px;
                overflow: hidden;
                box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
            }}
            th, td {{
                border: 1px solid #ddd;
                padding: 12px;
                text-align: left;
            }}
            th {{
                background-color: #3498DB;
                color: white;
                font-weight: bold;
            }}
            tr:nth-child(even) {{background-color: #f9f9f9;}}
            tr:hover {{background-color: #f1f1f1;}}
        </style>
    </head>
    <body>
        <h2>Summary</h2>
        {tables['summary'].get_html_string()}
        <h2>Atlas Versions</h2>
        {tables['atlas_versions'].get_html_string()}
        <h2>New Clusters Created in Last {NUM_DAYS_NEW_CLUSTERS} Days</h2>
        {tables['new_clusters'].get_html_string() if tables['new_clusters']._rows else f'<p><b>No new clusters created in the last {NUM_DAYS_NEW_CLUSTERS} days</b></p>'}
        <h2>CPU Utilization (Last {NUM_DAYS_ACTIVITY} Days, >= 75%)</h2>
        {tables['cpu_utilization'].get_html_string() if tables['cpu_utilization']._rows else '<p>No high CPU utilization found</p>'}
        <h2>Disk Utilization</h2>
        {tables['disk_utilization'].get_html_string() if tables['disk_utilization']._rows else f'<p>No disk increase above: {DISK_UTILIZATION_THRESHOLD}% threshold</p>'}
        <h2>Host Has Index Suggestions</h2>
        {tables['HOST_HAS_INDEX_SUGGESTIONS'].get_html_string() if tables['HOST_HAS_INDEX_SUGGESTIONS']._rows else '<p>No index suggestions found</p>'}
    """

    # Include all tables dynamically
    for table_name, table in tables.items():
        if table_name not in ['summary', 'atlas_versions', 'new_clusters', 'cpu_utilization', 'disk_utilization', 'HOST_HAS_INDEX_SUGGESTIONS']:
            if table._rows:
                html_content += f"<h2>{table_name.replace('_', ' ').title()}</h2>"
                html_content += table.get_html_string()

    html_content += "</body></html>"
    return html_content

def get_underutilized_instances(tables):
    """Return a list of underutilized instances with instance size >= M30 and Normalized CPU Utilization < 50%."""
    underutilized = []
    cpu_table = tables.get('cpu_utilization')
    new_clusters_table = tables.get('new_clusters')
    if not cpu_table or not new_clusters_table:
        return underutilized

    # Build a lookup for (project, cluster) to Normalized CPU Utilization
    cpu_lookup = {}
    for row in cpu_table._rows:
        project_name = row[0]
        cluster_name = row[1]
        try:
            cpu_util = float(row[3])
        except Exception:
            continue
        key = (project_name, cluster_name)
        # Use the minimum CPU utilization if there are multiple entries
        if key not in cpu_lookup or cpu_util < cpu_lookup[key]:
            cpu_lookup[key] = cpu_util

    # For each cluster, check instance size and CPU utilization
    for cluster_row in new_clusters_table._rows:
        project_name = cluster_row[0]
        cluster_name = cluster_row[1]
        instance_size = cluster_row[7]  # instanceSizeName
        disk_size = cluster_row[10]     # diskSizeGB
        key = (project_name, cluster_name)
        cpu_util = cpu_lookup.get(key, None)
        try:
            if (
                instance_size
                and instance_size.startswith('M')
                and int(instance_size[1:]) >= 30
                and cpu_util is not None
                and cpu_util < 50.0  # Only include if Normalized CPU Utilization < 50%
            ):
                underutilized.append({
                    'project_name': project_name,
                    'cluster_name': cluster_name,
                    'disk_size': disk_size,
                    'cpu_utilization': cpu_util
                })
        except Exception:
            continue
    return underutilized

def sort_table_descending(table, column_name=None):
    """Sort a PrettyTable in descending order by the specified column name (default: 'Count' or 'Timestamp')."""
    if not table._rows or not table.field_names:
        return
    if column_name is None:
        if 'Count' in table.field_names:
            column_name = 'Count'
        elif 'Timestamp' in table.field_names:
            column_name = 'Timestamp'
        else:
            column_name = table.field_names[-1]
    try:
        col_idx = table.field_names.index(column_name)
        def sort_key(row):
            try:
                val = row[col_idx]
                if isinstance(val, str):
                    val = val.replace('%','').replace(',','').strip()
                return float(val)
            except Exception:
                return -float('inf')
        table._rows.sort(key=sort_key, reverse=True)
    except Exception:
        pass

def safe_float(val, default=-float('inf')):
    try:
        if isinstance(val, str):
            val = val.replace('%','').replace(',','').strip()
        return float(val)
    except Exception:
        return default

def report_storage_gt_logical_csv(all_clusters_list, output_folder):
    """Generate a CSV for clusters where storage size > logical size."""
    if not all_clusters_list:
        return None
    sample = all_clusters_list[0]
    storage_keys = [k for k in sample if 'storage' in k.lower() and 'size' in k.lower()]
    logical_keys = [k for k in sample if 'logical' in k.lower() and 'size' in k.lower()]
    if not storage_keys or not logical_keys:
        return None
    storage_key = storage_keys[0]
    logical_key = logical_keys[0]
    filtered_rows = []
    for row in all_clusters_list:
        try:
            storage_val = float(row.get(storage_key, 0))
            logical_val = float(row.get(logical_key, 0))
            if storage_val > logical_val:
                filtered_rows.append(row)
        except Exception:
            continue
    if filtered_rows:
        from datetime import datetime
        csv_name = f"clusters_storage_gt_logical_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        csv_path = os.path.join(output_folder, csv_name)
        save_to_csv(filtered_rows, list(sample.keys()), csv_path)
        print(f"Storage > Logical size CSV saved to {csv_path}")
        return csv_path
    return None

def collect_performance_advisor_data(api_key, api_secret, projects_info, output_folder):
    """Collect indexSuggestions, dropIndexSuggestions, and schemaAdvice for all clusters and save to JSON if available."""
    import os
    performance_advisor = {}
    for project in projects_info:
        project_id = project.get('id')
        project_name = project.get('name')
        if not project_id or not project_name:
            continue
        clusters = list(list_clusters(api_key, api_secret, project_id))
        for cluster in clusters:
            cluster_name = cluster.get('name')
            if not cluster_name:
                continue
            cluster_key = f"{project_name}/{cluster_name}"
            cluster_advice = {}
            # Index Suggestions
            url_index = f"{BASE_URL}/groups/{project_id}/clusters/{cluster_name}/performanceAdvisor/indexSuggestions"
            resp_index = make_api_request(url_index, api_key, api_secret, f"Error fetching index suggestions for {project_id}/{cluster_name}")
            if resp_index and resp_index.get('results'):
                cluster_advice['indexSuggestions'] = resp_index['results']
            # Drop Index Suggestions
            url_drop = f"{BASE_URL}/groups/{project_id}/clusters/{cluster_name}/performanceAdvisor/dropIndexSuggestions"
            resp_drop = make_api_request(url_drop, api_key, api_secret, f"Error fetching drop index suggestions for {project_id}/{cluster_name}")
            if resp_drop and resp_drop.get('results'):
                cluster_advice['dropIndexSuggestions'] = resp_drop['results']
            # Schema Advice
            url_schema = f"{BASE_URL}/groups/{project_id}/clusters/{cluster_name}/performanceAdvisor/schemaSuggestions"
            resp_schema = make_api_request(url_schema, api_key, api_secret, f"Error fetching schema advice for {project_id}/{cluster_name}")
            if resp_schema and resp_schema.get('results'):
                cluster_advice['schemaAdvice'] = resp_schema['results']
            if cluster_advice:
                performance_advisor[cluster_key] = cluster_advice
    # Save to JSON if any advice found
    if performance_advisor:
        out_path = os.path.join(output_folder, "performance_advisor.json")
        save_to_json(performance_advisor, out_path)
        print(f"Performance advisor data saved to {out_path}")
        return out_path
    else:
        print("No performance advisor data found for any cluster.")
        return None

def main():
    start_time = time.time()
    delete_old_files(directory=output_folder, days=7)
    tables = initialize_tables()
    version_counts = {}
    version_to_clusters = {}
    unique_projects = set()
    projects_info = list_projects(API_KEY, API_SECRET, ORG_ID)
    if not projects_info:
        print("No projects found or error fetching projects")
        return
    # Build project_id_to_name mapping
    project_id_to_name = {p['id']: p['name'] for p in projects_info if 'id' in p and 'name' in p}
    events_data = []
    # Initial fetch without raw (faster)
    events_data.extend(fetch_activity_feed_parallel(
        projects_info,
        INCLUDED_EVENTS,
        NUM_DAYS_ACTIVITY,
        include_raw=False,
        max_workers=12))
    # Upgrade required events to include raw payloads
    events_data = ensure_raw_payloads(projects_info, events_data, NUM_DAYS_ACTIVITY)
    cluster_to_project = {}
    for project in projects_info:
        project_id = project['id']
        project_name = project['name']
        for cluster in list(list_clusters(API_KEY, API_SECRET, project_id)):
            cluster_name = cluster.get('name')
            if cluster_name:
                cluster_to_project[(project_id, cluster_name)] = project_name
    total_clusters = 0
    all_clusters_list = []
    for project in projects_info:
        clusters = list(list_clusters(API_KEY, API_SECRET, project['id']))
        total_clusters += len(clusters)
        process_cluster_data(API_KEY, API_SECRET, project, tables, version_counts, unique_projects, version_to_clusters, all_clusters_list)
    all_clusters_info_path = save_all_clusters_info_csv(all_clusters_list, output_folder)
    attachments = [all_clusters_info_path, unique_projects_filename]
    storage_gt_logical_csv = report_storage_gt_logical_csv(all_clusters_list, output_folder)
    if storage_gt_logical_csv:
        attachments.append(storage_gt_logical_csv)
    version_counts.clear()
    for version, cluster_set in version_to_clusters.items():
        version_counts[version] = len(cluster_set)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cluster_info_filename = os.path.join(output_folder, f"cluster_data_{timestamp}.csv")
    save_cluster_data_to_csv(tables, cluster_info_filename)
    new_clusters_data_csv = os.path.join(output_folder, f"new_clusters_data_{timestamp}.csv")
    new_clusters_data = [dict(zip(FIELDNAMES, row)) for row in tables['new_clusters']._rows]
    if new_clusters_data:
        save_to_csv(new_clusters_data, FIELDNAMES, new_clusters_data_csv)
        attachments.append(new_clusters_data_csv)
    disk_utilization_all_csv = os.path.join(output_folder, f"disk_utilization_all_partitions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    try:
        all_disk_rows = []
        disk_fieldnames = [
            "Project Name", "Cluster Name", "Process ID", "Partition Name",
            "Free Space (GB)", "Used Space (GB)", "% Increase", "Utilization (%)", "Timestamp"
        ]
        for project in projects_info:
            project_id = project['id']
            project_name = project['name']
            clusters = list(list_clusters(API_KEY, API_SECRET, project_id))
            for cluster in clusters:
                cluster_name = cluster['name']
                processes_info = list_processes(API_KEY, API_SECRET, project_id)
                if not processes_info:
                    continue
                for process in processes_info:
                    process_id = process.get('id')
                    disks_info = list_partition_names(API_KEY, API_SECRET, project_id, process_id)
                    if not disks_info:
                        continue
                    for disk in disks_info:
                        partition_name = disk.get('partitionName')
                        if not partition_name:
                            continue
                        try:
                            disk_metrics = fetch_utilization_data(
                                API_KEY, API_SECRET, project_id,
                                'disk', process_id, NUM_DAYS_CPU_UTILIZATION,
                                [
                                    'DISK_PARTITION_SPACE_FREE',
                                    'DISK_PARTITION_SPACE_USED',
                                    'DISK_PARTITION_SPACE_PERCENT_USED'
                                ],
                                partition_name
                            )
                            if not disk_metrics or 'measurements' not in disk_metrics:
                                continue
                            used_measurement = next(
                                (m for m in disk_metrics['measurements'] if m.get('name') == 'DISK_PARTITION_SPACE_USED'), None
                            )
                            if not used_measurement:
                                continue
                            data_points = used_measurement.get('dataPoints', [])
                            if len(data_points) < 2:
                                continue
                            data_points.sort(key=lambda dp: dp['timestamp'])
                            historical_point = data_points[0]
                            latest_point = data_points[-1]
                            historical_used_gb = bytes_to_gb(historical_point['value'])
                            current_used_gb = bytes_to_gb(latest_point['value'])
                            if historical_used_gb == 0:
                                percent_increase = None
                            elif current_used_gb is None or historical_used_gb is None:
                                percent_increase = None
                            else:
                                percent_increase = ((current_used_gb - historical_used_gb) / historical_used_gb) * 100
                            free_bytes = next(
                                (m.get('dataPoints', [{}])[-1].get('value')
                                 for m in disk_metrics.get('measurements', [])
                                 if m.get('name') == 'DISK_PARTITION_SPACE_FREE'), None)
                            utilization = next(
                                (m.get('dataPoints', [{}])[-1].get('value')
                                 for m in disk_metrics.get('measurements', [])
                                 if m.get('name') == 'DISK_PARTITION_SPACE_PERCENT_USED'), None)

                            # Skip rows where utilization is None or <= 50%
                            if utilization is None or utilization <= 50:
                                continue

                            free_gb = bytes_to_gb(free_bytes) if free_bytes is not None else None
                            row = [
                                project_name,
                                cluster_name,
                                process_id,
                                partition_name,
                                f"{free_gb:.2f}" if free_gb is not None else 'N/A',
                                f"{current_used_gb:.2f}" if current_used_gb is not None else 'N/A',
                                f"{percent_increase:.2f}%" if percent_increase is not None else 'N/A',
                                f"{utilization:.2f}" if utilization is not None else 'N/A',
                                latest_point['timestamp']
                            ]
                            all_disk_rows.append(row)
                        except Exception as e:
                            print(f"Error processing partition {partition_name} for CSV: {e}")
                            continue
        def safe_float(val):
            try:
                if isinstance(val, str):
                    val = val.replace('%','').replace(',','').strip()
                return float(val)
            except Exception:
                return -float('inf')
        all_disk_rows.sort(key=lambda row: safe_float(row[7]), reverse=True)
        with open(disk_utilization_all_csv, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(disk_fieldnames)
            for row in all_disk_rows:
                writer.writerow(row)
        attachments.append(disk_utilization_all_csv)
    except Exception as e:
        print(f"Failed to save disk utilization (all partitions) data to CSV: {str(e)}")
    underutilized = get_underutilized_instances(tables)
    underutilized_csv = None
    if underutilized:
        underutilized_csv = os.path.join(output_folder, f"underutilized_instances_data_{timestamp}.csv")
        with open(underutilized_csv, 'w', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=['project_name', 'cluster_name', 'disk_size', 'cpu_utilization'])
            writer.writeheader()
            writer.writerows(underutilized)
        attachments.append(underutilized_csv)
    # process_host_rollback_events(events_data, tables, cluster_to_project)
    # Special / custom event processors executed only if present in config
    if 'HOST_HAS_INDEX_SUGGESTIONS' in INCLUDED_EVENTS:
        process_host_has_index_suggestions_events(API_KEY, API_SECRET, projects_info, tables, days=NUM_DAYS_ACTIVITY)
    if 'CLUSTER_DELETE_SUBMITTED' in INCLUDED_EVENTS:
        process_cluster_delete_submitted_events(events_data, tables, cluster_to_project, project_id_to_name)
    if 'CLUSTER_UPDATE_SUBMITTED' in INCLUDED_EVENTS:
        process_cluster_update_submitted_events(events_data, tables, cluster_to_project)
    if 'DISK_AUTO_SCALE_INITIATED' in INCLUDED_EVENTS:
        process_disk_auto_scale_initiated_events(events_data, tables, cluster_to_project)
    # Generic processor handles remaining configured events
    process_generic_events(events_data, tables, cluster_to_project, project_id_to_name)
    # Fetch drop index suggestions count
    drop_indexes_count = get_total_drop_index_suggestions(API_KEY, API_SECRET, projects_info)
    html_content = generate_html_content(tables, version_counts, unique_projects, events_data, total_clusters, drop_indexes_count=drop_indexes_count)
    send_email(html_content, attachments)
    end_time = time.time()
    elapsed = end_time - start_time
    print(f"Script execution time: {elapsed:.2f} seconds")

if __name__ == "__main__":
    main()
