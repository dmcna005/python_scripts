# Author: Dwayne McNab
# License: GENU
import argparse
import json
import os
import requests
from requests.auth import HTTPDigestAuth
from cryptography.fernet import Fernet

# Load configruation from JSON file
with open('atlas_config.json', 'r') as config_file:
    config = json.load(config_file)

# Decrypt credentials
def decrypt_credential(encrypted_cred, key):
    f = Fernet(key.encode())
    return f.decrypt(encrypted_cred.encode()).decode()

# MongoDB Atlas API credentials
ENCRYPTION_KEY = config['encryption_key']
PUBLIC_KEY = decrypt_credential(config['api_key'], ENCRYPTION_KEY) 
PRIVATE_KEY = decrypt_credential(config['api_secret'], ENCRYPTION_KEY)

# Use version 2 of the API, It is more secure
BASE_URL = 'https://cloud.mongodb.com/api/atlas/v2'
# BASE_URL = 'https://cloud.mongodb.com/api/atlas/v1.0'
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/vnd.atlas.2024-11-13+json"
    # "Accept": "applicaion/json"
}

# Function to get alerts of a specific cluster and filter required fields
def get_alerts(project_id):
    url = f"{BASE_URL}/groups/{project_id}/alertConfigs"
    response = requests.get(url, auth=HTTPDigestAuth(PUBLIC_KEY, PRIVATE_KEY), headers=HEADERS, verify=False )
    
    if response.status_code == 200:
        alerts = response.json()['results']
        
        # Filter only alerts with matching project_id (groupId)
        filtered_alerts = []
        for alert in alerts:
            if alert.get('groupId') == project_id:
                filtered_alert = {
                    'enabled': alert.get('enabled'),
                    'eventTypeName': alert['eventTypeName'],  # Required field
                    'matchers': alert.get('matchers', []),  # Defaults to an empty array if not present
                    'notifications': alert['notifications']  # Required field
                }
                filtered_alerts.append(filtered_alert)
        
        # Save the filtered alerts to a configuration file
        with open(f'{project_id}_alerts.json', 'w') as f:
            json.dump(filtered_alerts, f, indent=4)
        
        print(f"Filtered alerts for project '{project_id}' saved to {project_id}_alerts.json")
    else:
        print(f"Failed to retrieve alerts: {response.status_code}, {response.text}")

# Function to create new alerts from a config file
def create_alerts(project_id, cluster_name, config_file):
    url = f"{BASE_URL}/groups/{project_id}/alertConfigs"

    with open(config_file, 'r') as f:
        alerts = json.load(f)

    for alert in alerts:
        # Update the alert with the new cluster name if needed
        alert['clusterName'] = cluster_name

        response = requests.post(url, auth=HTTPDigestAuth(PUBLIC_KEY, PRIVATE_KEY), headers=HEADERS, data=json.dumps(alert), verify=False )

        if response.status_code == 201:
            print(f"Successfully created alert for cluster '{cluster_name}'")
        else:
            print(f"Failed to create alert: {response.status_code}, {response.text}")

# Function to delete alerts for a project
def delete_alerts(project_id):
    url = f"{BASE_URL}/groups/{project_id}/alertConfigs"

    # Fetch all the alerts
    response = requests.get(url, auth=HTTPDigestAuth(PUBLIC_KEY, PRIVATE_KEY), headers=HEADERS )

    if response.status_code == 200:
        alerts = response.json()['results']
        if not alerts:
            print(f"No alerts found for project: {project_id}")
            return
        
        # Loop through all the alerts by their alert ID
        for alert in alerts:
            alertConfigId = alert['id']  # Each alert has a unique 'id' {alertConfigId}
            del_url = f"{BASE_URL}/groups/{project_id}/alertConfigs/{alertConfigId}"
            del_response = requests.delete(del_url, auth=HTTPDigestAuth(PUBLIC_KEY, PRIVATE_KEY), headers=HEADERS )

            if del_response.status_code == 204:
                print(f"Successfully deleted alert ID: {alertConfigId}")
            else:
                print(f"Failed to delete alert ID: {alertConfigId}: {del_response.status_code}, {del_response.text}")
    else:
        print(f"Failed to retrieve alerts: {response.status_code}, {response.text}")

# Function to add an email notification to alerts
def add_email_notification(project_id, email, alert_id=None):
    def get_delay_and_interval(alert_config):
        # Try to extract delayMin and intervalMin from any existing notification
        if 'notifications' in alert_config and alert_config['notifications']:
            first_notification = alert_config['notifications'][0]
            return first_notification.get('delayMin', 1), first_notification.get('intervalMin', 5)
        return 1, 5  # Default values if no notifications exist

    def email_exists(alert_config, email):
        return any(
            notification.get('typeName') == 'EMAIL' and notification.get('emailAddress') == email
            for notification in alert_config.get('notifications', [])
        )

    def add_email_to_alert(alert_config, email):
        delayMin, intervalMin = get_delay_and_interval(alert_config)
        email_notification = {
            "typeName": "EMAIL",
            "emailAddress": email,
            "delayMin": delayMin,
            "intervalMin": intervalMin,
        }
        alert_config['notifications'].append(email_notification)

    if alert_id:
        # Single alert
        url = f"{BASE_URL}/groups/{project_id}/alertConfigs/{alert_id}"
        response = requests.get(url, auth=HTTPDigestAuth(PUBLIC_KEY, PRIVATE_KEY), headers=HEADERS, verify=False)

        if response.status_code == 200:
            alert_config = response.json()
            if email_exists(alert_config, email):
                print(f"Email '{email}' already exists in alert ID: {alert_id}. Skipping.")
                return

            add_email_to_alert(alert_config, email)

            update_response = requests.put(url, auth=HTTPDigestAuth(PUBLIC_KEY, PRIVATE_KEY), headers=HEADERS, data=json.dumps(alert_config), verify=False)
            if update_response.status_code == 200:
                print(f"Added email '{email}' to alert ID: {alert_id}.")
            else:
                print(f"Failed to update alert ID {alert_id}: {update_response.status_code}, {update_response.text}")
        else:
            print(f"Failed to fetch alert ID {alert_id}: {response.status_code}, {response.text}")
    else:
        # All alerts
        url = f"{BASE_URL}/groups/{project_id}/alertConfigs"
        response = requests.get(url, auth=HTTPDigestAuth(PUBLIC_KEY, PRIVATE_KEY), headers=HEADERS, verify=False)

        if response.status_code == 200:
            alerts = response.json()['results']
            for alert in alerts:
                alert_id = alert['id']
                if email_exists(alert, email):
                    print(f"Email '{email}' already exists in alert ID: {alert_id}. Skipping.")
                    continue

                add_email_to_alert(alert, email)

                alert_url = f"{BASE_URL}/groups/{project_id}/alertConfigs/{alert_id}"
                update_response = requests.put(alert_url, auth=HTTPDigestAuth(PUBLIC_KEY, PRIVATE_KEY), headers=HEADERS, data=json.dumps(alert), verify=False)

                if update_response.status_code == 200:
                    print(f"Added email '{email}' to alert ID: {alert_id}.")
                else:
                    print(f"Failed to update alert ID {alert_id}: {update_response.status_code}, {update_response.text}")
        else:
            print(f"Failed to fetch alerts: {response.status_code}") 


def delete_email_notification(project_id, email, alert_id=None):
    if alert_id:
        # Delete email from a specific alert
        url = f"{BASE_URL}/groups/{project_id}/alertConfigs/{alert_id}"
        response = requests.get(url, auth=HTTPDigestAuth(PUBLIC_KEY, PRIVATE_KEY), headers=HEADERS, verify=False)

        if response.status_code == 200:
            alert_config = response.json()

            # Ensure notifications exist
            if 'notifications' in alert_config:
                # Remove the email notification
                alert_config['notifications'] = [
                    notification for notification in alert_config['notifications']
                    if notification.get('emailAddress') != email
                ]

                # Update the alert configuration
                update_response = requests.put(url, auth=HTTPDigestAuth(PUBLIC_KEY, PRIVATE_KEY), headers=HEADERS, data=json.dumps(alert_config), verify=False)

                if update_response.status_code == 200:
                    print(f"Successfully deleted email notification for alert ID: {alert_id}")
                else:
                    print(f"Failed to update alert configuration: {update_response.status_code}, {update_response.text}")
            else:
                print(f"No notifications found for alert ID: {alert_id}")
        else:
            print(f"Failed to fetch alert configuration: {response.status_code}, {response.text}")
    else:
        # Delete email from all alerts
        url = f"{BASE_URL}/groups/{project_id}/alertConfigs"
        response = requests.get(url, auth=HTTPDigestAuth(PUBLIC_KEY, PRIVATE_KEY), headers=HEADERS, verify=False)

        if response.status_code == 200:
            alerts = response.json()['results']
            for alert in alerts:
                alert_id = alert['id']

                # Ensure notifications exist
                if 'notifications' in alert:
                    # Remove the email notification
                    alert['notifications'] = [
                        notification for notification in alert['notifications']
                        if notification.get('emailAddress') != email
                    ]

                    # Update the alert configuration
                    alert_url = f"{BASE_URL}/groups/{project_id}/alertConfigs/{alert_id}"
                    update_response = requests.put(alert_url, auth=HTTPDigestAuth(PUBLIC_KEY, PRIVATE_KEY), headers=HEADERS, data=json.dumps(alert), verify=False)

                    if update_response.status_code == 200:
                        print(f"Successfully deleted email notification from alert ID: {alert_id}")
                    else:
                        print(f"Failed to update alert ID {alert_id}: {update_response.status_code}, {update_response.text}")
                else:
                    print(f"No notifications found for alert ID: {alert_id}")
        else:
            print(f"Failed to fetch alerts: {response.status_code}, {response.text}")

if __name__ == "__main__":
    # Command-line argument parsing
    parser = argparse.ArgumentParser(description="Manage MongoDB Atlas Alerts")
    parser.add_argument('--action', choices=['get', 'create', 'delete', 'add-email', 'delete-email'], required=True, help="Action to perform: 'get', 'create', 'delete', 'add-email', or 'delete-email'")
    parser.add_argument('--projectId', required=True, help="The MongoDB Atlas project ID")
    parser.add_argument('--clusterName', help="The name of the cluster (required for 'create' action)")
    parser.add_argument('--configFile', help="The config file to use when creating alerts (required for 'create' action)")
    parser.add_argument('--alertId', help="The alert ID to add or delete email notification to/from (optional for 'add-email' and 'delete-email' actions)")
    parser.add_argument('--email', help="The email address to add or delete for notifications (required for 'add-email' and 'delete-email' actions)")

    args = parser.parse_args()

    if args.action == 'get':
        get_alerts(args.projectId)
    elif args.action == 'create':
        if not args.configFile or not args.clusterName:
            print("Error: both configFile and clusterName are required for 'create' action")
        else:
            create_alerts(args.projectId, args.clusterName, args.configFile)
    elif args.action == 'delete':
        delete_alerts(args.projectId)
    elif args.action == 'add-email':
        if not args.email:
            print("Error: email is required for 'add-email' action")
        else:
            add_email_notification(args.projectId, args.email, args.alertId)
    elif args.action == 'delete-email':
        if not args.email:
            print("Error: email is required for 'delete-email' action")
        else:
            delete_email_notification(args.projectId, args.email, args.alertId)
