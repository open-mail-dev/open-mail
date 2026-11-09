from datetime import timedelta

from flows import Flow, Step

# Common industry timing (checkout +1h, +20h, +40h between mails). Not "marketing" for the frequency cap: a reminder about
# the buyer's own cart must not wait 16h behind a welcome mail. Sent to everyone who has not opted out
# (audience) — the holdout test measures whether that is worth it.
FLOW = Flow(
    name="abandoned_checkout",
    steps=[
        Step(1, timedelta(hours=1), marketing=False),
        Step(2, timedelta(hours=21), marketing=False),
        Step(3, timedelta(hours=61), marketing=False),
    ],
    exit_on={"order_placed"},
    audience="not_unsubscribed",
)
