-- Run once, on first start of the courier_ts_data volume.
--
-- drone_state is created as a hypertable by a migration (P1-04), not here.
-- This file only guarantees the extensions that migration depends on.

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- DBNAME is a psql built-in, so this works whatever POSTGRES_DB is set to.
ALTER DATABASE :"DBNAME" SET timezone TO 'UTC';
