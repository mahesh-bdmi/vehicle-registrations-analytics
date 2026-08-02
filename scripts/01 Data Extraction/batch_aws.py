import argparse
import csv
import subprocess
import sys
from pathlib import Path
import shlex
import os

import boto3
from botocore.exceptions import ClientError

CSV_FILE = "fuel_types.csv"

# Set this to your bucket name (see ec2-s3-write-policy.json)
S3_BUCKET = "vahan-project-raw-486491621202-ap-south-1-an"
S3_PREFIX = "scraped"  # objects land at s3://<bucket>/scraped/<out_file>

# scraper.py's --out defaults to "vahan_data" and is never overridden in
# BASE_COMMAND, so every CSV actually lands at vahan_data/<out_file> —
# not at <out_file> directly.
OUT_DIR = "vahan_data"

# scraper.py always writes its per-state progress tracker to
# <cwd>/vahan_data/_completed_states.txt (out_dir = abspath(args.out)).
# This must be deleted before EVERY fuel-type batch, or the 2nd+ fuel
# type in the CSV will see a stale "done" list and skip every state.
COMPLETED_STATES_FILE = Path(OUT_DIR) / "_completed_states.txt"

BASE_COMMAND = [
    sys.executable,  # Uses the current Python interpreter
    "scraper.py",
    "--yaxis",
    "Vehicle Class",
    "--xaxis",
    "Month Wise",
    "--state",
    "ALL",
    "--start-year",
    "2017",
    "--end-year",
    "2026",
    "--concurrency",
    "5",
    "--aggregate-only",
]

# No access keys here on purpose — boto3 picks up credentials automatically
# from the EC2 instance's attached IAM role (vahan-scraper-role).
s3 = boto3.client("s3")


def upload_to_s3(local_path: str, bucket: str, prefix: str) -> bool:
    key = f"{prefix}/{Path(local_path).name}"
    try:
        s3.upload_file(local_path, bucket, key)
        print(f"  Uploaded to s3://{bucket}/{key}")
        return True
    except ClientError as e:
        print(f"  [S3 UPLOAD FAILED] {e}")
        return False


def run_scrape_and_collect(csv_file: str) -> list[Path]:
    """Runs the scraper for every fuel type in csv_file, returns the local
    paths of every file that completed successfully (exit code 0)."""
    completed_files = []
    with open(csv_file, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        print(reader.fieldnames)
        for row in reader:
            if COMPLETED_STATES_FILE.exists():
                COMPLETED_STATES_FILE.unlink()
                print(f"Reset progress tracker: {COMPLETED_STATES_FILE}")
            else:
                print(f"No progress tracker to reset yet: {COMPLETED_STATES_FILE}")

            fuel_type = row["fuel_types"]
            fuel_types = fuel_type.split("|")

            out_file = row["out"]
            local_path = Path(OUT_DIR) / out_file

            local_path.parent.mkdir(parents=True, exist_ok=True)

            command = BASE_COMMAND + ["--fuel", *fuel_types, "--out-file", out_file]

            print(shlex.join(command))
            print(f"\nRunning: {fuel_types}")

            result = subprocess.run(command)

            if result.returncode == 0:
                print(f"Completed: {fuel_type}")
                completed_files.append(local_path)
            else:
                print(f"Failed: {fuel_type} (Exit code {result.returncode})")

    return completed_files


def collect_existing_files(csv_file: str) -> list[Path]:
    """--upload-only mode: skip scraping entirely, just check which files
    from the CSV's `out` column already exist locally and upload those.
    Nothing here touches the VAHAN site."""
    found_files = []
    missing_files = []
    with open(csv_file, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            local_path = Path(OUT_DIR) / row["out"]
            if local_path.exists() and local_path.stat().st_size > 0:
                found_files.append(local_path)
            else:
                missing_files.append(local_path)

    if missing_files:
        print(f"Note: {len(missing_files)} file(s) from {csv_file} were not found locally and will be skipped:")
        for p in missing_files:
            print(f"  {p}")

    return found_files


def upload_all(files: list[Path]) -> None:
    print(f"\nUploading {len(files)} file(s) to S3...")
    failed_uploads = []
    for local_path in files:
        if not upload_to_s3(str(local_path), S3_BUCKET, S3_PREFIX):
            failed_uploads.append(local_path)

    if failed_uploads:
        print(f"\n{len(failed_uploads)} file(s) failed to upload:")
        for p in failed_uploads:
            print(f"  {p}")
    else:
        print("\nAll files uploaded successfully.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--upload-only",
        action="store_true",
        help="Skip scraping entirely; just upload whatever files from "
        "fuel_types.csv's `out` column already exist in vahan_data/. "
        "Use this to retry a failed S3 upload without re-hitting VAHAN.",
    )
    args = parser.parse_args()

    if args.upload_only:
        files_to_upload = collect_existing_files(CSV_FILE)
    else:
        files_to_upload = run_scrape_and_collect(CSV_FILE)

    upload_all(files_to_upload)