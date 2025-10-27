#!/usr/bin/env python3
"""
Vehicle Position Data Extractor

This script reads all *.db files in the db/ directory and extracts vehicle_positions
entries into a unified CSV and Parquet format. It handles different database schemas
by analyzing each database structure and mapping columns appropriately.

Key features:
- Deduplication based on vehicle_id, timestamp, latitude, longitude, route_id
- Excludes trip_id as it's considered noisy data
- Exports to both CSV and Parquet formats
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
    def __init__(self, db_directory: str = "db"):
        self.db_directory = Path(db_directory)
        self.schemas: Dict[str, List[Dict]] = {}
        self.unified_columns: Set[str] = set()
        self.extracted_data: List[Dict] = []
        
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
    
    def extract_data_from_db(self, db_path: Path, schema: List[Dict]) -> List[Dict]:
        """Extract vehicle_positions data from a single database."""
        try:
            conn = sqlite3.connect(db_path)
            
            # Get column names and table name from schema
            columns = [col['name'] for col in schema]
            table_name = schema[0]['table_name'] if schema else 'vehicle_positions'
            
            # Query all data from the identified table
            query = f"SELECT {', '.join(columns)} FROM {table_name}"
            cursor = conn.cursor()
            cursor.execute(query)
            
            rows = cursor.fetchall()
            logger.info(f"Extracted {len(rows)} records from {db_path.name}")
            
            # Convert to list of dictionaries
            data = []
            for row in rows:
                record = dict(zip(columns, row))
                record['_source_db'] = db_path.name  # Add source database info
                data.append(record)
            
            conn.close()
            return data
            
        except sqlite3.Error as e:
            logger.error(f"Error extracting data from {db_path.name}: {e}")
            return []
    
    def harmonize_data(self, all_data: List[Dict]) -> pd.DataFrame:
        """Harmonize data from different schemas into a unified DataFrame."""
        if not all_data:
            logger.warning("No data to harmonize")
            return pd.DataFrame()
        
        # Create DataFrame
        df = pd.DataFrame(all_data)
        
        # Ensure all unified columns exist (fill missing with None)
        for col in self.unified_columns:
            if col not in df.columns:
                df[col] = None
        
        # Normalize column names (handle lat/latitude, lon/longitude variations)
        column_mapping = {}
        if 'lat' in df.columns and 'latitude' not in df.columns:
            column_mapping['lat'] = 'latitude'
        if 'lon' in df.columns and 'longitude' not in df.columns:
            column_mapping['lon'] = 'longitude'
        
        if column_mapping:
            df = df.rename(columns=column_mapping)
            logger.info(f"Renamed columns: {column_mapping}")
        
        # Remove duplicates based on key fields: vehicle_id, timestamp, latitude, longitude, route_id
        # Explicitly excluding trip_id as it's considered noisy data
        dedup_columns = ['vehicle_id', 'timestamp', 'latitude', 'longitude', 'route_id']
        available_dedup_columns = [col for col in dedup_columns if col in df.columns]
        
        if available_dedup_columns:
            initial_count = len(df)
            df = df.drop_duplicates(subset=available_dedup_columns, keep='first')
            final_count = len(df)
            duplicates_removed = initial_count - final_count
            logger.info(f"Removed {duplicates_removed:,} duplicate records based on columns: {available_dedup_columns}")
            logger.info(f"Records after deduplication: {final_count:,}")
        else:
            logger.warning("Could not perform deduplication - key columns not found")
        
        # Remove trip_id column if it exists (as per user request)
        if 'trip_id' in df.columns:
            df = df.drop(columns=['trip_id'])
            logger.info("Removed trip_id column as requested")
        
        # Reorder columns for consistency (common columns first, excluding trip_id)
        common_columns = [
            'id', 'vehicle_id', 'route_id',
            'latitude', 'longitude', 'bearing', 'speed',
            'timestamp', 'status', 'last_updated', 'created_at'
        ]
        
        # Add columns that exist in the data
        existing_common = [col for col in common_columns if col in df.columns]
        remaining_columns = [col for col in df.columns if col not in existing_common]
        
        final_column_order = existing_common + sorted(remaining_columns)
        df = df.reindex(columns=final_column_order)
        
        logger.info(f"Final harmonized data shape: {df.shape}")
        logger.info(f"Final columns: {list(df.columns)}")
        
        return df
    
    def export_data(self, df: pd.DataFrame, base_filename: str = "vehicle_positions"):
        """Export data to both CSV and Parquet formats."""
        if df.empty:
            logger.warning("No data to export")
            return
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Export to CSV
        csv_filename = f"{base_filename}_{timestamp}.csv"
        df.to_csv(csv_filename, index=False)
        logger.info(f"Data exported to CSV: {csv_filename}")
        
        # Export to Parquet (if pyarrow is available)
        try:
            parquet_filename = f"{base_filename}_{timestamp}.parquet"
            df.to_parquet(parquet_filename, index=False)
            logger.info(f"Data exported to Parquet: {parquet_filename}")
        except ImportError:
            logger.warning("pyarrow not available. Install with 'pip install pyarrow' for Parquet export.")
        except Exception as e:
            logger.error(f"Error exporting to Parquet: {e}")
        
        # Print summary statistics
        self.print_summary(df)
    
    def print_summary(self, df: pd.DataFrame):
        """Print summary statistics about the extracted data."""
        print("\n" + "="*50)
        print("EXTRACTION SUMMARY")
        print("="*50)
        print(f"Total unique records extracted: {len(df):,}")
        print(f"Total columns: {len(df.columns)}")
        print(f"Source databases: {df['_source_db'].nunique()}")
        
        if '_source_db' in df.columns:
            print("\nRecords per database:")
            source_counts = df['_source_db'].value_counts()
            for db, count in source_counts.items():
                print(f"  {db}: {count:,}")
        
        # Show unique vehicles and routes
        if 'vehicle_id' in df.columns:
            unique_vehicles = df['vehicle_id'].nunique()
            print(f"\nUnique vehicles: {unique_vehicles:,}")
        
        if 'route_id' in df.columns:
            unique_routes = df['route_id'].nunique()
            print(f"Unique routes: {unique_routes:,}")
        
        # Show time range
        if 'timestamp' in df.columns:
            try:
                df['timestamp_dt'] = pd.to_datetime(df['timestamp'], unit='s', errors='coerce')
                min_time = df['timestamp_dt'].min()
                max_time = df['timestamp_dt'].max()
                if pd.notna(min_time) and pd.notna(max_time):
                    print(f"Time range: {min_time} to {max_time}")
            except:
                pass
        
        # Show data types
        print(f"\nColumn data types:")
        for col, dtype in df.dtypes.items():
            non_null_count = df[col].notna().sum()
            print(f"  {col}: {dtype} ({non_null_count:,} non-null)")
        
        # Show sample data
        print(f"\nSample data (first 3 rows):")
        print(df.head(3).to_string())
        print("="*50)
    
    def run(self):
        """Main execution method."""
        logger.info("Starting vehicle position extraction...")
        
        # Find database files
        db_files = self.find_db_files()
        if not db_files:
            logger.error(f"No .db files found in {self.db_directory}")
            return
        
        # Analyze schemas
        for db_path in db_files:
            schema = self.analyze_schema(db_path)
            if schema:
                self.schemas[str(db_path)] = schema
        
        if not self.schemas:
            logger.error("No valid vehicle_positions tables found in any database")
            return
        
        logger.info(f"Found vehicle position tables in {len(self.schemas)} databases")
        logger.info(f"Unified column set: {sorted(self.unified_columns)}")
        
        # Extract data from all databases
        all_data = []
        for db_path_str, schema in self.schemas.items():
            db_path = Path(db_path_str)
            data = self.extract_data_from_db(db_path, schema)
            all_data.extend(data)
        
        if not all_data:
            logger.error("No data extracted from any database")
            return
        
        logger.info(f"Total raw records extracted: {len(all_data):,}")
        
        # Harmonize and export data
        df = self.harmonize_data(all_data)
        self.export_data(df)
        
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
