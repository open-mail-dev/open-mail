CREATE TABLE IF NOT EXISTS schema_migrations (filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE profiles (
  id          bigserial PRIMARY KEY,
  email       text NOT NULL UNIQUE,              -- normalised: trimmed + lowercased
  first_seen  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE consent (
  profile_id  bigint NOT NULL REFERENCES profiles(id),
  store       text   NOT NULL,
  status      text   NOT NULL CHECK (status IN ('subscribed','unsubscribed')),
  source      text   NOT NULL,                   -- popup | footer | checkout | import_klaviyo | unsubscribe_link
  ip          inet,
  ts          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (profile_id, store)
);
CREATE TABLE consent_log (LIKE consent INCLUDING DEFAULTS, id bigserial PRIMARY KEY);

CREATE TABLE suppressions (
  email   text NOT NULL,
  store   text NOT NULL,                          -- '*' = all stores (hard bounce / complaint)
  reason  text NOT NULL CHECK (reason IN ('unsubscribed','hard_bounce','complaint','import')),
  ts      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (email, store)
);

CREATE TABLE coupons (
  profile_id bigint NOT NULL REFERENCES profiles(id),
  store      text   NOT NULL,
  pool       text   NOT NULL,
  code       text   NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (profile_id, store, pool)
);

CREATE TABLE events (
  id          bigserial PRIMARY KEY,
  store       text NOT NULL,
  profile_id  bigint REFERENCES profiles(id),
  type        text NOT NULL,                      -- signup | order_placed | ...
  external_id text,                               -- e.g. Shopify order id, for idempotency
  payload     jsonb NOT NULL DEFAULT '{}',
  ts          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (store, type, external_id)
);
CREATE INDEX events_profile_type_ts ON events (profile_id, type, ts);

CREATE TABLE enrolments (
  id          bigserial PRIMARY KEY,
  flow        text NOT NULL,
  store       text NOT NULL,
  profile_id  bigint NOT NULL REFERENCES profiles(id),
  started_at  timestamptz NOT NULL DEFAULT now(),
  state       text NOT NULL DEFAULT 'active' CHECK (state IN ('active','exited','completed')),
  exit_reason text,
  UNIQUE (flow, store, profile_id)
);

CREATE TABLE flow_steps (
  enrolment_id bigint NOT NULL REFERENCES enrolments(id),
  step         int    NOT NULL,
  due_at       timestamptz NOT NULL,
  status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','skipped','failed')),
  attempts     int  NOT NULL DEFAULT 0,
  last_error   text,
  message_id   bigint,
  PRIMARY KEY (enrolment_id, step)
);
CREATE INDEX flow_steps_due ON flow_steps (due_at) WHERE status = 'pending';

CREATE TABLE messages (
  id          bigserial PRIMARY KEY,
  profile_id  bigint NOT NULL REFERENCES profiles(id),
  store       text NOT NULL,
  kind        text NOT NULL,                      -- flow:welcome:1 | campaign:<id>
  marketing   boolean NOT NULL DEFAULT true,
  ses_id      text,
  sent_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX messages_profile_sent ON messages (profile_id, sent_at);

CREATE TABLE clicks (
  message_id bigint NOT NULL REFERENCES messages(id),
  url        text   NOT NULL,
  ts         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE signup_rate (ip inet NOT NULL, ts timestamptz NOT NULL DEFAULT now());
CREATE INDEX signup_rate_ip_ts ON signup_rate (ip, ts);
