from datetime import timedelta

from flows import Flow, Step

# One mail as soon as stock.poll sees the requested variant buyable. Not marketing (no frequency cap) and audience
# "requested": the person asked for it, so only a hard bounce / complaint / import suppression stops it.
FLOW = Flow(
    name="back_in_stock",
    steps=[Step(1, timedelta(0), marketing=False)],
    audience="requested",
)
