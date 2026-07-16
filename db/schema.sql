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

-- Scheduling profile fields extend the existing user/employee record.  The
-- application accepts weekly_availability as JSON so it can represent multiple
-- availability windows in a day without creating another employee schema.
ALTER TABLE users ADD COLUMN IF NOT EXISTS weekly_availability jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE users ADD COLUMN IF NOT EXISTS max_hours_per_week numeric(5,2) NOT NULL DEFAULT 40;

CREATE TABLE IF NOT EXISTS shifts (
    id            SERIAL PRIMARY KEY,
    resto_id      integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    employee_id   integer REFERENCES users(id) ON DELETE SET NULL,
    role_required text NOT NULL CHECK (role_required IN ('foh', 'boh')),
    shift_date    date NOT NULL,
    start_time    time NOT NULL,
    end_time      time NOT NULL,
    status        text NOT NULL DEFAULT 'Open'
                  CHECK (status IN ('Scheduled', 'Open', 'Pending_Swap')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    CHECK (start_time <> end_time)
);

CREATE INDEX IF NOT EXISTS shifts_resto_week_idx ON shifts (resto_id, shift_date);
CREATE INDEX IF NOT EXISTS shifts_employee_week_idx ON shifts (employee_id, shift_date);

CREATE TABLE IF NOT EXISTS swap_requests (
    id                     SERIAL PRIMARY KEY,
    resto_id               integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    original_shift_id      integer NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
    requesting_employee_id integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_employee_id     integer REFERENCES users(id) ON DELETE CASCADE,
    status                 text NOT NULL DEFAULT 'Pending_Match'
                           CHECK (status IN ('Pending_Match', 'Pending_Approval', 'Completed', 'Rejected')),
    created_at             timestamptz NOT NULL DEFAULT now(),
    UNIQUE (original_shift_id, target_employee_id)
);

CREATE INDEX IF NOT EXISTS swap_requests_target_status_idx
ON swap_requests (target_employee_id, status);
