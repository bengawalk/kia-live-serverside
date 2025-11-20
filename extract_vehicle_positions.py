#!/usr/bin/env python3
"""
Vehicle Position Data Extractor (Memory-Efficient Version)

This script reads all *.db files in the db/ directory and extracts vehicle_positions
entries into a unified CSV format. It processes each database file incrementally,
appending to the output CSV file instead of loading all data into memory.

Key features:
- Memory-efficient: Processes one database at a time
- Incremental writes: Appends to CSV after each database
- Deduplication: Removes duplicates within each database chunk
- Excludes trip_id as it's considered noisy data
"""

import sqlite3
import pandas as pd
import os
import sys
from pathlib import Path
from typing import Dict, List, Set, Any, Optional
import logging
from datetime import datetime

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class VehiclePositionExtractor:
    def __init__(self, db_directory: str = "db", output_file: str = "db/vehicle_positions.csv"):
        self.db_directory = Path(db_directory)
        self.output_file = output_file
        self.unified_columns: Set[str] = set()
        self.total_records_written = 0
        self.total_duplicates_removed = 0
        self.db_stats = []
        self.file_initialized = False

    def find_db_files(self) -> List[Path]:
        """Find all .db files in the specified directory."""
        db_files = list(self.db_directory.glob("*.db"))
        logger.info(f"Found {len(db_files)} database files: {[f.name for f in db_files]}")
        return db_files

    def find_vehicle_tables(self, db_path: Path) -> List[str]:
        """Find tables that might contain vehicle position data."""
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            # Get all table names
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            all_tables = [row[0] for row in cursor.fetchall()]

            # Look for tables that might contain vehicle data
            vehicle_tables = []
            for table in all_tables:
                if any(keyword in table.lower() for keyword in ['vehicle', 'position', 'live', 'trip', 'bus']):
                    vehicle_tables.append(table)

            conn.close()
            return vehicle_tables

        except sqlite3.Error as e:
            logger.error(f"Error finding tables in {db_path.name}: {e}")
            return []

    def analyze_schema(self, db_path: Path) -> Optional[List[Dict]]:
        """Analyze the schema of vehicle_positions table in a database."""
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            # Look for tables that might contain vehicle data
            vehicle_tables = self.find_vehicle_tables(db_path)

            # Prioritize common table names (note: vehicle_positions is plural)
            preferred_tables = ['vehicle_positions', 'vehicle_position', 'live_data', 'vehicles', 'positions']
            table_name = None

            for preferred in preferred_tables:
                if preferred in vehicle_tables:
                    table_name = preferred
                    break

            if not table_name and vehicle_tables:
                table_name = vehicle_tables[0]  # Use the first one found

            if not table_name:
                logger.warning(f"No vehicle-related tables found in {db_path.name}")
                conn.close()
                return None

            logger.info(f"Using table '{table_name}' from {db_path.name}")

            # Get table schema
            cursor.execute(f"PRAGMA table_info({table_name})")
            schema = cursor.fetchall()

            schema_info = []
            for col in schema:
                col_info = {
                    'cid': col[0],
                    'name': col[1],
                    'type': col[2],
                    'notnull': col[3],
                    'default_value': col[4],
                    'pk': col[5],
                    'table_name': table_name
                }
                schema_info.append(col_info)
                self.unified_columns.add(col[1])

            conn.close()
            logger.info(f"Schema for {db_path.name} (table: {table_name}): {[col['name'] for col in schema_info]}")
            return schema_info

        except sqlite3.Error as e:
            logger.error(f"Error analyzing schema for {db_path.name}: {e}")
            return None

    def process_and_write_db(self, db_path: Path, schema: List[Dict], is_first: bool = False):
        """
        Extract data from a single database, process it, and append to CSV.
        This is memory-efficient as it processes one database at a time.
        """
        try:
            logger.info(f"Processing {db_path.name}...")

            # Connect to database
            conn = sqlite3.connect(db_path)

            # Get column names and table name from schema
            columns = [col['name'] for col in schema]
            table_name = schema[0]['table_name'] if schema else 'vehicle_positions'

            # Query all data from the identified table
            query = f"SELECT {', '.join(columns)} FROM {table_name}"

            # Read data into pandas DataFrame
            df = pd.read_sql_query(query, conn)
            conn.close()

            initial_count = len(df)
            logger.info(f"Extracted {initial_count:,} records from {db_path.name}")

            if df.empty:
                logger.warning(f"No data in {db_path.name}, skipping")
                return

            # Add source database info
            df['_source_db'] = db_path.name

            # Normalize column names (handle lat/latitude, lon/longitude variations)
            column_mapping = {}
            if 'lat' in df.columns and 'latitude' not in df.columns:
                column_mapping['lat'] = 'latitude'
            if 'lon' in df.columns and 'longitude' not in df.columns:
                column_mapping['lon'] = 'longitude'

            if column_mapping:
                df = df.rename(columns=column_mapping)
                logger.info(f"Renamed columns: {column_mapping}")

            # Remove duplicates within this database
            dedup_columns = ['vehicle_id', 'timestamp', 'latitude', 'longitude', 'route_id']
            available_dedup_columns = [col for col in dedup_columns if col in df.columns]

            if available_dedup_columns:
                df = df.drop_duplicates(subset=available_dedup_columns, keep='first')
                final_count = len(df)
                duplicates_removed = initial_count - final_count
                self.total_duplicates_removed += duplicates_removed
                logger.info(f"Removed {duplicates_removed:,} duplicates from {db_path.name}")
            else:
                final_count = len(df)
                logger.warning("Could not perform deduplication - key columns not found")

            # Remove trip_id column if it exists
            if 'trip_id' in df.columns:
                df = df.drop(columns=['trip_id'])
                logger.info("Removed trip_id column")

            # Reorder columns for consistency
            common_columns = [
                'id', 'vehicle_id', 'route_id',
                'latitude', 'longitude', 'bearing', 'speed',
                'timestamp', 'status', 'last_updated', 'created_at'
            ]

            existing_common = [col for col in common_columns if col in df.columns]
            remaining_columns = [col for col in df.columns if col not in existing_common]
            final_column_order = existing_common + sorted(remaining_columns)
            df = df.reindex(columns=final_column_order)

            # Write to CSV (append mode after first write)
            write_header = is_first
            mode = 'w' if is_first else 'a'

            df.to_csv(self.output_file, mode=mode, header=write_header, index=False)

            self.total_records_written += len(df)
            self.db_stats.append({
                'database': db_path.name,
                'raw_records': initial_count,
                'duplicates_removed': duplicates_removed if available_dedup_columns else 0,
                'records_written': final_count
            })

            logger.info(f"Appended {final_count:,} records from {db_path.name} to {self.output_file}")

            # Clear DataFrame from memory
            del df

        except sqlite3.Error as e:
            logger.error(f"Database error processing {db_path.name}: {e}")
        except Exception as e:
            logger.error(f"Error processing {db_path.name}: {e}")
            import traceback
            traceback.print_exc()

    def print_summary(self):
        """Print summary statistics about the extraction."""
        print("\n" + "="*60)
        print("EXTRACTION SUMMARY")
        print("="*60)
        print(f"Total databases processed: {len(self.db_stats)}")
        print(f"Total unique records written: {self.total_records_written:,}")
        print(f"Total duplicates removed: {self.total_duplicates_removed:,}")
        print(f"Output file: {self.output_file}")

        if os.path.exists(self.output_file):
            file_size_mb = os.path.getsize(self.output_file) / (1024 * 1024)
            print(f"Output file size: {file_size_mb:.2f} MB")

        print("\nPer-database statistics:")
        print(f"{'Database':<30} {'Raw':<12} {'Duplicates':<12} {'Written':<12}")
        print("-" * 60)
        for stat in self.db_stats:
            print(f"{stat['database']:<30} {stat['raw_records']:<12,} "
                  f"{stat['duplicates_removed']:<12,} {stat['records_written']:<12,}")

        print("="*60)

    def run(self):
        """Main execution method - processes databases incrementally."""
        logger.info("Starting memory-efficient vehicle position extraction...")

        # Find database files
        db_files = self.find_db_files()
        if not db_files:
            logger.error(f"No .db files found in {self.db_directory}")
            return

        # First pass: Analyze schemas to build unified column set
        logger.info("Analyzing database schemas...")
        schemas = {}
        for db_path in db_files:
            schema = self.analyze_schema(db_path)
            if schema:
                schemas[str(db_path)] = schema

        if not schemas:
            logger.error("No valid vehicle_positions tables found in any database")
            return

        logger.info(f"Found vehicle position tables in {len(schemas)} databases")
        logger.info(f"Unified column set: {sorted(self.unified_columns)}")

        # Delete existing output file if it exists
        if os.path.exists(self.output_file):
            logger.info(f"Removing existing output file: {self.output_file}")
            os.remove(self.output_file)

        # Second pass: Process each database incrementally
        logger.info("Processing databases and writing to CSV incrementally...")
        is_first = True
        for db_path_str, schema in schemas.items():
            db_path = Path(db_path_str)
            self.process_and_write_db(db_path, schema, is_first=is_first)
            is_first = False

        if self.total_records_written == 0:
            logger.error("No data extracted from any database")
            return

        # Print summary
        self.print_summary()

        logger.info("Vehicle position extraction completed successfully!")


def main():
    """Main entry point."""
    # Check if db directory exists
    if not os.path.exists("db"):
        print("Error: 'db' directory not found in current directory")
        sys.exit(1)

    # Create extractor and run
    extractor = VehiclePositionExtractor()
    extractor.run()


if __name__ == "__main__":
    main()