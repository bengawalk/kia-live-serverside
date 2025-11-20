#!/bin/bash

################################################################################
# KIA Live Server Pipeline
#
# This script automates the complete pipeline for setting up the KIA live
# transit data service, following the steps outlined in steps.txt:
#
# 1. Download database backups from R2 bucket
# 2. Extract vehicle positions to CSV
# 3. Train ML prediction model
# 4. Generate input files from BMTC API
#
# Usage:
#   ./run_pipeline.sh [options]
#
# Options:
#   --skip-download      Skip step 1 (downloading from R2)
#   --skip-extract       Skip step 2 (extracting vehicle positions)
#   --skip-train         Skip step 3 (training model)
#   --skip-generate      Skip step 4 (generating input files)
#   --no-copy            Don't copy generated files to in/ directory
#   --help               Show this help message
#
# Environment:
#   Requires R2 credentials in .env file for step 1:
#   - R2_ACCOUNT_ID
#   - R2_ACCESS_KEY_ID
#   - R2_SECRET_ACCESS_KEY
#   - R2_BUCKET_NAME
################################################################################

set -e  # Exit on error
set -u  # Exit on undefined variable

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Parse command line arguments
SKIP_DOWNLOAD=false
SKIP_EXTRACT=false
SKIP_TRAIN=false
SKIP_GENERATE=false
NO_COPY=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-download)
            SKIP_DOWNLOAD=true
            shift
            ;;
        --skip-extract)
            SKIP_EXTRACT=true
            shift
            ;;
        --skip-train)
            SKIP_TRAIN=true
            shift
            ;;
        --skip-generate)
            SKIP_GENERATE=true
            shift
            ;;
        --no-copy)
            NO_COPY=true
            shift
            ;;
        --help)
            head -n 30 "$0" | tail -n 27
            exit 0
            ;;
        *)
            echo -e "${RED}Error: Unknown option $1${NC}"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Helper functions
print_header() {
    echo ""
    echo -e "${BLUE}============================================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}============================================================${NC}"
    echo ""
}

print_step() {
    echo -e "${GREEN}[STEP] $1${NC}"
}

print_info() {
    echo -e "${YELLOW}[INFO] $1${NC}"
}

print_error() {
    echo -e "${RED}[ERROR] $1${NC}"
}

print_success() {
    echo -e "${GREEN}[SUCCESS] $1${NC}"
}

# Check if we're in the correct directory
if [ ! -f "pyproject.toml" ]; then
    print_error "pyproject.toml not found. Please run this script from the project root."
    exit 1
fi

# Check if poetry is installed
if ! command -v poetry &> /dev/null; then
    print_error "Poetry is not installed. Please install it first:"
    echo "  curl -sSL https://install.python-poetry.org | python3 -"
    exit 1
fi

print_header "KIA Live Server Data Pipeline"

################################################################################
# STEP 1: Download database backups from R2 bucket
################################################################################

if [ "$SKIP_DOWNLOAD" = false ]; then
    print_header "Step 1: Download Database Backups from R2"

    print_step "Checking for R2 download script..."

    # Check if download script exists
    if [ -f "download_from_r2.py" ]; then
        print_step "Running R2 download script..."

        # Load environment variables if .env exists
        if [ -f ".env" ]; then
            print_info "Loading environment variables from .env"
            set -a
            source .env
            set +a
        else
            print_error ".env file not found. Please create it with R2 credentials."
            print_info "See R2_SETUP_GUIDE.md for instructions."
            exit 1
        fi

        # Run download script
        poetry run python download_from_r2.py
        print_success "Database backups downloaded successfully"
    else
        print_info "No download script found (download_from_r2.py)"
        print_info "Assuming database files are already in db/ directory"
        print_info "If you need to download from R2, create download_from_r2.py or manually download files"

        # Check if db directory exists and has files
        if [ ! -d "db" ] || [ -z "$(ls -A db/*.db 2>/dev/null)" ]; then
            print_error "No database files found in db/ directory"
            print_info "Please ensure database backup files are in db/ directory"
            exit 1
        fi
    fi
else
    print_info "Skipping Step 1: Download from R2 (--skip-download flag set)"
fi

################################################################################
# STEP 2: Extract vehicle positions to CSV
################################################################################

if [ "$SKIP_EXTRACT" = false ]; then
    print_header "Step 2: Extract Vehicle Positions to CSV"

    print_step "Checking for database files..."

    # Check if database files exist
    DB_COUNT=$(ls -1 db/*.db 2>/dev/null | wc -l)
    if [ "$DB_COUNT" -eq 0 ]; then
        print_error "No database files found in db/ directory"
        exit 1
    fi

    print_info "Found $DB_COUNT database file(s) in db/ directory"

    print_step "Running extract_vehicle_positions.py..."
    poetry run python extract_vehicle_positions.py

    # Check if CSV was created
    if [ ! -f "db/vehicle_positions.csv" ]; then
        print_error "Failed to create db/vehicle_positions.csv"
        exit 1
    fi

    # Show CSV file size
    CSV_SIZE=$(du -h db/vehicle_positions.csv | cut -f1)
    print_success "Vehicle positions extracted successfully (Size: $CSV_SIZE)"
else
    print_info "Skipping Step 2: Extract vehicle positions (--skip-extract flag set)"

    # Still check if CSV exists
    if [ ! -f "db/vehicle_positions.csv" ]; then
        print_error "db/vehicle_positions.csv not found. Cannot skip this step."
        exit 1
    fi
fi

################################################################################
# STEP 3: Train ML prediction model
################################################################################

if [ "$SKIP_TRAIN" = false ]; then
    print_header "Step 3: Train Universal Prediction Model"

    print_step "Checking prerequisites..."

    # Check if vehicle_positions.csv exists
    if [ ! -f "db/vehicle_positions.csv" ]; then
        print_error "db/vehicle_positions.csv not found. Run step 2 first."
        exit 1
    fi

    # Check if client_stops.json exists
    if [ ! -f "in/client_stops.json" ]; then
        print_error "in/client_stops.json not found. Please ensure input files exist."
        print_info "You may need to run step 4 first with existing data, or provide client_stops.json manually."
        exit 1
    fi

    print_step "Training universal model..."
    print_info "This may take several minutes depending on data size..."

    # Create models directory if it doesn't exist
    mkdir -p models

    # Train the model
    poetry run python -m src.model.cli train-universal \
        --vehicle-positions db/vehicle_positions.csv \
        --stops-data in/client_stops.json \
        --model-dir models

    # Check if model was created
    if [ ! -d "models" ] || [ -z "$(ls -A models 2>/dev/null)" ]; then
        print_error "Model training failed - no model files created"
        exit 1
    fi

    print_success "Model trained successfully and saved to models/"
else
    print_info "Skipping Step 3: Train model (--skip-train flag set)"

    # Check if model exists
    if [ ! -d "models" ] || [ -z "$(ls -A models 2>/dev/null)" ]; then
        print_error "No trained model found in models/ directory. Cannot skip this step."
        exit 1
    fi
fi

################################################################################
# STEP 4: Generate input files from BMTC API
################################################################################

if [ "$SKIP_GENERATE" = false ]; then
    print_header "Step 4: Generate Input Files from BMTC API"

    print_step "Running generate_in_files.py..."
    print_info "This will fetch data from BMTC API and may take several minutes..."

    # Determine flags
    GENERATE_FLAGS="-s -r -t -tdb"

    if [ "$NO_COPY" = false ]; then
        GENERATE_FLAGS="$GENERATE_FLAGS -c"
        print_info "Files will be copied to in/ directory"
    else
        print_info "Files will NOT be copied to in/ directory (--no-copy flag set)"
    fi

    print_info "Running with flags: $GENERATE_FLAGS"

    # Run the generation script
    poetry run python generate_in_files.py $GENERATE_FLAGS

    # Check if files were generated
    if [ ! -d "generated_in" ] || [ -z "$(ls -A generated_in 2>/dev/null)" ]; then
        print_error "File generation failed - no files created in generated_in/"
        exit 1
    fi

    # List generated files
    print_info "Generated files:"
    ls -lh generated_in/

    print_success "Input files generated successfully"
else
    print_info "Skipping Step 4: Generate input files (--skip-generate flag set)"
fi

################################################################################
# Pipeline Complete
################################################################################

print_header "Pipeline Completed Successfully!"

print_info "Summary:"
echo "  - Database files: db/"
echo "  - Vehicle positions CSV: db/vehicle_positions.csv"
echo "  - Trained models: models/"
echo "  - Generated input files: generated_in/"
if [ "$NO_COPY" = false ] && [ "$SKIP_GENERATE" = false ]; then
    echo "  - Production input files: in/"
fi

print_info "Next steps:"
echo "  1. Review generated files in generated_in/"
echo "  2. Start the server: poetry run python -m src.main"
echo "  3. Access endpoints at http://localhost:59966"
echo "     - GET /gtfs.zip - Static GTFS feed"
echo "     - GET /gtfs-rt.proto - Real-time GTFS feed"
echo "     - GET /ws/gtfs-rt - WebSocket real-time stream"

echo ""
print_success "All done! 🚀"
echo ""