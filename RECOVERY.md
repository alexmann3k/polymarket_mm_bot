# Position recovery

The existing Bot loop and order manager now enforce the following sequence:

1. Reconcile order/trade settlement and real CLOB balances.
2. Cancel inventory-increasing orders. Pause account-wide on unsettled orders; quarantine markets with trade/balance disagreement.
3. Check the held token's external best bid for a stop loss, before market-health quoting checks.
4. Otherwise quote only the complementary BUY, subject to existing health and risk limits.
5. Manage every recovery market, then select/quote one additional normal market.

Startup retains saved markets with unmatched inventory even when they are absent from scanner
results. An active market with unmatched inventory moves into separate recovery tracking,
keeping its health history and any valid reducing order. This frees the normal market slot.
All recovery markets are visited by the existing loop and their orders can rest concurrently
alongside one normal market. RED stops that market's quoting, not tracking. A recovered market
is released only after neutral inventory and confirmed order cleanup. Capital, cash and order
limits remain account-wide; pending fills/cancellations still pause new orders until reconciled.

## Settings

```
STOP_LOSS_DISTANCE=0.30
MIN_PAIR_PROFIT=0.01
```

These defaults are loaded through Config. Existing .env credentials/settings are not modified.
Inventory epsilon is 0.01 shares (at most $0.01 payout, matching the SDK's share-size precision).
Smaller dust remains recorded and counted in capital but does not block market selection.
This is not a waiver of market minimum order sizes: a one-share imbalance remains material.

Recovery BUY price is at most `1 - held_average_entry - MIN_PAIR_PROFIT`, rounded DOWN to the
book tick. A lower normal quote remains lower. Size is at most the unmatched inventory and
normal configured size, rounded down to the SDK's two-decimal share precision. Existing smaller
partial-order remainders may stay if their price and remaining size are still safe.

## Entry cost

Inventory CSV version 3 adds weighted-average YES/NO entry cost and persistent stop intent;
versions 2 and legacy fixed-six snapshots remain readable. Quantity always comes from CLOB
balances, never submitted orders. Confirmed authenticated trade history is fetched on startup
and updated by the settlement path. If a managed market has a trade/balance mismatch, the next
inventory refresh also reloads history before reconciling balances.

Cost is replayed from own taker fills / own maker legs, including BUY and SELL quantities.
SELLs reduce cost proportionally. Paired and unmatched shares share the token's weighted-average
cost; this is not FIFO or exact unmatched-lot accounting. Decimal/fixed-six history is accepted
only when its resulting quantity explains the balance. Saved cost is not blindly reused after
restart. Transfers, merges, redemption, incomplete history or unsupported responses can make
cost unknowable; the bot pauses that market instead of estimating it from the book. A dedicated account
without concurrent manual trading remains required. Only markets known to the existing CSV /
scanner lifecycle are discovered; this is not a new wallet-wide position-discovery service.

The configured pair margin is a gross price margin, not a guarantee of net profit after fees,
gas or other costs. Share-deducted fees that prevent quantity reconciliation cause a pause.

## Stop execution and limitations

A positive `entry - STOP_LOSS_DISTANCE` threshold is compared with the held token's best bid.
A nonpositive threshold cannot trigger. Once triggered, stop intent is saved before execution
and remains latched even if price rebounds. All market orders must be cancelled/settled, then
balances refreshed before submitting a SELL for only the unmatched remainder.

The SELL is a limit FAK (fill-and-kill), not a passive GTC. Its limit crosses displayed bid depth,
subject to existing configured min/max prices and valid ticks. Order detail and trade settlement
must confirm the result before another attempt. Partial fills reduce subsequent close size;
ambiguous writes block further placement. No reverse position is intentionally created.

Spread collapse alone no longer invalidates a resting recovery BUY. Its unchanged price must
still satisfy the pair-profit cap, tick/price bounds and current ask, and its remaining size
must obey inventory and account risk limits. This also permits retention when a narrow spread
prevents forming a new two-sided quote. New quotes still use the existing quote generator.
Recovery quotes use GTD with INVENTORY_QUOTE_EXPIRATION_SECONDS=35. The Unix expiration is
ceil(time.time() + max(configured duration, GTD_MIN_EXPIRATION_SECONDS)). The current CLOB
requires it to be more than 180 seconds ahead, so the default is 240 seconds ahead rather than
an exact 35-second lifetime.
No routine age-based cancellation applies to these GTD orders. After confirmed expiry and
fill reconciliation, the existing loop can quote the remaining unmatched size again, subject
to the existing price, size and health checks. Once paired, recovery stops.
A surviving normal GTC quote is cancelled and reconciled once before replacement with GTD.
Normal GTC refresh is unchanged. A narrow spread does not bypass volatility, maximum-spread,
depth or price checks. Watchdog, heartbeat, shutdown and other hard cancellations remain active.

Other health RED/YELLOW conditions and resolution cutoff still prohibit recovery BUYs, but do not suppress a
stop on an otherwise tradable book. Closed/disabled markets cannot be traded. Minimum order
size, price floors, missing bids, unknown cost, settlement delays and exchange outages can prevent
a close; the bot retains the position while other eligible markets can continue quoting.
In particular, a one-share remainder on a book with a five-share minimum is NOT overhedged.
Dry-run submits no real close and does not simulate a fill; it logs at most one simulated stop
per recovery episode.

## Reconciliation stalls and network failures

A mismatched active market moves into retained tracking, including when its current balance
is zero. Its orders must be cancelled and settled before another market can trade. Other
markets may then continue within the existing account-wide capital and cash limits. The
quarantined market cannot quote or be discarded until reconciliation succeeds; refreshing
trade history does not guess missing transfers, merges or redemptions.

`Trading status` is logged every 30 seconds after completed cycles, including active market,
order count, quarantine, inventory refresh and settlement state. This is a progress report,
not an independent watchdog during a blocked network call.

Transient CLOB GET failures get one retry after 250 ms. HTTPX's existing network timeouts
remain enabled. Order submissions are never automatically replayed by this retry layer.
Persistent failures still use the existing bounded cycle-error and shutdown cleanup paths.
An API rejection such as `403 Trading restricted in your region` requires resolving access
with the platform; retrying or restarting does not remove that restriction.

Restart is required to load changes into a process that was already running.

## Verification

Run from the project directory:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
```

Recovery tests cover YES/NO direction, A–E, epsilon, price caps/ticks, partial hedges, minimum
size, capital/cash/position/order limits, RED/YELLOW, concurrent recovery plus one normal market, restart retention,
both stops, stop priority, partial stop fills, delayed settlement, cancellation refusal/races,
unknown cost, trade/balance lag, persisted stop intent, and weighted-average cost replay.

API contracts checked against official documentation:

- https://docs.polymarket.com/api-reference/trade/get-trades
- https://docs.polymarket.com/api-reference/wss/user
- https://docs.polymarket.com/api-reference/trade/post-a-new-order
- https://docs.polymarket.com/concepts/order-lifecycle
