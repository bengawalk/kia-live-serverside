# Pipeline Automation Guide

This guide explains how to use the automated pipeline script (`run_pipeline.sh`) to set up and run the KIA Live Server data pipeline.

## Quick Start

```bash
# Run the complete pipeline
./run_pipeline.sh

# Run with options
./run_pipeline.sh --skip-download --no-copy
```

## Overview

The pipeline automates these steps from `steps.txt`:

1. **Download database backups from R2** - Fetches backup files from Cloudflare R2 storage
2. **Extract vehicle positions** - Converts SQLite data to CSV for ML training
3. **Train ML model** - Trains the universal prediction model
4. **Generate input files** - Fetches latest data from BMTC API and generates GTFS files

## Prerequisites

### 1. Install Poetry

```bash
curl -sSL https://install.python-poetry.org | python3 -
```

### 2. Install Dependencies

```bash
poetry install
```

### 3. Set Up R2 Credentials (for Step 1)

Create a `.env` file with your R2 credentials:

```bash
cp .env.example .env
# Edit .env and add your credentials
```

Required variables:
- `R2_ACCOUNT_ID`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_BUCKET_NAME`

See `R2_SETUP_GUIDE.md` for detailed setup instructions.

## Usage

### Run Complete Pipeline

```bash
./run_pipeline.sh
```

This will:
1. Download all database backups from R2
2. Extract vehicle positions to CSV
3. Train the universal prediction model
4. Generate input files from BMTC API
5. Copy generated files to `in/` directory

### Command-Line Options

```bash
./run_pipeline.sh [OPTIONS]
```

Available options:

| Option | Description |
|--------|-------------|
| `--skip-download` | Skip downloading from R2 (use existing files in `db/`) |
| `--skip-extract` | Skip extracting vehicle positions (use existing CSV) |
| `--skip-train` | Skip training model (use existing model in `models/`) |
| `--skip-generate` | Skip generating input files (use existing files) |
| `--no-copy` | Don't copy generated files to `in/` directory |
| `--help` | Show help message |

### Common Scenarios

#### First-Time Setup

```bash
# Complete pipeline with all steps
./run_pipeline.sh
```

#### Update Input Files Only

```bash
# Skip download, extract, and training - just regenerate input files
./run_pipeline.sh --skip-download --skip-extract --skip-train
```

#### Retrain Model with Existing Data

```bash
# Skip download and extract, but retrain model and regenerate files
./run_pipeline.sh --skip-download --skip-extract
```

#### Test Generated Files Without Copying

```bash
# Generate files but don't copy to in/ directory
./run_pipeline.sh --skip-download --skip-extract --skip-train --no-copy
```

#### Use Local Database Files

If you already have database files in `db/`:

```bash
# Skip R2 download
./run_pipeline.sh --skip-download
```

## Pipeline Steps Detail

### Step 1: Download from R2

**Script:** `download_from_r2.py`

Downloads database backup files from Cloudflare R2 storage to `db/` directory.

**Options:**
```bash
# Download only the latest backup
poetry run python download_from_r2.py --latest-only

# Download files matching a pattern
poetry run python download_from_r2.py --pattern "database-2025*.db"
```

**Skip if:**
- You already have database files in `db/`
- You're using a local development database

### Step 2: Extract Vehicle Positions

**Script:** `extract_vehicle_positions.py`

Extracts vehicle position data from SQLite database(s) to CSV format for ML training.

**Output:** `db/vehicle_positions.csv`

**Skip if:**
- You already have `db/vehicle_positions.csv`
- You're not retraining the model

### Step 3: Train Model

**Command:**
```bash
poetry run python -m src.model.cli train-universal \
    --vehicle-positions db/vehicle_positions.csv \
    --stops-data in/client_stops.json \
    --model-dir models
```

Trains the universal stop-to-stop prediction model using historical vehicle position data.

**Output:** Model files in `models/` directory

**Skip if:**
- You already have a trained model in `models/`
- You're just updating input files without retraining

**Note:** Requires `in/client_stops.json` to exist. If it doesn't exist, you may need to:
1. Run step 4 first with existing data, OR
2. Manually create/copy `client_stops.json` from a previous run

### Step 4: Generate Input Files

**Script:** `generate_in_files.py`

Fetches latest data from BMTC API and generates input files for the GTFS feeds.

**Flags used:**
- `-s` - Generate client_stops.json with stop information
- `-r` - Generate routelines.json with route polylines
- `-t` - Generate timings.tsv with schedule data from API
- `-tdb` - Generate times.json with ML predictions (requires trained model)
- `-c` - Copy generated files to `in/` directory (unless `--no-copy` is set)

**Output Files (in `generated_in/`):**
- `route_children_ids.json` - Route ID mappings
- `route_parent_ids.json` - Parent route ID mappings
- `client_stops.json` - Stop locations and information
- `routelines.json` - Encoded route polylines
- `timings.tsv` - Schedule timings
- `times.json` - ML-predicted stop-by-stop times

**Skip if:**
- You don't need to refresh data from BMTC API
- You're only retraining the model

## Directory Structure

```
.
├── db/                      # Database files
│   ├── database.db          # Current/downloaded database
│   └── vehicle_positions.csv # Extracted vehicle positions
├── generated_in/            # Generated input files (staging)
│   ├── route_children_ids.json
│   ├── route_parent_ids.json
│   ├── client_stops.json
│   ├── routelines.json
│   ├── timings.tsv
│   └── times.json
├── in/                      # Production input files
│   ├── routes_children_ids.json
│   ├── routes_parent_ids.json
│   ├── client_stops.json
│   ├── routelines.json
│   ├── times.json
│   └── helpers/
│       ├── construct_stops/
│       │   └── client_stops.json
│       └── construct_timings/
│           └── timings.tsv
└── models/                  # Trained ML models
    └── universal_model.pkl
```

## Troubleshooting

### Error: "pyproject.toml not found"

Make sure you're running the script from the project root directory:

```bash
cd /path/to/kia-live-serverside
./run_pipeline.sh
```

### Error: "Poetry is not installed"

Install Poetry first:

```bash
curl -sSL https://install.python-poetry.org | python3 -
```

### Error: "Missing required environment variables"

Set up your `.env` file with R2 credentials. See `R2_SETUP_GUIDE.md`.

### Error: "No database files found in db/"

Either:
1. Don't use `--skip-download` (let it download from R2), OR
2. Manually place database files in `db/` directory

### Error: "in/client_stops.json not found" (during model training)

This happens when training a model for the first time without existing input files. Solutions:

1. **Option A:** Run step 4 first with existing data:
   ```bash
   # Generate input files without model predictions
   poetry run python generate_in_files.py -s -r -t
   # Then run full pipeline
   ./run_pipeline.sh --skip-download --skip-extract
   ```

2. **Option B:** Copy from a previous run:
   ```bash
   cp /path/to/old/client_stops.json in/client_stops.json
   ```

### Error: "Model training failed"

Check that:
- `db/vehicle_positions.csv` exists and has data
- `in/client_stops.json` exists
- You have enough disk space
- Dependencies are installed: `poetry install`

### Pipeline stops midway

The script uses `set -e`, so it will stop on any error. Check the error message and:
1. Fix the issue
2. Re-run with appropriate `--skip-*` flags to resume from where it failed

## Performance Tips

### Speed Up Model Training

- Use `--skip-download` and `--skip-extract` if data hasn't changed
- Model training time depends on CSV size (can take 5-30 minutes)

### Speed Up File Generation

- API calls are rate-limited, expect 5-10 minutes for full generation
- Files are cached in `generated_in/` - review before copying to `in/`

### Disk Space

Typical space requirements:
- Database backups: ~30-100 MB each
- vehicle_positions.csv: ~50-500 MB (depends on time range)
- Models: ~10-50 MB
- Generated files: ~1-5 MB total

## Next Steps After Pipeline

Once the pipeline completes successfully:

1. **Review generated files:**
   ```bash
   ls -lh generated_in/
   ```

2. **Start the server:**
   ```bash
   poetry run python -m src.main
   ```

3. **Access endpoints:**
   - Static GTFS: `http://localhost:59966/gtfs.zip`
   - Real-time GTFS-RT: `http://localhost:59966/gtfs-rt.proto`
   - WebSocket stream: `ws://localhost:59966/ws/gtfs-rt`

4. **Monitor logs** for any issues

## Automation

### Cron Job for Regular Updates

Update input files daily at 3 AM:

```bash
# Edit crontab
crontab -e

# Add this line (adjust path)
0 3 * * * cd /path/to/kia-live-serverside && ./run_pipeline.sh --skip-download --skip-extract --skip-train >> logs/pipeline.log 2>&1
```

### Systemd Service

For production deployment, consider creating a systemd service to run the server after pipeline completion.

## See Also

- `steps.txt` - Original manual steps
- `R2_SETUP_GUIDE.md` - R2 credentials setup
- `CLAUDE.md` - Project architecture and development guide
- `DATABASE_LOCK_ROOT_CAUSE_ANALYSIS.md` - Database optimization details