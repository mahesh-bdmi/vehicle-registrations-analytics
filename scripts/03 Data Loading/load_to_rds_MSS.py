"""
Loads the cleaned S3 data (final_df.csv, policies.csv, charging_stations.csv)
into the RDS SQL Server (Express) schema (schema_sqlserver.sql).

Connection settings are read from environment variables — nothing hardcoded:
    export DB_HOST=<your-rds-endpoint>
    export DB_NAME=vahan
    export DB_USER=sqlserver_admin
    export DB_PASSWORD=<your-master-password>

Requires the Microsoft ODBC Driver for SQL Server on EC2 first — see the
install steps that go with this script. Then:
    pip3.11 install pandas boto3 sqlalchemy pyodbc --break-system-packages
    python3.11 load_to_rds.py
"""

import io
import os
import urllib.parse

import boto3
import pandas as pd
from sqlalchemy import create_engine

BUCKET = "vahan-project-raw-486491621202-ap-south-1-an"
PROCESSED_PREFIX = "processed/"

# Charging station counts are a single snapshot pulled from the source on
# this date — every row in fact_charging_stations shares it.
CHARGING_STATIONS_AS_OF = "2025-12-16"

# SQL Server caps queries at 2100 parameters total. to_sql's "multi" method
# sends one INSERT per chunk with (rows x columns) parameters, so this must
# stay well under that regardless of how wide a given table is (a bit
# conservative on purpose, rather than tuning per-table).
CHUNK_SIZE = 200

s3 = boto3.client("s3")


def read_csv_from_s3(key: str) -> pd.DataFrame:
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    return pd.read_csv(io.BytesIO(obj["Body"].read()))


def get_engine():
    host = os.environ["DB_HOST"]
    db = os.environ["DB_NAME"]
    user = os.environ["DB_USER"]
    pwd = os.environ["DB_PASSWORD"]
    odbc_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={host},1433;DATABASE={db};UID={user};PWD={pwd};"
        f"Encrypt=yes;TrustServerCertificate=yes;"
    )
    url = "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(odbc_str)
    return create_engine(url)


def load_table(df: pd.DataFrame, table: str, engine, if_exists="append") -> None:
    df.to_sql(table, engine, if_exists=if_exists, index=False, method="multi", chunksize=CHUNK_SIZE)
    print(f"  Loaded {len(df)} row(s) into {table}")


def main():
    engine = get_engine()

    print("=== Reading cleaned data from S3 ===")
    final_df = read_csv_from_s3(f"{PROCESSED_PREFIX}final_df.csv")
    policies = read_csv_from_s3(f"{PROCESSED_PREFIX}policies.csv")
    charging_stations = read_csv_from_s3(f"{PROCESSED_PREFIX}charging_stations.csv")
    print(f"  final_df: {final_df.shape}, policies: {policies.shape}, charging_stations: {charging_stations.shape}")

    # ── 1. dim_state ─────────────────────────────────────────────────
    print("\n=== Loading dim_state ===")
    dim_state = (
        final_df[["state_id", "state_code", "state"]]
        .dropna(subset=["state_id"])
        .drop_duplicates()
        .rename(columns={"state": "state_name"})
    )
    dim_state["state_id"] = dim_state["state_id"].astype(int)
    load_table(dim_state, "dim_state", engine)

    # Lookup used below to attach state_id to policies/charging_stations,
    # which only have the state NAME, not the id.
    state_name_to_id = dict(zip(dim_state["state_name"], dim_state["state_id"]))

    # ── 2. dim_vehicle_class ─────────────────────────────────────────
    print("\n=== Loading dim_vehicle_class ===")
    dim_vehicle_class = (
        final_df[["vehicle_class", "vehicle_category"]]
        .dropna(subset=["vehicle_class"])
        .drop_duplicates()
    )
    load_table(dim_vehicle_class, "dim_vehicle_class", engine)

    # ── 3. dim_fuel_type ─────────────────────────────────────────────
    print("\n=== Loading dim_fuel_type ===")
    dim_fuel_type = pd.DataFrame({"fuel_type": final_df["fuel_type"].dropna().unique()})
    dim_fuel_type["is_electric"] = dim_fuel_type["fuel_type"].eq("electric")
    load_table(dim_fuel_type, "dim_fuel_type", engine)

    # ── 4. fact_registrations ────────────────────────────────────────
    print("\n=== Loading fact_registrations ===")
    fact_registrations = final_df[[
        "state_id", "vehicle_class", "fuel_type", "year", "month_number",
        "financial_year", "registrations",
    ]].dropna(subset=["state_id", "vehicle_class", "fuel_type", "registrations"]).copy()
    fact_registrations["state_id"] = fact_registrations["state_id"].astype(int)
    fact_registrations["registrations"] = fact_registrations["registrations"].astype(int)
    load_table(fact_registrations, "fact_registrations", engine)

    # ── 5. fact_population ───────────────────────────────────────────
    print("\n=== Loading fact_population ===")
    fact_population = (
        final_df[["state_id", "year", "population"]]
        .dropna()
        .drop_duplicates()
    )
    fact_population["state_id"] = fact_population["state_id"].astype(int)
    fact_population["population"] = fact_population["population"].astype(int)
    load_table(fact_population, "fact_population", engine)

    # ── 6. fact_gsdp ──────────────────────────────────────────────────
    print("\n=== Loading fact_gsdp ===")
    fact_gsdp = (
        final_df[["state_id", "financial_year", "gsdp_lakhs"]]
        .dropna(subset=["state_id", "financial_year"])  # gsdp_lakhs itself may be NULL (recent FYs)
        .drop_duplicates()
    )
    fact_gsdp["state_id"] = fact_gsdp["state_id"].astype(int)
    load_table(fact_gsdp, "fact_gsdp", engine)

    # ── 7. fact_charging_stations ────────────────────────────────────
    print("\n=== Loading fact_charging_stations ===")
    cs = charging_stations.rename(columns={
        "Total Chargers": "total_chargers",
        "Fast Chargers": "fast_chargers",
        "Slow Chargers": "slow_chargers",
    }).copy()
    cs["state_id"] = cs["State"].map(state_name_to_id)
    unmatched = cs[cs["state_id"].isna()]
    if len(unmatched):
        print(f"  WARNING: {len(unmatched)} charging_station row(s) didn't match a known state, skipping:")
        print(unmatched[["State"]].to_string(index=False))
    cs = cs.dropna(subset=["state_id"])
    cs["state_id"] = cs["state_id"].astype(int)
    cs["as_of_date"] = CHARGING_STATIONS_AS_OF
    cs = cs[["state_id", "as_of_date", "total_chargers", "fast_chargers", "slow_chargers"]]
    load_table(cs, "fact_charging_stations", engine)

    # ── 8. policy_events ──────────────────────────────────────────────
    print("\n=== Loading policy_events ===")
    pol = policies.rename(columns={
        "Policy Name": "policy_name",
        "Effective / Launch Date": "effective_date_raw",
        "Policy Focus & Key Highlights": "description",
    }).copy()
    pol["state_id"] = pol["State"].map(state_name_to_id)
    unmatched_pol = pol[pol["state_id"].isna()]
    if len(unmatched_pol):
        print(f"  WARNING: {len(unmatched_pol)} policy row(s) didn't match a known state:")
        print(unmatched_pol[["State"]].to_string(index=False))
        # Not dropped — state_id NULL is valid (means "national policy") in
        # this schema, but flagged above in case it's actually a typo.

    # Best-effort parse; anything that doesn't parse cleanly (e.g.
    # "September 2019 (Revised Feb 2023)") becomes NULL here but the
    # original text is preserved in effective_date_raw regardless.
    pol["effective_date"] = pd.to_datetime(pol["effective_date_raw"], errors="coerce").dt.date
    unparsed = pol[pol["effective_date"].isna()]
    if len(unparsed):
        print(f"  Note: {len(unparsed)} date(s) didn't parse cleanly (kept in effective_date_raw):")
        print(unparsed[["policy_name", "effective_date_raw"]].to_string(index=False))

    pol = pol[["state_id", "policy_name", "effective_date", "effective_date_raw", "description"]]
    load_table(pol, "policy_events", engine)

    print("\nDone.")


if __name__ == "__main__":
    main()
