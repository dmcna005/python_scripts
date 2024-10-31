import requests
from requests.auth import HTTPDigestAuth

# Replace with your MongoDB Atlas API credentials and project details
PUBLIC_KEY = 'your_public_key'
PRIVATE_KEY = 'your_private_key'
PROJECT_ID = 'your_project_id'

# Base URL for the MongoDB Atlas API
BASE_URL = f"https://cloud.mongodb.com/api/atlas/v1.0/groups/{PROJECT_ID}/accessList"

# Function to retrieve all IPs in the MongoDB Atlas access list
def get_all_ips():
    response = requests.get(BASE_URL, auth=HTTPDigestAuth(PUBLIC_KEY, PRIVATE_KEY))
    if response.status_code == 200:
        access_list = response.json()
        return [entry['ipAddress'] for entry in access_list['results']]
    else:
        print(f"Failed to retrieve access list. Status code: {response.status_code}, Response: {response.text}")
        return []

# Function to delete an IP from the MongoDB Atlas access list
def delete_ip(ip_address):
    url = f"{BASE_URL}/{ip_address}"
    response = requests.delete(url, auth=HTTPDigestAuth(PUBLIC_KEY, PRIVATE_KEY))
    if response.status_code == 204:
        print(f"IP address {ip_address} successfully deleted.")
    else:
        print(f"Failed to delete IP {ip_address}. Status code: {response.status_code}, Response: {response.text}")

# Main function to delete all IPs or a provided list of IPs
def delete_ips(ips_to_delete=None):
    if ips_to_delete is None:
        ips_to_delete = get_all_ips()  # Retrieve all IPs if none are provided

    if not ips_to_delete:
        print("No IPs to delete.")
        return

    for ip in ips_to_delete:
        delete_ip(ip)

# Usage
# To delete all IPs, call delete_ips() without arguments
delete_ips()

# To delete specific IPs, provide a list of IPs to delete
# delete_ips(["192.0.2.1", "203.0.113.5", "198.51.100.42"])
