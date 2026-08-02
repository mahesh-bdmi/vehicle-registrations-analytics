"""
Cleans and merges the raw VAHAN scrape output + supporting data, all read
from and written back to S3. This is a direct port of data_cleaning.ipynb's
logic — same transformations, just S3 I/O instead of local folders.

Run on the EC2 instance (same one used for scraping):
    pip3.11 install pandas boto3 openpyxl --break-system-packages  # if needed
    python3.11 clean_data_s3.py
"""

import io
import os

import boto3
import numpy as np
import pandas as pd

BUCKET = "vahan-project-raw-486491621202-ap-south-1-an"
RAW_PREFIX = "scraped/"                # source: per-fuel-type CSVs
SUPPORTING_PREFIX = "supporting_data/"  # source: gsdp.csv, support.xlsx
OUTPUT_PREFIX = "processed/"            # destination: cleaned/merged output

s3 = boto3.client("s3")


# ── S3 helpers ────────────────────────────────────────────────────────────

def list_keys(bucket: str, prefix: str) -> list[str]:
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith("/"):  # skip "folder" placeholders
                keys.append(obj["Key"])
    return keys


def read_csv_from_s3(bucket: str, key: str, **kwargs) -> pd.DataFrame:
    obj = s3.get_object(Bucket=bucket, Key=key)
    return pd.read_csv(io.BytesIO(obj["Body"].read()), **kwargs)


def read_excel_from_s3(bucket: str, key: str, **kwargs):
    obj = s3.get_object(Bucket=bucket, Key=key)
    return pd.read_excel(io.BytesIO(obj["Body"].read()), **kwargs)


def write_csv_to_s3(df: pd.DataFrame, bucket: str, key: str) -> None:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    s3.put_object(Bucket=bucket, Key=key, Body=buf.getvalue())
    print(f"  Wrote s3://{bucket}/{key} ({len(df)} rows)")


# ── Static lookup tables (unchanged from the notebook) ──────────────────

MONTH_MAP = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
}

VEHICLE_CAT = {
    'Agricultural Vehicle': [
        'AGRICULTURAL TRACTOR', 'POWER TILLER', 'HARVESTER',
        'TRAILER (AGRICULTURAL)', 'POWER TILLER (COMMERCIAL)',
        'TRACTOR (COMMERCIAL)', 'PULLER TRACTOR'
    ],
    'Bus': [
        'OMNI BUS (PRIVATE USE)', 'BUS', 'SCHOOL BUS',
        'EDUCATIONAL INSTITUTION BUS', 'OMNI BUS'
    ],
    'Car': ['MOTOR CAR'],
    'Construction & Industrial Equipment': [
        'FORK LIFT', 'CRANE MOUNTED VEHICLE', 'CONSTRUCTION EQUIPMENT VEHICLE',
        'ROAD ROLLER', 'EXCAVATOR (NT)', 'BULLDOZER',
        'EARTH MOVING EQUIPMENT', 'EXCAVATOR (COMMERCIAL)',
        'CONSTRUCTION EQUIPMENT VEHICLE (COMMERCIAL)'
    ],
    'Emergency Vehicle': [
        'AMBULANCE', 'ANIMAL AMBULANCE', 'FIRE TENDERS',
        'SNORKED LADDERS', 'FIRE FIGHTING VEHICLE', 'HEARSES'
    ],
    'Goods Vehicle': [
        'GOODS CARRIER', 'AUXILIARY TRAILER', 'ARTICULATED VEHICLE',
        'DUMPER', 'TRAILER (COMMERCIAL)', 'TRACTOR-TROLLEY(COMMERCIAL)',
        'SEMI-TRAILER (COMMERCIAL)', 'MODULAR HYDRAULIC TRAILER'
    ],
    'Quadricycle': ['QUADRICYCLE (PRIVATE)', 'QUADRICYCLE (COMMERCIAL)'],
    'Recreational Vehicle': [
        'CAMPER VAN / TRAILER (PRIVATE USE)', 'TRAILER FOR PERSONAL USE',
        'MOTOR CARAVAN', 'CAMPER VAN / TRAILER'
    ],
    'Service Vehicle': [
        'PRIVATE SERVICE VEHICLE (INDIVIDUAL USE)', 'PRIVATE SERVICE VEHICLE'
    ],
    'Special Purpose Vehicle': [
        'VEHICLE FITTED WITH RIG', 'VEHICLE FITTED WITH GENERATOR',
        'VEHICLE FITTED WITH COMPRESSOR', 'TOW TRUCK', 'BREAKDOWN VAN',
        'RECOVERY VEHICLE', 'TOWER WAGON', 'TREE TRIMMING VEHICLE',
        'ARMOURED/SPECIALISED VEHICLE', 'MOBILE WORKSHOP', 'CASH VAN',
        'ADAPTED VEHICLE', 'MOBILE CLINIC', 'X-RAY VAN', 'LIBRARY VAN',
        'MOBILE CANTEEN'
    ],
    'Taxi / Cab': ['LUXURY CAB', 'MAXI CAB', 'MOTOR CAB'],
    'Three Wheeler': [
        'E-RICKSHAW WITH CART (G)', 'THREE WHEELER (GOODS)',
        'THREE WHEELER (PERSONAL)', 'E-RICKSHAW(P)',
        'THREE WHEELER (PASSENGER)'
    ],
    'Two Wheeler': [
        'MOTOR CYCLE/SCOOTER-SIDECAR(T)', 'MOTOR CYCLE/SCOOTER-WITH TRAILER',
        'M-CYCLE/SCOOTER', 'M-CYCLE/SCOOTER-WITH SIDE CAR', 'MOPED',
        'MOTORISED CYCLE (CC > 25CC)', 'MOTOR CYCLE/SCOOTER-USED FOR HIRE'
    ],
    'Vintage Vehicle': ['VINTAGE MOTOR VEHICLE'],
}
VEHICLE_LOOKUP = {
    vehicle: category
    for category, vehicles in VEHICLE_CAT.items()
    for vehicle in vehicles
}

# Vetted state-name reconciliation map (from the notebook's rapidfuzz pass,
# manually confirmed). Applied identically to every source's state column.
STATE_MAPPING = {
    'All States': 'All States',
    'Andaman & Nicobar (UT)': 'Andaman and Nicobar Islands',
    'Andaman & Nicobar Island': 'Andaman and Nicobar Islands',
    'Andaman & Nicobar Islands': 'Andaman and Nicobar Islands',
    'Andaman And Nicobar Islands': 'Andaman and Nicobar Islands',
    'Andhra Pradesh': 'Andhra Pradesh',
    'Arunachal Pradesh': 'Arunachal Pradesh',
    'Assam': 'Assam',
    'Bihar': 'Bihar',
    'Chandigarh': 'Chandigarh',
    'Chandigarh (UT)': 'Chandigarh',
    'Chhattisgarh': 'Chattisgarh',
    'DNH and DD (UT)': 'Dadra & Nagar Haveli and Daman & Diu',
    'Dadra and Nagar Haveli and Daman and Diu': 'Dadra & Nagar Haveli and Daman & Diu',
    'Delhi': 'Delhi',
    'Goa': 'Goa',
    'Gujarat': 'Gujarat',
    'Haryana': 'Haryana',
    'Himachal Pradesh': 'Himachal Pradesh',
    'Jammu & Kashmir': 'Jammu and Kashmir',
    'Jammu & Kashmir*': 'Jammu and Kashmir',
    'Jammu And Kashmir': 'Jammu and Kashmir',
    'Jammu and Kashmir': 'Jammu and Kashmir',
    'Jharkhand': 'Jharkhand',
    'Karnataka': 'Karnataka',
    'Kerala': 'Kerala',
    'Ladakh': 'Ladakh',
    'Ladakh (UT)': 'Ladakh',
    'Lakshadweep': 'Lakshadweep Islands',
    'Lakshadweep (UT)': 'Lakshadweep Islands',
    'Madhya Pradesh': 'Madhya Pradesh',
    'Maharashtra': 'Maharashtra',
    'Manipur': 'Manipur',
    'Meghalaya': 'Meghalaya',
    'Mizoram': 'Mizoram',
    'NCT of Delhi': 'Delhi',
    'Nagaland': 'Nagaland',
    'Odisha': 'Odisha',
    'Puducherry': 'Pondicherry',
    'Puducherry (UT)': 'Pondicherry',
    'Punjab': 'Punjab',
    'Rajasthan': 'Rajasthan',
    'Sikkim': 'Sikkim',
    'Tamil Nadu': 'Tamil Nadu',
    'Telangana': 'Telangana',
    'Tripura': 'Tripura',
    'UT of DNH and DD': 'Dadra & Nagar Haveli and Daman & Diu',
    'Uttar Pradesh': 'Uttar Pradesh',
    'Uttarakhand': 'Uttarakhand',
    'West Bengal': 'West Bengal',
}


# ── Step 1: combine the per-fuel-type VAHAN CSVs from S3 ────────────────

def load_and_combine_vahan(bucket: str, prefix: str) -> pd.DataFrame:
    keys = list_keys(bucket, prefix)
    if not keys:
        raise RuntimeError(f"No files found under s3://{bucket}/{prefix}")
    print(f"Found {len(keys)} raw file(s) under s3://{bucket}/{prefix}")

    first_df = read_csv_from_s3(bucket, keys[0])
    expected_columns = list(first_df.columns)

    df_list = []
    for key in keys:
        fuel_name = os.path.splitext(os.path.basename(key))[0]
        df = read_csv_from_s3(bucket, key)
        if list(df.columns) != expected_columns:
            print(f"  Column mismatch in {key}: {list(df.columns)} — reindexing")
            df = df.reindex(columns=expected_columns)
        df['fuel_type'] = fuel_name
        df_list.append(df)

    combined_df = pd.concat(df_list, ignore_index=True)
    print(f"  Combined shape: {combined_df.shape}")
    return combined_df


def clean_vahan(combined_df: pd.DataFrame) -> pd.DataFrame:
    df = combined_df.copy()
    df.columns = df.columns.str.lower().str.replace(' ', '_')
    df = df[['year', 'month_wise', 'state', 'vehicle_class', 'fuel_type', 'value']]

    df['value'] = pd.to_numeric(
        df['value'].astype(str).str.replace(',', '').str.strip(), errors='coerce'
    )

    df['vehicle_category'] = df['vehicle_class'].map(VEHICLE_LOOKUP)
    df['month_number'] = df['month_wise'].map(MONTH_MAP)

    df["financial_year"] = np.where(
        df["month_number"] >= 4,
        df["year"].astype(str) + "-" + (df["year"] + 1).astype(str).str[2:],
        (df["year"] - 1).astype(str) + "-" + df["year"].astype(str).str[2:],
    )

    df['state'] = df['state'].map(STATE_MAPPING)
    return df


# ── Step 2: supporting data (gsdp.csv, support.xlsx) from S3 ────────────

def load_gsdp(bucket: str, prefix: str) -> pd.DataFrame:
    gsdp = read_csv_from_s3(bucket, f"{prefix}gsdp.csv")
    gsdp_melted = gsdp.melt(
        id_vars="State/Union Territory", var_name="financial_year", value_name="gsdp_lakhs"
    )
    gsdp_melted['gsdp_lakhs'] = pd.to_numeric(
        gsdp_melted['gsdp_lakhs'].astype(str).str.replace(',', '').str.strip(),
        errors='coerce',
    ).astype("Int64")
    gsdp_melted.columns = gsdp_melted.columns.str.replace('/', '_').str.replace(' ', '_')
    gsdp_melted = gsdp_melted.rename(columns={'State_Union_Territory': 'state'})
    gsdp_melted['state'] = gsdp_melted['state'].map(STATE_MAPPING)
    return gsdp_melted


def load_support_workbook(bucket: str, prefix: str) -> dict[str, pd.DataFrame]:
    dfs = read_excel_from_s3(bucket, f"{prefix}support.xlsx", sheet_name=None)
    print(f"  Sheets found: {list(dfs.keys())}")
    return dfs


def clean_population(population: pd.DataFrame) -> pd.DataFrame:
    population = population.rename(columns={'State/Union Territory': 'State'})
    population['State'] = population['State'].map(STATE_MAPPING)
    population_melted = population.melt(id_vars="State", var_name="year", value_name="population")
    population_melted['year'] = pd.to_numeric(population_melted['year']).astype("int64")
    return population_melted


# ── Step 3: final merge (mirrors notebook cells 62–72) ───────────────────

def build_final_df(
    vahan: pd.DataFrame,
    state_codes: pd.DataFrame,
    population_melted: pd.DataFrame,
    gsdp_melted: pd.DataFrame,
) -> pd.DataFrame:
    final_df = vahan.merge(state_codes, how='left', left_on='state', right_on='State').drop(columns='State')
    final_df = final_df.merge(
        population_melted, how='left', left_on=['state', 'year'], right_on=['State', 'year']
    ).drop(columns='State')
    final_df = final_df.merge(gsdp_melted, how='left', on=['state', 'financial_year'])

    final_df.columns = (
        final_df.columns.str.replace('/', '_')
        .str.replace(' ', '_')
        .str.lower()
        .str.replace('tin', 'state_id')
        .str.replace('month_wise', 'month')
        .str.replace('value', 'registrations')
    )

    final_df = final_df[[
        'year', 'month_number', 'month', 'financial_year', 'state_id', 'state_code',
        'state', 'population', 'gsdp_lakhs', 'fuel_type', 'vehicle_class',
        'vehicle_category', 'registrations',
    ]]
    return final_df


# ── Main ──────────────────────────────────────────────────────────────

def main():
    print("=== Step 1: Combine + clean raw VAHAN data ===")
    combined = load_and_combine_vahan(BUCKET, RAW_PREFIX)
    vahan = clean_vahan(combined)

    print("\n=== Step 2: Supporting data ===")
    gsdp_melted = load_gsdp(BUCKET, SUPPORTING_PREFIX)

    dfs = load_support_workbook(BUCKET, SUPPORTING_PREFIX)
    population = dfs['population']
    policies = dfs['policies']
    charging_stations = dfs['charging_stations']
    state_codes = dfs['state_codes']

    population_melted = clean_population(population)

    # Policies and charging_stations don't have a clean join grain into the
    # main fact table (as discussed) — clean their state names for
    # consistency and export separately for the policy-overlay / snapshot
    # tables in RDS, rather than merging them into final_df.
    policies['State'] = policies['State'].map(STATE_MAPPING)
    charging_stations['State'] = charging_stations['State'].map(STATE_MAPPING)

    print("\n=== Step 3: Final merge ===")
    final_df = build_final_df(vahan, state_codes, population_melted, gsdp_melted)
    print(f"  final_df shape: {final_df.shape}")

    print("\n=== Step 4: Write cleaned outputs back to S3 ===")
    write_csv_to_s3(final_df, BUCKET, f"{OUTPUT_PREFIX}final_df.csv")
    write_csv_to_s3(policies, BUCKET, f"{OUTPUT_PREFIX}policies.csv")
    write_csv_to_s3(charging_stations, BUCKET, f"{OUTPUT_PREFIX}charging_stations.csv")

    print("\nDone.")


if __name__ == "__main__":
    main()