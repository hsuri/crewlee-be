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

-- Departments sit between the restaurant and its roles/employees (Location -> Department ->
-- Role -> Employee). Each department binds to one of the existing foh/boh role categories
-- rather than introducing a separate custom-role taxonomy, so validate_assignment's role-match
-- logic keeps working unchanged; a restaurant can still have multiple departments per category
-- (e.g. "Kitchen" + "Prep", both boh) if it wants a finer split.
CREATE TABLE IF NOT EXISTS departments (
    id            SERIAL PRIMARY KEY,
    resto_id      integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    name          text NOT NULL,
    role_category text NOT NULL CHECK (role_category IN ('foh', 'boh')),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS departments_resto_idx ON departments (resto_id);

-- Every restaurant gets a default FOH/BOH department the first time this runs against it,
-- so there's always somewhere for existing shifts/users to backfill onto below.
INSERT INTO departments (resto_id, name, role_category)
SELECT r.id, 'Front of House', 'foh' FROM restaurants r
WHERE NOT EXISTS (SELECT 1 FROM departments d WHERE d.resto_id = r.id AND d.role_category = 'foh');

INSERT INTO departments (resto_id, name, role_category)
SELECT r.id, 'Back of House', 'boh' FROM restaurants r
WHERE NOT EXISTS (SELECT 1 FROM departments d WHERE d.resto_id = r.id AND d.role_category = 'boh');

ALTER TABLE users ADD COLUMN IF NOT EXISTS department_id integer REFERENCES departments(id) ON DELETE SET NULL;

-- Backfill: any foh/boh user without a department lands in their restaurant's default
-- department for their role. Re-runs harmlessly once every user has one (WHERE ... IS NULL).
UPDATE users u
SET department_id = d.id
FROM roles r, departments d
WHERE u.role_id = r.id AND r.name IN ('foh', 'boh')
  AND d.resto_id = u.restaurant_id AND d.role_category = r.name
  AND u.department_id IS NULL;

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

ALTER TABLE shifts ADD COLUMN IF NOT EXISTS department_id integer REFERENCES departments(id) ON DELETE SET NULL;

-- Draft/publish is orthogonal to the Open/Scheduled/Pending_Swap status lifecycle: a manager
-- can build or edit a shift in draft (invisible to employees) and publish it later. Defaults to
-- false so pre-existing shifts (created before this column existed) are already published.
ALTER TABLE shifts ADD COLUMN IF NOT EXISTS is_draft boolean NOT NULL DEFAULT false;

-- Backfill: any shift without a department lands in its restaurant's default department
-- for its role_required category.
UPDATE shifts s
SET department_id = d.id
FROM departments d
WHERE d.resto_id = s.resto_id AND d.role_category = s.role_required AND s.department_id IS NULL;

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

-- A template snapshots a week's shifts as a single jsonb blob (dayOffset/departmentId/
-- employeeId/startTime/endTime per entry) rather than a join table — templates are read-mostly
-- and small, so there's no benefit to normalizing them into their own shift-like rows.
CREATE TABLE IF NOT EXISTS schedule_templates (
    id         SERIAL PRIMARY KEY,
    resto_id   integer NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    name       text NOT NULL,
    shifts     jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS schedule_templates_resto_idx ON schedule_templates (resto_id);
