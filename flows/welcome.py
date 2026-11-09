from datetime import timedelta

from flows import Flow, Step

FLOW = Flow(
    name="welcome",
    steps=[
        Step(1, timedelta(0), marketing=False),   # delivers the requested code → exempt from cap
        # 79% of signup-buyers order within a day, so step 2 waits until they have dropped out;
        # step 3 lands 3 days before the 14-day code expires (see stores.welcome_valid_days)
        Step(2, timedelta(days=2)),
        Step(3, timedelta(days=11)),
    ],
    exit_on={"order_placed"},
)
