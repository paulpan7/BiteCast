-- FleetCast MySQL schema.
--
-- Source-of-truth tables are trip, trip_species, analysis_row, species, boat,
-- landing. Everything else is derived and can be dropped and rebuilt from those
-- (see rebuild_derived.py). Only the source-of-truth tables need backup.
--
-- Column sizes are set from the measured corpus (17,507 trips / 56,748 species
-- rows / 6,337 analysis rows): anglers max 94, encounters max 533, epa max 51.0,
-- trip_no max 2, boat name max 15 chars, species name max 22 chars.

SET NAMES utf8mb4;

-- ---------------------------------------------------------------- vocabularies

CREATE TABLE landing (
  landing_id   TINYINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  name         VARCHAR(64)  NOT NULL,
  landing_path VARCHAR(128) NULL,
  city         VARCHAR(64)  NULL,
  UNIQUE KEY uq_landing_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE boat (
  boat_id    SMALLINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  name       VARCHAR(64)  NOT NULL,
  boat_path  VARCHAR(128) NULL,
  landing_id TINYINT UNSIGNED NULL,          -- current/primary landing; trips carry their own
  UNIQUE KEY uq_boat_name (name),
  CONSTRAINT fk_boat_landing FOREIGN KEY (landing_id) REFERENCES landing(landing_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- analysis_pos mirrors the positional fish[10] array's index, ordered by
-- ANALYSIS_SPECIES in extend_history.py. NULL for the other 45 species.
CREATE TABLE species (
  species_id   SMALLINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  name         VARCHAR(64) NOT NULL,
  analysis_pos TINYINT UNSIGNED NULL,
  UNIQUE KEY uq_species_name (name),
  UNIQUE KEY uq_species_pos (analysis_pos)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ------------------------------------------------------------ source of truth

-- fingerprint is the sha1 of extend_history.py's fingerprint() tuple:
-- (date, boat, landing, period, anglers, sorted((species, kept, released))).
-- It is the idempotency key -- replaying a scrape is a no-op. Note this
-- reproduces existing semantics exactly, including that a corrected species
-- count yields a NEW fingerprint rather than updating the existing row.
--
-- (trip_date, boat_id, period) is deliberately NOT unique: 90 groups in the
-- corpus legitimately hold two trips (different angler counts). trip_no
-- disambiguates and is DERIVED -- assigned across the whole sorted set after
-- dedup, never computed row-locally.
CREATE TABLE trip (
  trip_id     INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  fingerprint CHAR(40) NOT NULL,
  trip_date   DATE NOT NULL,
  period      ENUM('AM','PM') NOT NULL,
  boat_id     SMALLINT UNSIGNED NOT NULL,
  landing_id  TINYINT UNSIGNED NOT NULL,
  anglers     SMALLINT UNSIGNED NOT NULL,
  kept        SMALLINT UNSIGNED NOT NULL,
  released    SMALLINT UNSIGNED NOT NULL,
  encounters  SMALLINT UNSIGNED NOT NULL,
  epa         DECIMAL(7,3) NULL,             -- NULL when anglers = 0 (57 rows)
  trip_no     TINYINT UNSIGNED NOT NULL DEFAULT 1,
  source_url  VARCHAR(255) NULL,
  UNIQUE KEY uq_trip_fingerprint (fingerprint),
  KEY ix_trip_date_period (trip_date, period),
  KEY ix_trip_boat_date (boat_id, trip_date),
  KEY ix_trip_landing_date (landing_id, trip_date),
  CONSTRAINT fk_trip_boat    FOREIGN KEY (boat_id)    REFERENCES boat(boat_id),
  CONSTRAINT fk_trip_landing FOREIGN KEY (landing_id) REFERENCES landing(landing_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- `position` preserves the order the source report listed species in. Nothing
-- in the page depends on it (every consumer aggregates by name), but keeping it
-- means the served payload reproduces the original literal exactly, which keeps
-- migration diffs honest.
CREATE TABLE trip_species (
  trip_id    INT UNSIGNED NOT NULL,
  species_id SMALLINT UNSIGNED NOT NULL,
  position   TINYINT UNSIGNED NOT NULL DEFAULT 0,
  kept       SMALLINT UNSIGNED NOT NULL,
  released   SMALLINT UNSIGNED NOT NULL,
  PRIMARY KEY (trip_id, species_id),
  KEY ix_trip_species_species (species_id),
  CONSTRAINT fk_ts_trip    FOREIGN KEY (trip_id)    REFERENCES trip(trip_id) ON DELETE CASCADE,
  CONSTRAINT fk_ts_species FOREIGN KEY (species_id) REFERENCES species(species_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- One weather observation per (date, period). All measurements nullable --
-- current coverage of 6,337 rows: water_temp 6,317 / air_temp 5,974 /
-- pressure 6,204 / swell 6,321 / tide 6,337.
--
-- Writers MUST preserve extend_history.py's only-fill-if-absent semantics
-- (build_analysis_rows never overwrites a value that is already present):
--   INSERT ... ON DUPLICATE KEY UPDATE swell_ft = COALESCE(swell_ft, VALUES(swell_ft))
-- one COALESCE clause per measurement column. That single idiom also preserves
-- the ordered wave fallback chain (46232 -> 46225 -> CDIP 191 -> CDIP 100)
-- without a Python read-modify-write.
CREATE TABLE analysis_row (
  obs_date       DATE NOT NULL,
  period         ENUM('AM','PM') NOT NULL,
  air_temp_f     DECIMAL(6,3) NULL,
  water_temp_f   DECIMAL(6,3) NULL,
  pressure_hpa   DECIMAL(7,3) NULL,
  swell_ft       DECIMAL(6,3) NULL,
  swell_source   ENUM('46232','46225','cdip191','cdip100') NULL,
  tide_height_ft DECIMAL(6,3) NULL,
  tide_swing_ft  DECIMAL(6,3) NULL,
  tide_delta_ft  DECIMAL(6,3) NULL,
  PRIMARY KEY (obs_date, period)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ------------------------------------------------------- derived / rebuildable

-- Replaces the positional fish[10] array. Rebuilt from trip + trip_species.
CREATE TABLE catch_rollup (
  obs_date   DATE NOT NULL,
  period     ENUM('AM','PM') NOT NULL,
  species_id SMALLINT UNSIGNED NOT NULL,
  kept       INT UNSIGNED NOT NULL,
  released   INT UNSIGNED NOT NULL,
  encounters INT UNSIGNED NOT NULL,
  trips      SMALLINT UNSIGNED NOT NULL,
  anglers    INT UNSIGNED NOT NULL,
  PRIMARY KEY (obs_date, period, species_id),
  KEY ix_rollup_species_date (species_id, obs_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE boat_profile (
  boat_id    SMALLINT UNSIGNED NOT NULL PRIMARY KEY,
  trip_count SMALLINT UNSIGNED NOT NULL,
  landing_id TINYINT UNSIGNED NULL,
  recent12   DECIMAL(7,3) NULL,
  CONSTRAINT fk_bp_boat FOREIGN KEY (boat_id) REFERENCES boat(boat_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- recent12 is the mean of the last 12 epa values in date order (values[-12:]).
-- distribution is the ordered epa list, kept as JSON since it is display-only.
CREATE TABLE boat_profile_period (
  boat_id      SMALLINT UNSIGNED NOT NULL,
  period       ENUM('AM','PM') NOT NULL,
  n            SMALLINT UNSIGNED NOT NULL,
  recent12     DECIMAL(7,3) NULL,
  distribution JSON NULL,
  PRIMARY KEY (boat_id, period),
  CONSTRAINT fk_bpp_boat FOREIGN KEY (boat_id) REFERENCES boat(boat_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ------------------------------------------------------------------- modelling

-- One row per fit. status='shadow' means the fit ran but must not be served
-- (used when a freeze marker is active); 'live' is the served pointer.
CREATE TABLE fit_run (
  fit_run_id     INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  fitted_at      DATETIME NOT NULL,
  lambda_value   DECIMAL(6,2) NOT NULL,
  split_date     DATE NOT NULL,
  rows_trained   INT UNSIGNED NOT NULL,
  trained_through DATE NULL,
  status         ENUM('live','shadow','superseded') NOT NULL DEFAULT 'shadow',
  mae            DECIMAL(8,4) NULL,
  rmse           DECIMAL(8,4) NULL,
  within2        DECIMAL(6,4) NULL,
  coverage       DECIMAL(6,4) NULL,
  KEY ix_fit_status (status, fitted_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Nine coefficients per model, matching weather_features():
-- [1, w, w*w, s, s*s, pm, recent, pm*w, t].
--
-- boat_id = 0 is the sentinel for the fleet-wide model. It cannot be NULL --
-- MySQL requires every part of a PRIMARY KEY to be NOT NULL, and a UNIQUE index
-- would not do, since UNIQUE treats NULLs as distinct and would happily admit
-- duplicate fleet rows. For the same reason there is no FK on boat_id here:
-- 0 has no matching row in boat.
CREATE TABLE model_coef (
  fit_run_id  INT UNSIGNED NOT NULL,
  boat_id     SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  coef_index  TINYINT UNSIGNED NOT NULL,
  coef_value  DOUBLE NOT NULL,
  PRIMARY KEY (fit_run_id, boat_id, coef_index),
  CONSTRAINT fk_coef_run FOREIGN KEY (fit_run_id) REFERENCES fit_run(fit_run_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Residual quantiles drive the forecast intervals (q10/q25/q75/q90).
-- boat_id = 0 is the fleet model, as in model_coef.
CREATE TABLE model_residual (
  fit_run_id   INT UNSIGNED NOT NULL,
  boat_id      SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  training_n   INT UNSIGNED NOT NULL,
  validation_n INT UNSIGNED NOT NULL,
  mode         VARCHAR(16) NOT NULL,
  q10 DOUBLE NULL, q25 DOUBLE NULL, q75 DOUBLE NULL, q90 DOUBLE NULL,
  PRIMARY KEY (fit_run_id, boat_id),
  CONSTRAINT fk_resid_run FOREIGN KEY (fit_run_id) REFERENCES fit_run(fit_run_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE forecast_period (
  obs_date     DATE NOT NULL,
  period       ENUM('AM','PM') NOT NULL,
  epa          DECIMAL(7,3) NULL,
  score        DECIMAL(5,2) NULL,
  typical_low  DECIMAL(7,3) NULL,
  typical_high DECIMAL(7,3) NULL,
  planning_low DECIMAL(7,3) NULL,
  planning_high DECIMAL(7,3) NULL,
  range_n      SMALLINT UNSIGNED NULL,
  wind_kt      SMALLINT UNSIGNED NULL,
  wind_dir     SMALLINT UNSIGNED NULL,
  seas_ft      DECIMAL(5,2) NULL,
  period_sec   DECIMAL(5,2) NULL,
  sst_f        DECIMAL(6,2) NULL,
  source       VARCHAR(32) NULL,
  generated_at DATETIME NULL,
  PRIMARY KEY (obs_date, period)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- The frozen prospective snapshot, loaded so parity checking is a SQL join
-- rather than a file diff.
CREATE TABLE frozen_prediction (
  snapshot     VARCHAR(64) NOT NULL,
  obs_date     DATE NOT NULL,
  boat_id      SMALLINT UNSIGNED NOT NULL,
  period       ENUM('AM','PM') NOT NULL,
  predicted_epa DECIMAL(10,4) NOT NULL,
  typical_low  DECIMAL(10,4) NULL,
  typical_high DECIMAL(10,4) NULL,
  planning_low DECIMAL(10,4) NULL,
  planning_high DECIMAL(10,4) NULL,
  water_temp_f DECIMAL(8,3) NULL,
  swell_ft     DECIMAL(8,3) NULL,
  tide_delta_ft DECIMAL(8,3) NULL,
  weather_source VARCHAR(48) NULL,
  PRIMARY KEY (snapshot, obs_date, boat_id, period)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------- boat tracks

-- Written by the GitHub Actions ShipFinder scrape via POST /api/ingest/boat-tracks.
-- Playwright cannot run on PythonAnywhere (README.md), so this is the one job
-- that must stay on Actions. Dedup key matches merge_points' rounding.
CREATE TABLE boat_track (
  mmsi      VARCHAR(16) NOT NULL,
  track_day DATE NOT NULL,
  ts        DATETIME NOT NULL,
  lat       DECIMAL(9,5) NOT NULL,
  lon       DECIMAL(9,5) NOT NULL,
  speed_kt  DECIMAL(5,2) NULL,
  PRIMARY KEY (mmsi, ts, lat, lon),
  KEY ix_track_day (track_day, mmsi)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Scalar site metadata that has nowhere better to live (the freshness
-- timestamps the footer renders, for instance).
CREATE TABLE site_meta (
  meta_key   VARCHAR(64) NOT NULL PRIMARY KEY,
  meta_value VARCHAR(255) NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Append-only audit trail. Partly recovers the provenance that git commits
-- provided before data moved out of the repo.
CREATE TABLE ingest_log (
  ingest_id   INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  received_at DATETIME NOT NULL,
  source      VARCHAR(32) NOT NULL,
  payload_day DATE NULL,
  inserted    INT UNSIGNED NOT NULL DEFAULT 0,
  updated     INT UNSIGNED NOT NULL DEFAULT 0,
  skipped     INT UNSIGNED NOT NULL DEFAULT 0,
  note        VARCHAR(255) NULL,
  KEY ix_ingest_received (received_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
