-- Roles for the offers database. Locally a superuser runs this; on Neon
-- `offers-db provision` creates the same roles through the migrator.
--
-- offers_migrator  owns the schema and runs migrations only.
-- offers_api       the API at runtime: no superuser, no BYPASSRLS, owns nothing.
-- offers_publisher sweeps and marks the outbox across workspaces; nothing else.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'offers_migrator') THEN
        CREATE ROLE offers_migrator LOGIN NOSUPERUSER NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'offers_api') THEN
        CREATE ROLE offers_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'offers_publisher') THEN
        CREATE ROLE offers_publisher LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
    END IF;
END
$$;
