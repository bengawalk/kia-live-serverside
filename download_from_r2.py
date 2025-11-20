#!/usr/bin/env python3
"""
Download database backup files from Cloudflare R2 storage and delete them from R2.

This script downloads all database backup files from the R2 bucket's
'backups/' folder to the local 'db/' directory, then deletes the successfully
downloaded files from R2 to free up cloud storage space.

Environment Variables Required:
    R2_ACCOUNT_ID: Cloudflare R2 account ID
    R2_ACCESS_KEY_ID: R2 API access key ID
    R2_SECRET_ACCESS_KEY: R2 API secret access key
    R2_BUCKET_NAME: R2 bucket name

Usage:
    python download_from_r2.py [--latest-only] [--pattern PATTERN]

Options:
    --latest-only    Download only the most recent backup file
    --pattern        Download only files matching this pattern (e.g., "database-2025*.db")

Note:
    Files are only deleted from R2 after successful download to local storage.
    If a download fails, the R2 file is preserved.
"""

import os
import sys
from datetime import datetime

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:
    print("Error: boto3 library not found.")
    print("Install it with: poetry add boto3")
    sys.exit(1)


def format_size(size_bytes):
    """Format bytes to human-readable size."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"


def get_r2_client():
    """Create and return R2 S3 client."""
    # Get credentials from environment
    account_id = os.getenv('R2_ACCOUNT_ID')
    access_key_id = os.getenv('R2_ACCESS_KEY_ID')
    secret_access_key = os.getenv('R2_SECRET_ACCESS_KEY')
    bucket_name = os.getenv('R2_BUCKET_NAME')

    # Check for missing credentials
    missing = []
    if not account_id:
        missing.append('R2_ACCOUNT_ID')
    if not access_key_id:
        missing.append('R2_ACCESS_KEY_ID')
    if not secret_access_key:
        missing.append('R2_SECRET_ACCESS_KEY')
    if not bucket_name:
        missing.append('R2_BUCKET_NAME')

    if missing:
        print("Error: Missing required environment variables:")
        for var in missing:
            print(f"  - {var}")
        print("\nPlease set them in your .env file or environment.")
        print("See R2_SETUP_GUIDE.md for instructions.")
        sys.exit(1)

    # Create R2 client
    endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"

    try:
        s3_client = boto3.client(
            's3',
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name='auto'
        )
        return s3_client, bucket_name
    except Exception as e:
        print(f"Error creating R2 client: {e}")
        sys.exit(1)


def list_backup_files(s3_client, bucket_name, prefix='backups/', pattern=None):
    """List all backup files in R2 bucket."""
    try:
        response = s3_client.list_objects_v2(Bucket=bucket_name, Prefix=prefix)

        if 'Contents' not in response:
            print(f"No files found in bucket '{bucket_name}' with prefix '{prefix}'")
            return []

        files = []
        for obj in response['Contents']:
            key = obj['Key']
            # Skip directory markers
            if key.endswith('/'):
                continue

            # Apply pattern filter if provided
            if pattern and pattern not in key:
                continue

            files.append({
                'key': key,
                'size': obj['Size'],
                'last_modified': obj['LastModified']
            })

        # Sort by last modified date (newest first)
        files.sort(key=lambda x: x['last_modified'], reverse=True)
        return files

    except ClientError as e:
        print(f"Error listing files: {e}")
        sys.exit(1)


def download_file(s3_client, bucket_name, key, local_path):
    """Download a file from R2 to local path."""
    try:
        # Ensure local directory exists
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        # Download file
        print(f"  Downloading: {key}")
        s3_client.download_file(bucket_name, key, local_path)

        # Get file size
        size = os.path.getsize(local_path)
        print(f"  Downloaded: {format_size(size)} -> {local_path}")
        return True

    except ClientError as e:
        print(f"  Error downloading {key}: {e}")
        return False


def delete_file_from_r2(s3_client, bucket_name, key):
    """Delete a file from R2 bucket."""
    try:
        print(f"  Deleting from R2: {key}")
        s3_client.delete_object(Bucket=bucket_name, Key=key)
        print(f"  Deleted from R2: {key}")
        return True
    except ClientError as e:
        print(f"  Error deleting {key} from R2: {e}")
        return False


def main():
    """Main function."""
    print("=" * 60)
    print("Download Database Backups from Cloudflare R2")
    print("=" * 60)
    print()

    # Parse arguments
    latest_only = '--latest-only' in sys.argv
    pattern = None

    if '--pattern' in sys.argv:
        try:
            pattern_index = sys.argv.index('--pattern')
            pattern = sys.argv[pattern_index + 1]
        except (IndexError, ValueError):
            print("Error: --pattern requires an argument")
            sys.exit(1)

    # Get R2 client
    s3_client, bucket_name = get_r2_client()

    # List backup files
    print(f"Listing backup files in bucket '{bucket_name}'...")
    files = list_backup_files(s3_client, bucket_name, pattern=pattern)

    if not files:
        print("No backup files found.")
        sys.exit(0)

    print(f"Found {len(files)} backup file(s):")
    for i, file_info in enumerate(files, 1):
        print(f"  {i}. {file_info['key']} ({format_size(file_info['size'])}) - {file_info['last_modified']}")

    print()

    # Determine which files to download
    if latest_only:
        files_to_download = [files[0]]
        print(f"Downloading latest file only: {files[0]['key']}")
    else:
        files_to_download = files
        print(f"Downloading all {len(files)} file(s)...")

    print()

    # Download files
    success_count = 0
    fail_count = 0
    delete_count = 0
    delete_fail_count = 0

    for file_info in files_to_download:
        key = file_info['key']
        # Create local path (remove 'backups/' prefix if present)
        local_filename = key.replace('backups/', '')
        local_path = os.path.join('db', local_filename)

        # Check if file already exists
        file_already_existed = False
        if os.path.exists(local_path):
            local_size = os.path.getsize(local_path)
            remote_size = file_info['size']

            if local_size == remote_size:
                print(f"  Skipping: {local_filename} (already exists with same size)")
                success_count += 1
                file_already_existed = True
                # Don't continue - still try to delete from R2
            else:
                print(f"  File exists but size differs (local: {format_size(local_size)}, remote: {format_size(remote_size)})")
                print(f"  Re-downloading...")

        # Download file (if not already existed)
        download_success = file_already_existed or download_file(s3_client, bucket_name, key, local_path)

        if download_success:
            if not file_already_existed:
                success_count += 1

            # Delete from R2 after successful download
            if delete_file_from_r2(s3_client, bucket_name, key):
                delete_count += 1
            else:
                delete_fail_count += 1
        else:
            fail_count += 1

    print()
    print("=" * 60)
    print("Download and Cleanup Summary")
    print("=" * 60)
    print(f"Total files: {len(files_to_download)}")
    print(f"Successfully downloaded: {success_count}")
    print(f"Download failures: {fail_count}")
    print(f"Deleted from R2: {delete_count}")
    print(f"Deletion failures: {delete_fail_count}")
    print()

    if fail_count > 0:
        print("Some files failed to download. Check errors above.")
        sys.exit(1)
    elif delete_fail_count > 0:
        print("All files downloaded successfully, but some failed to delete from R2.")
        print("Check errors above.")
        print()
        print(f"Downloaded files are in: db/")
        print()
        # Exit with 0 since downloads succeeded
        sys.exit(0)
    else:
        print("All files downloaded successfully and deleted from R2!")
        print()
        print(f"Downloaded files are in: db/")
        print()


if __name__ == '__main__':
    if '--help' in sys.argv or '-h' in sys.argv:
        print(__doc__)
        sys.exit(0)

    main()