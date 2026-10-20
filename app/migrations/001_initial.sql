-- 001: initial schema. All timestamps are UTC ISO-8601 strings ending in 'Z'
-- unless the column name says otherwise (TomTom departure/arrival times keep
-- the offset exactly as returned).

CREATE TABLE journeys (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  origin_label TEXT NOT NULL,
  origin_lat REAL NOT NULL,
  origin_lon REAL NOT NULL,
  dest_label TEXT NOT NULL,
  dest_lat REAL NOT NULL,
  dest_lon REAL NOT NULL,
  created_utc TEXT NOT NULL,
  updated_utc TEXT NOT NULL
);

CREATE TABLE directions (
  id INTEGER PRIMARY KEY,
  journey_id INTEGER NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
  code TEXT NOT NULL,              -- 'outbound' (origin -> destination) or 'return'
  enabled INTEGER NOT NULL DEFAULT 1,
  UNIQUE (journey_id, code)
);

CREATE TABLE corridors (
  id INTEGER PRIMARY KEY,
  direction_id INTEGER NOT NULL REFERENCES directions(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  color TEXT NOT NULL,
  method TEXT NOT NULL CHECK (method IN ('waypoints', 'reconstruct')),
  waypoints_json TEXT NOT NULL DEFAULT '[]',         -- [[lat, lon], ...] ordered via points
  supporting_points_json TEXT,                        -- [[lat, lon], ...] for 'reconstruct'
  reference_variant_id INTEGER REFERENCES route_variants(id),  -- geometry the corridor was confirmed with
  preview_variant_id INTEGER REFERENCES route_variants(id),
  preview_utc TEXT,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'confirmed', 'retired')),
  source TEXT NOT NULL DEFAULT 'user',                -- user | suggested | from_route
  note TEXT NOT NULL DEFAULT '',
  deviation_threshold_m INTEGER NOT NULL DEFAULT 400,
  sort INTEGER NOT NULL DEFAULT 0,
  confirmed_utc TEXT,
  created_utc TEXT NOT NULL,
  updated_utc TEXT NOT NULL
);

CREATE TABLE route_variants (
  id INTEGER PRIMARY KEY,
  direction_id INTEGER NOT NULL REFERENCES directions(id) ON DELETE CASCADE,
  fingerprint TEXT NOT NULL,
  geometry_z BLOB NOT NULL,                           -- zlib(JSON [[lat, lon], ...]) at original precision
  point_count INTEGER NOT NULL,
  length_m INTEGER,
  roads_json TEXT NOT NULL DEFAULT '[]',              -- road numbers / street names in order
  first_seen_utc TEXT NOT NULL,
  last_seen_utc TEXT NOT NULL,
  seen_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE (direction_id, fingerprint)
);

CREATE TABLE collection_cycles (
  id INTEGER PRIMARY KEY,
  trigger TEXT NOT NULL CHECK (trigger IN ('schedule', 'manual')),
  slot_utc TEXT,                                      -- intended observation time; NULL never for schedule
  status TEXT NOT NULL CHECK (status IN ('running', 'complete', 'partial', 'failed', 'skipped', 'missed', 'interrupted')),
  reason TEXT,                                        -- why skipped / missed / failed
  planned_requests INTEGER NOT NULL DEFAULT 0,
  attempted_requests INTEGER NOT NULL DEFAULT 0,
  started_utc TEXT,
  finished_utc TEXT,
  worker TEXT
);
CREATE UNIQUE INDEX ux_cycles_schedule_slot ON collection_cycles(slot_utc) WHERE trigger = 'schedule';
CREATE INDEX ix_cycles_slot ON collection_cycles(slot_utc);

CREATE TABLE request_ledger (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                                 -- routing
  purpose TEXT NOT NULL,                              -- schedule | manual | retry | preview | verification
  period_key TEXT NOT NULL,
  counted INTEGER NOT NULL,                           -- 1 if it counts against the allowance
  created_utc TEXT NOT NULL,
  sent_utc TEXT,
  completed_utc TEXT,
  outcome TEXT NOT NULL,                              -- pending | ok | http_error | network_error | timeout | invalid_response | blocked_budget | blocked_backoff | interrupted
  http_status INTEGER,
  tracking_id TEXT,
  endpoint TEXT,                                      -- sanitized, no credentials
  cycle_id INTEGER REFERENCES collection_cycles(id),
  note TEXT
);
CREATE INDEX ix_ledger_period ON request_ledger(kind, period_key, counted);

CREATE TABLE observations (
  id INTEGER PRIMARY KEY,
  cycle_id INTEGER NOT NULL REFERENCES collection_cycles(id) ON DELETE CASCADE,
  direction_id INTEGER NOT NULL REFERENCES directions(id) ON DELETE CASCADE,
  target TEXT NOT NULL CHECK (target IN ('unrestricted', 'corridor')),
  corridor_id INTEGER REFERENCES corridors(id) ON DELETE SET NULL,
  role TEXT NOT NULL CHECK (role IN ('recommended', 'alternative', 'corridor')),
  returned_index INTEGER,                             -- position in TomTom's response; informational only
  ledger_id INTEGER REFERENCES request_ledger(id),
  variant_id INTEGER REFERENCES route_variants(id),
  status TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
  failure_reason TEXT,
  http_status INTEGER,
  intended_utc TEXT NOT NULL,
  request_utc TEXT,
  response_utc TEXT,
  distance_m INTEGER,
  travel_time_s INTEGER,
  traffic_delay_s INTEGER,
  free_flow_s INTEGER,
  historic_s INTEGER,
  live_s INTEGER,
  traffic_length_m INTEGER,
  departure_time TEXT,
  arrival_time TEXT,
  resembles_corridor_id INTEGER REFERENCES corridors(id) ON DELETE SET NULL,
  resembles_score REAL,
  deviation_flag INTEGER NOT NULL DEFAULT 0,
  deviation_share REAL,                               -- share of points further than threshold from the corridor reference
  deviation_max_m INTEGER,
  traffic_sections_json TEXT,
  request_params_json TEXT,
  tracking_id TEXT,
  raw_response_z BLOB
);
CREATE INDEX ix_obs_dir_time ON observations(direction_id, intended_utc);
CREATE INDEX ix_obs_cycle ON observations(cycle_id);
CREATE INDEX ix_obs_corridor ON observations(corridor_id, intended_utc);

CREATE TABLE tile_usage (
  day_utc TEXT NOT NULL,
  kind TEXT NOT NULL,                                 -- map | traffic
  requests INTEGER NOT NULL DEFAULT 0,
  errors INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day_utc, kind)
);

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_utc TEXT NOT NULL
);

CREATE TABLE scheduler_lease (
  name TEXT PRIMARY KEY,
  holder TEXT NOT NULL,
  expires_utc TEXT NOT NULL
);

CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  ts_utc TEXT NOT NULL,
  level TEXT NOT NULL,
  kind TEXT NOT NULL,
  message TEXT NOT NULL
);
CREATE INDEX ix_events_ts ON events(ts_utc);
