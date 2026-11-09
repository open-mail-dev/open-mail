-- Back-in-stock: one row per "mail me when it's back" request (stock.py). waiting → enrolled (a back_in_stock
-- enrolment with ref stock:<id>:<attempt>) → sent; enrolled → waiting again when the variant sold out before the mail.
CREATE TABLE stock_requests (
  id          bigserial PRIMARY KEY,
  store       text NOT NULL,
  profile_id  bigint NOT NULL REFERENCES profiles(id),
  product_id  text NOT NULL,
  variant_id  text NOT NULL,
  created_at  timestamptz NOT NULL,
  expires_at  timestamptz NOT NULL,
  state       text NOT NULL DEFAULT 'waiting' CHECK (state IN ('waiting','enrolled','sent','expired','cancelled')),
  attempt     int NOT NULL DEFAULT 0,
  source      text NOT NULL CHECK (source IN ('form','import')),  -- import: requests carried over from a previous tool
  message_id  bigint REFERENCES messages(id)
);
CREATE UNIQUE INDEX stock_requests_open ON stock_requests (store, profile_id, variant_id) WHERE state IN ('waiting','enrolled');
CREATE INDEX stock_requests_waiting ON stock_requests (store, variant_id) WHERE state = 'waiting';
