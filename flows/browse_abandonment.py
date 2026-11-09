from datetime import timedelta

from flows import Flow, Step

# Common industry timing (+2 h, then a day later). Marketing: only to subscribers, under the frequency cap. A cart add takes
# over (cart_reminder), a checkout start hands over to abandoned_checkout, an order ends it.
FLOW = Flow(
    name="browse_abandonment",
    steps=[Step(1, timedelta(hours=2)), Step(2, timedelta(hours=26))],
    exit_on={"order_placed", "checkout_started", "added_to_cart"},
)
