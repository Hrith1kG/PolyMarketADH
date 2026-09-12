"""Lightweight stand-ins for the SDK objects the crypto strategy reads.

The crypto path only ever touches a documented handful of attributes, so
hand-built doubles keep the tests deterministic and offline -- a strategy whose
whole point is a 30-second window cannot be tested against a live clock.
"""
from datetime import datetime, timedelta, timezone

# A fixed "now" so every window assertion is exact rather than racing the clock.
NOW = datetime(2026, 9, 12, 14, 0, 0, tzinfo=timezone.utc)


class Outcome:
    def __init__(self, label, token_id, price=None):
        self.label = label
        self.token_id = token_id
        self.price = price


class Outcomes:
    def __init__(self, yes, no):
        self.yes = yes
        self.no = no


class State:
    def __init__(self, start_date, end_date, closed=False, accepting_orders=True,
                 active=True, archived=False):
        self.start_date = start_date
        self.end_date = end_date
        self.closed = closed
        self.accepting_orders = accepting_orders
        self.active = active
        self.archived = archived


class FeeSchedule:
    def __init__(self, rate=0.07, exponent=1, taker_only=True, rebate_rate=0.2):
        self.rate = rate
        self.exponent = exponent
        self.taker_only = taker_only
        self.rebate_rate = rebate_rate


class Trading:
    """Market.trading, as the live API populates it for these rounds."""

    def __init__(self, minimum_tick_size=0.01, minimum_order_size=5.0,
                 fees_enabled=True, fee_schedule=None):
        self.minimum_tick_size = minimum_tick_size
        self.minimum_order_size = minimum_order_size
        self.fees_enabled = fees_enabled
        self.fee_schedule = fee_schedule if fee_schedule is not None else FeeSchedule()


class Metrics:
    def __init__(self, volume=None, liquidity=0.0):
        self.volume = volume
        self.liquidity = liquidity


class EventRef:
    def __init__(self, slug):
        self.slug = slug


class FakeMarket:
    def __init__(self, id, slug, question, state, outcomes, metrics, events=(), trading=None):
        self.trading = trading if trading is not None else Trading()
        self.id = id
        self.slug = slug
        self.question = question
        self.state = state
        self.outcomes = outcomes
        self.metrics = metrics
        self.events = events


def canonical_slug(asset, duration_seconds, round_start):
    """The slug shape Polymarket actually emits: btc-updown-5m-1789214400."""
    if duration_seconds % 3600 == 0:
        unit = f"{int(duration_seconds // 3600)}h"
    else:
        unit = f"{int(duration_seconds // 60)}m"
    return f"{asset}-updown-{unit}-{int(round_start.timestamp())}"


def make_market(
    asset="btc",
    duration_seconds=300,
    seconds_left=20,
    market_id="1001",
    labels=("Up", "Down"),
    prices=(0.93, 0.07),
    tokens=("tok-up", "tok-down"),
    slug=None,
    question=None,
    volume=None,
    liquidity=2000.0,
    start="listing",
    listing_offset_seconds=86400,
    now=NOW,
    naive_timestamps=False,
    events=(),
    trading=None,
    **state_kwargs,
):
    """Builds one round ending `seconds_left` from `now`.

    Defaults mirror what the live Gamma API returns for these markets:

    * the slug is canonical (`btc-updown-5m-<round start epoch>`);
    * `state.start_date` is the LISTING time, ~24h before the round -- which is
      what the API really serves, and a trap for anything that tries to measure
      a round with it;
    * `metrics.volume` is None, because Gamma does not populate it on a round
      only minutes old.

    Pass `slug=` to model a non-canonical shape, or `start=None` to model the
    field being absent entirely.
    """
    end = now + timedelta(seconds=seconds_left)
    round_start = end - timedelta(seconds=duration_seconds)
    if slug is None:
        slug = canonical_slug(asset, duration_seconds, round_start)
    if start == "listing":
        start_dt = end - timedelta(seconds=listing_offset_seconds)
    else:
        start_dt = start
    if naive_timestamps:
        end = end.replace(tzinfo=None)
        if start_dt is not None:
            start_dt = start_dt.replace(tzinfo=None)
    return FakeMarket(
        id=market_id,
        slug=slug,
        question=question if question is not None else f"{asset.upper()} Up or Down",
        state=State(start_dt, end, **state_kwargs),
        outcomes=Outcomes(
            Outcome(labels[0], tokens[0], prices[0]),
            Outcome(labels[1], tokens[1], prices[1]),
        ),
        metrics=Metrics(volume, liquidity),
        events=tuple(EventRef(e) for e in events),
        trading=trading,
    )


class Level:
    def __init__(self, price, size):
        self.price = price
        self.size = size


class FakeBook:
    """Order book shaped like the SDK's: bids ascending, asks descending, best last."""

    def __init__(self, best_bid=0.92, best_ask=0.94, ask_size=500.0, bid_size=500.0,
                 timestamp=NOW, min_order_size=5.0, tick_size=0.01, one_sided=None,
                 token_id=None):
        self.token_id = token_id
        self.bids = [] if one_sided == "no_bids" else [Level(best_bid - 0.05, bid_size), Level(best_bid, bid_size)]
        self.asks = [] if one_sided == "no_asks" else [Level(best_ask + 0.05, ask_size), Level(best_ask, ask_size)]
        self.timestamp = timestamp
        self.min_order_size = min_order_size
        self.tick_size = tick_size


class FakePage:
    def __init__(self, items):
        self.items = items


class FakeClient:
    """Public client double.

    `markets` is what list_markets pages over; `books` maps token id -> FakeBook.
    Every call is counted so the tests can assert the scan does not fetch an
    order book it had no reason to fetch.
    """

    def __init__(self, markets=(), books=None):
        self.markets = list(markets)
        self.books = dict(books or {})
        self.list_calls = []
        self.book_calls = []
        self.batch_calls = []
        self.no_batch = False
        self.market_calls = []
        self.book_error = None

    def list_markets(self, **kwargs):
        self.list_calls.append(kwargs)
        return [FakePage(self.markets)]

    def get_order_books(self, token_ids):
        """Batch read, as the CLOB exposes it. Books echo their own token id."""
        if self.no_batch:
            raise AttributeError("batch endpoint unavailable")
        self.batch_calls.append(list(token_ids))
        if self.book_error is not None:
            raise self.book_error
        out = []
        for t in token_ids:
            book = self.books.get(t)
            if book is None:
                continue
            book.token_id = t
            out.append(book)
        return tuple(out)

    def get_order_book(self, token_id):
        self.book_calls.append(token_id)
        if self.book_error is not None:
            raise self.book_error
        book = self.books.get(token_id)
        if book is None:
            raise KeyError(f"no book fixture for {token_id}")
        return book

    def get_market(self, id):
        self.market_calls.append(id)
        for market in self.markets:
            if str(market.id) == str(id):
                return market
        raise KeyError(f"no market fixture for {id}")
