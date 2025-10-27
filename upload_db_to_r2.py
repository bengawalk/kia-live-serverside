#!/usr/bin/env python3
"""
Upload database.db to Cloudflare R2 storage and remove it locally.

Required environment variables:
- R2_ACCOUNT_ID: Your Cloudflare account ID
- R2_ACCESS_KEY_ID: Your R2 access key ID
- R2_SECRET_ACCESS_KEY: Your R2 secret access key
- R2_BUCKET_NAME: The name of your R2 bucket

Usage:
    python upload_db_to_r2.py
"""

import os
import sys
import time
from datetime import datetime
import boto3
from botocore.exceptions import ClientError

# Configuration
DB_FILE_PATH = "db/database.db"
R2_OBJECT_KEY = f"backups/database-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
MAX_RETRIES = 5
INITIAL_RETRY_DELAY = 2  # seconds

def get_r2_client():
    """Create and return an R2 client using boto3."""
    account_id = os.getenv("R2_ACCOUNT_ID")
    access_key_id = os.getenv("R2_ACCESS_KEY_ID")
    secret_access_key = os.getenv("R2_SECRET_ACCESS_KEY")

    if not all([account_id, access_key_id, secret_access_key]):
        print("Error: Missing required environment variables")
        print("Please set: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY")
        sys.exit(1)

    # Cloudflare R2 endpoint
    endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto"  # R2 uses auto region
    )

def upload_file_with_retry(s3_client, bucket_name, file_size):
    """
    Upload file with retry logic and exponential backoff.

    Returns:
        bool: True if upload successful, False otherwise
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"Uploading to R2 bucket '{bucket_name}' as '{R2_OBJECT_KEY}'...")
            if attempt > 1:
                print(f"  Attempt {attempt}/{MAX_RETRIES}")

            s3_client.upload_file(
                DB_FILE_PATH,
                bucket_name,
                R2_OBJECT_KEY,
                Callback=lambda bytes_transferred: print(
                    f"  Uploaded: {bytes_transferred / (1024*1024):.2f} MB",
                    end="\r"
                )
            )
            print(f"\nSuccessfully uploaded to R2: {R2_OBJECT_KEY}")
            return True

        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            print(f"\n  ClientError ({error_code}): {e}")

            if attempt < MAX_RETRIES:
                delay = INITIAL_RETRY_DELAY * (2 ** (attempt - 1))  # Exponential backoff
                print(f"  Retrying in {delay} seconds...")
                time.sleep(delay)
            else:
                print(f"  Max retries ({MAX_RETRIES}) reached")
                return False

        except Exception as e:
            print(f"\n  Unexpected error: {e}")

            if attempt < MAX_RETRIES:
                delay = INITIAL_RETRY_DELAY * (2 ** (attempt - 1))
                print(f"  Retrying in {delay} seconds...")
                time.sleep(delay)
            else:
                print(f"  Max retries ({MAX_RETRIES}) reached")
                return False

    return False


def upload_to_r2():
    """Upload the database file to R2 storage with retry logic."""
    bucket_name = os.getenv("R2_BUCKET_NAME")

    if not bucket_name:
        print("Error: R2_BUCKET_NAME environment variable not set")
        sys.exit(1)

    file_size = os.path.getsize(DB_FILE_PATH)
    print(f"Found database file: {DB_FILE_PATH} ({file_size / (1024*1024):.2f} MB)")

    # Create R2 client
    s3_client = get_r2_client()

    # Upload file with retry logic
    upload_success = upload_file_with_retry(s3_client, bucket_name, file_size)

    if upload_success:
        # Remove local file only after successful upload
        try:
            print(f"Removing local file: {DB_FILE_PATH}")
            os.remove(DB_FILE_PATH)
            print("Local file removed successfully")
            return True
        except Exception as e:
            print(f"Warning: Failed to remove local file: {e}")
            print("Upload was successful, but local file remains")
            return True
    else:
        print("Upload failed after all retry attempts")
        print(f"Local file preserved at: {DB_FILE_PATH}")
        return False

def main():
    """Main entry point."""
    # Fast exit if database file doesn't exist
    if not os.path.exists(DB_FILE_PATH):
        print(f"No database file found at {DB_FILE_PATH}, skipping backup")
        sys.exit(0)

    print("=" * 60)
    print("Database Backup to Cloudflare R2")
    print("=" * 60)

    success = upload_to_r2()

    if success:
        print("\nBackup completed successfully!")
    else:
        print("\nBackup failed after all retry attempts!")
        print("The script will exit gracefully (exit code 0)")

    # Always exit with code 0 to prevent error propagation
    sys.exit(0)

if __name__ == "__main__":
    main()