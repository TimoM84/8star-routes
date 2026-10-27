-- Retired corridors can be hidden from Setup and the analysis views; their observations stay.
ALTER TABLE corridors ADD COLUMN hidden_utc TEXT;
