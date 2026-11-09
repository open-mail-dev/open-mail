from datetime import timedelta

from flows import Flow, Step

# Common industry timing (+1 day thank-you, +21 days cross-sell). Mail 1 is about their own order, so it does not wait behind
# the frequency cap; mail 2 does. A new order ends the series; orders.handle then starts a fresh one for that order.
FLOW = Flow(
    name="post_purchase",
    steps=[Step(1, timedelta(days=1), marketing=False), Step(2, timedelta(days=21))],
    exit_on={"order_placed"},
)
