CREATE TABLE IF NOT EXISTS restaurants (
    id         SERIAL PRIMARY KEY,
    name       text NOT NULL,
    created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS roles (
    id   SERIAL PRIMARY KEY,
    name text NOT NULL UNIQUE
);

INSERT INTO roles (name) VALUES ('manager'), ('foh'), ('boh')
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    restaurant_id integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    role_id       integer NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
    name          text NOT NULL,
    email         text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    created_at    timestamptz DEFAULT now()
);

-- ALTER guards let this run against a pre-existing dev DB that predates password_hash.
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash text;
