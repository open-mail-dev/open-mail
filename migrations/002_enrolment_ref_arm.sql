-- Abandoned checkout: one enrolment per CHECKOUT (ref = Shopify checkout id), not one per profile per flow ever;
-- welcome keeps ref '' so it stays once-per-profile. arm = A/B group of the holdout test (NULL outside a test).
ALTER TABLE enrolments ADD COLUMN ref text NOT NULL DEFAULT '';
ALTER TABLE enrolments ADD COLUMN arm text;
ALTER TABLE enrolments DROP CONSTRAINT enrolments_flow_store_profile_id_key;
ALTER TABLE enrolments ADD CONSTRAINT enrolments_flow_store_profile_ref_key UNIQUE (flow, store, profile_id, ref);
