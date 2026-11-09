-- Every fetch of a mail's open pixel. Raw on purpose: Apple Mail Privacy Protection and mail scanners fetch it too,
-- so report.py decides what counts as a likely human open; no IP is stored.
CREATE TABLE opens (
  message_id bigint NOT NULL REFERENCES messages(id),
  ts         timestamptz NOT NULL DEFAULT now(),
  user_agent text
);
CREATE INDEX opens_message ON opens (message_id);
