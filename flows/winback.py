from datetime import timedelta

from flows import Flow, Step

# One mail, sent as soon as winback.poll enrols someone (their last order turned 120 days old). Ordering or starting a
# checkout before the tick sends it ends it.
FLOW = Flow(
    name="winback",
    steps=[Step(1, timedelta(0))],
    exit_on={"order_placed", "checkout_started"},
)
