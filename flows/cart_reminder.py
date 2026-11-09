from datetime import timedelta

from flows import Flow, Step

# Common industry timing (+2 h, then a day later). Mail 1 is about their own cart, so it does not wait behind the frequency
# cap; mail 2 does. A checkout start hands over to abandoned_checkout, an order ends it.
FLOW = Flow(
    name="cart_reminder",
    steps=[Step(1, timedelta(hours=2), marketing=False), Step(2, timedelta(hours=26))],
    exit_on={"order_placed", "checkout_started"},
)
