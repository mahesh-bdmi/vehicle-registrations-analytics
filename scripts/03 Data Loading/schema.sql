-- ============================================================
-- India VAHAN Vehicle Registration — Analytics Schema
-- Star-schema-style: one fact table per measurement grain,
-- shared dimensions, policies as a standalone reference table.
-- Written for PostgreSQL; swap SERIAL/BIGSERIAL for
-- INTEGER PRIMARY KEY AUTOINCREMENT if using SQLite.
-- ============================================================

-- ---------- DIMENSIONS ----------

CREATE TABLE dim_state (
    state_id     INTEGER PRIMARY KEY,      -- from VAHAN state_id / TIN
    state_code   VARCHAR(5) NOT NULL,      -- e.g. 'MP', 'WB'
    state_name   VARCHAR(100) NOT NULL UNIQUE
);

CREATE TABLE dim_vehicle_class (
    vehicle_class     VARCHAR(100) PRIMARY KEY,  -- e.g. 'MOTOR CAR'
    vehicle_category  VARCHAR(60) NOT NULL       -- e.g. 'Car'
);

CREATE TABLE dim_fuel_type (
    fuel_type    VARCHAR(30) PRIMARY KEY,   -- e.g. 'electric', 'diesel'
    is_electric  BIT NOT NULL DEFAULT 0     -- SQL Server uses BIT (0 for False, 1 for True)
);

-- ---------- FACT TABLES (each at its own natural grain) ----------

-- Grain: one row per state x year x month x fuel_type x vehicle_class
CREATE TABLE fact_registrations (
    registration_id  BIGINT IDENTITY(1,1) PRIMARY KEY, -- Replaced BIGSERIAL with BIGINT IDENTITY
    state_id         INTEGER NOT NULL REFERENCES dim_state(state_id),
    vehicle_class    VARCHAR(100) NOT NULL REFERENCES dim_vehicle_class(vehicle_class),
    fuel_type        VARCHAR(30) NOT NULL REFERENCES dim_fuel_type(fuel_type),
    year             SMALLINT NOT NULL,
    month_number     SMALLINT NOT NULL CONSTRAINT CK_registrations_month CHECK (month_number BETWEEN 1 AND 12),
    financial_year   VARCHAR(7) NOT NULL,        -- e.g. '2023-24'
    registrations    INTEGER NOT NULL CONSTRAINT CK_registrations_count CHECK (registrations >= 0),
    CONSTRAINT UQ_registrations_grain UNIQUE (state_id, vehicle_class, fuel_type, year, month_number)
);

-- Grain: one row per state x year
CREATE TABLE fact_population (
    state_id    INTEGER NOT NULL REFERENCES dim_state(state_id),
    year        SMALLINT NOT NULL,
    population  BIGINT NOT NULL CONSTRAINT CK_population_positive CHECK (population >= 0),
    CONSTRAINT PK_fact_population PRIMARY KEY (state_id, year)
);

-- Grain: one row per state x financial year
CREATE TABLE fact_gsdp (
    state_id        INTEGER NOT NULL REFERENCES dim_state(state_id),
    financial_year  VARCHAR(7) NOT NULL,
    gsdp_lakhs      BIGINT CONSTRAINT CK_gsdp_positive CHECK (gsdp_lakhs >= 0),   -- nullable: recent FYs often unreleased
    CONSTRAINT PK_fact_gsdp PRIMARY KEY (state_id, financial_year)
);

-- Grain: one row per state x snapshot date.
-- Source data is a CUMULATIVE count as of 16-Dec-2025, not a per-year flow.
-- Modeled as (state_id, as_of_date) rather than a flat column so that if
-- you ever pull a second snapshot later, this becomes a real time series
-- without a schema change. Today, every row shares the same as_of_date.
-- total_chargers = fast_chargers + slow_chargers (source provides all three
-- directly, no need to derive).
CREATE TABLE fact_charging_stations (
    state_id         INTEGER NOT NULL REFERENCES dim_state(state_id),
    as_of_date       DATE NOT NULL,
    total_chargers   INTEGER NOT NULL CHECK (total_chargers >= 0),
    fast_chargers    INTEGER NOT NULL CHECK (fast_chargers >= 0),
    slow_chargers    INTEGER NOT NULL CHECK (slow_chargers >= 0),
    PRIMARY KEY (state_id, as_of_date)
);

-- Standalone reference table — NOT joined into fact grain.
-- Queried separately and plotted as annotations/vertical lines against the
-- registrations time series.
-- Source dates are inconsistently formatted (e.g. '2017-12-01 00:00:00' vs
-- free text like 'September 2019 (Revised Feb 2023)'). effective_date holds
-- the parsed value where possible (nullable — don't force a bad parse);
-- effective_date_raw always holds the original source text so nothing is
-- lost when parsing fails or is ambiguous (e.g. a revision date embedded
-- in the string).
CREATE TABLE policy_events (
    policy_id           INT IDENTITY(1,1) PRIMARY KEY, -- Replaced SERIAL with INT IDENTITY
    state_id            INTEGER REFERENCES dim_state(state_id),  -- NULL = national policy
    policy_name         VARCHAR(200) NOT NULL,
    effective_date      DATE,          -- NULL if source text didn't parse cleanly
    effective_date_raw  VARCHAR(100) NOT NULL,
    description         VARCHAR(MAX)   -- Replaced TEXT with VARCHAR(MAX)
);

-- ---------- USEFUL INDEXES ----------

CREATE INDEX idx_fact_registrations_state_year ON fact_registrations (state_id, year);
CREATE INDEX idx_fact_registrations_fuel ON fact_registrations (fuel_type);
CREATE INDEX idx_policy_events_state ON policy_events (state_id);

-- ============================================================
-- Example analysis queries (once loaded)
-- ============================================================

-- 1. National EV share by financial year
-- SELECT financial_year,
--        SUM(CASE WHEN f.fuel_type = 'electric' THEN registrations ELSE 0 END)::FLOAT
--          / SUM(registrations) AS ev_share
-- FROM fact_registrations r
-- JOIN dim_fuel_type f USING (fuel_type)
-- GROUP BY financial_year
-- ORDER BY financial_year;

-- 2. EV registrations per 1,000 people, by state, latest year
-- SELECT s.state_name,
--        SUM(r.registrations) FILTER (WHERE r.fuel_type = 'electric') * 1000.0
--          / p.population AS ev_per_1000
-- FROM fact_registrations r
-- JOIN dim_state s USING (state_id)
-- JOIN fact_population p ON p.state_id = r.state_id AND p.year = r.year
-- WHERE r.year = 2025
-- GROUP BY s.state_name, p.population
-- ORDER BY ev_per_1000 DESC;

-- 3. Charging station density (as of latest snapshot) vs cumulative EV
--    share to date, for a scatter plot. Note: stations is a single
--    point-in-time count, so it's compared against EV share across ALL
--    years in the fact table, not year-matched.
-- SELECT s.state_name,
--        c.cumulative_stations,
--        SUM(r.registrations) FILTER (WHERE r.fuel_type = 'electric')::FLOAT
--          / SUM(r.registrations) AS ev_share_to_date
-- FROM fact_registrations r
-- JOIN dim_state s USING (state_id)
-- JOIN fact_charging_stations c ON c.state_id = r.state_id
-- GROUP BY s.state_name, c.cumulative_stations;

-- 4. Policy overlay: pull events for a given state to annotate a chart
-- SELECT policy_name, effective_date, description
-- FROM policy_events
-- WHERE state_id = (SELECT state_id FROM dim_state WHERE state_name = 'Maharashtra')
--    OR state_id IS NULL   -- include national policies
-- ORDER BY effective_date;