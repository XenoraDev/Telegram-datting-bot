-- 003: profiles.country (ISO-3166 alpha-2, 'ZZ' = other). Used only to show a flag next to the
-- city/region. Nullable: existing profiles keep working and simply show a neutral globe.

ALTER TABLE profiles
    ADD COLUMN country TEXT CHECK (country IS NULL OR country ~ '^[A-Z]{2}$');
