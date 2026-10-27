-- 003: stable route identities and corridor retirement time.
-- A route family groups variants (exact geometries) that are the same road route with only
-- small differences, so identity no longer depends on exact coordinates or TomTom's ordering.
-- Families are assigned by app/families.py at startup and when new variants are stored.
CREATE TABLE route_families (
  id INTEGER PRIMARY KEY,
  direction_id INTEGER NOT NULL REFERENCES directions(id) ON DELETE CASCADE,
  representative_variant_id INTEGER NOT NULL REFERENCES route_variants(id),
  length_m INTEGER,
  roads_json TEXT NOT NULL DEFAULT '[]',
  created_utc TEXT NOT NULL
);
CREATE INDEX ix_families_direction ON route_families(direction_id);

ALTER TABLE route_variants ADD COLUMN family_id INTEGER REFERENCES route_families(id);
CREATE INDEX ix_variants_family ON route_variants(family_id);
CREATE INDEX ix_obs_variant ON observations(variant_id);

-- When a corridor stopped being collected. Existing retired corridors get their last observation time.
ALTER TABLE corridors ADD COLUMN retired_utc TEXT;
UPDATE corridors SET retired_utc = COALESCE(
  (SELECT MAX(o.intended_utc) FROM observations o WHERE o.corridor_id = corridors.id AND o.role = 'corridor'),
  updated_utc) WHERE status = 'retired';
