# Polymarket Market Maker V1

**Polymarket Market Maker V1** is a Python-based market-making bot for binary prediction markets. It automatically discovers and ranks tradable markets, reads live CLOB order books, and places post-only quotes on YES and NO tokens. The system combines inventory-aware quoting with market-health monitoring, position and capital limits, fill/settlement reconciliation, and conservative recovery logic for uncertain exchange states. The orchestration layer coordinates scanning, risk, inventory, and execution while preserving existing orders whenever possible to retain queue position. The project includes an offline test suite, GitHub Actions CI, and a safe-by-default dry-run mode.

This version uses a fixed size of **5 shares per new order** and intentionally avoids more complex features such as dynamic sizing or Avellaneda–Stoikov quoting.

> **Safety:** `DRY_RUN=true` by default. Live trading performs a geographic eligibility check before any trading order can be placed. Dry-run mode skips this check because it does not submit real orders.

---

## Architecture

`main.py` is the application entry point, while `bot.py` coordinates the trading lifecycle.

```text
main.py              application entry point
bot.py               runtime and trading orchestration
config.py / auth.py  configuration and CLOB authentication
scanner.py           market discovery and ranking
orderbook.py         order-book normalization
fair_value.py        fair-value calculation
quoting.py           quote pricing and direction logic
risk.py              market health and risk limits
inventory.py         inventory reconciliation and persistence
orders.py            order management and settlement
market_lifecycle.py  market transitions and cleanup
tests/               offline unit tests
```

Conceptually:

```text
Scanner   → WHERE to trade
Health    → WHETHER trading is currently safe
Inventory → WHICH sides may be quoted
Quoting   → AT WHAT prices to quote
Risk      → WHETHER the exposure is allowed
Orders    → HOW orders are executed and reconciled
```

`.env.example` contains the full configuration template. Secrets, logs, caches, virtual environments, and runtime-generated data are excluded through `.gitignore`.

---

## Market Selection

The scanner periodically loads active binary YES/NO markets from Gamma and filters them by:

- trading status and time to resolution,
- price and spread bounds,
- liquidity,
- 24-hour volume.

Within the eligible universe, a middle volume percentile band is retained. Markets are ranked using local spread and liquidity percentile ranks, with lower volume used as a tie-breaker.

Volume is only a simple proxy for competition; the bot does not attempt to identify professional market makers.

The current market remains active as long as it stays eligible. Other candidates are kept as reserves.

Before switching markets, existing orders are cancelled and confirmed, inventory is reconciled, and the new candidate is checked using fresh order books.

---

## Market Health

The bot uses a simple `GREEN / YELLOW / RED` state model based on the same order-book data used for quoting.

**GREEN**
- valid prices and spreads,
- sufficient depth,
- viable post-tick quote edge.

**YELLOW**
- triggered by a price movement larger than `MOVEMENT_THRESHOLD`,
- existing quotes are cancelled immediately,
- the market must remain stable for additional observations before returning to `GREEN`.

**RED**
- spread collapse,
- missing book sides,
- resolution cutoff,
- or no viable quote after tick rounding.

`YELLOW` and `RED` states prevent new quoting.

---

## Quoting Strategy

For each token:

```text
bid = floor_to_tick(best_bid + tick)
ask = ceil_to_tick(max(best_ask - tick, mid))
```

Quotes must remain inside configured price limits, maintain the minimum required spread, and never cross the external order book.

All orders are **post-only**.

Example:

```text
External book: 0.40 / 0.46
Tick size:     0.01

Bot quote:     0.41 / 0.45
```

Unchanged target prices keep their existing orders whenever possible, preserving queue position.

After a confirmed fill, inventory is reconciled before new quotes are placed. If settlement is unresolved, quoting is paused.

New orders always use a fixed size of **5 shares**.

---

## Inventory Management

The bot tracks confirmed YES and NO holdings:

```text
net = YES shares - NO shares
```

Open and unfilled orders do not count as inventory.

| Inventory | Allowed BUY quotes |
|---|---|
| `net == 0` | YES and NO |
| `net > 0` | NO only |
| `net < 0` | YES only |

Selling requires sufficient confirmed token inventory. The bot never sells short.

When inventory is unbalanced, the bot uses the opposite token's BUY side for reduction rather than simultaneously placing multiple reduction orders that could flip the position.

Because order size is fixed at five shares, a smaller imbalance may occasionally cross through zero. After the fill and reconciliation, the permitted quoting direction adjusts accordingly.

---

## Risk and Capital Controls

The bot enforces:

- maximum position per token,
- maximum total capital,
- maximum open orders,
- available cash,
- no short selling,
- price and spread limits,
- minimum order size,
- market-health gating,
- recovery-specific constraints.

`MAX_TOTAL_CAPITAL` deliberately uses a conservative definition: held shares and planned/open BUY shares reserve capital across all markets managed by the bot.

Cash is additionally reserved using the intended purchase price.

The bot does not calculate NAV or daily PnL.

---

## Recovery and Lifecycle

A normal active market may leave the main strategy while still holding inventory.

Such markets remain under recovery management until:

```text
net == 0
AND
all orders are confirmed cancelled
```

Recovery markets continue to respect health and risk controls.

After `RED`, multiple healthy snapshots are required before recovery quoting resumes. Closed or near-resolution markets remain tracked but unquoted.

A fill during cancellation triggers another inventory reconciliation rather than assuming the market is already flat.

---

## Startup and Reconciliation

At startup the bot:

1. loads persisted inventory,
2. cancels old account orders,
3. refreshes trade and settlement state,
4. restores token and market metadata,
5. reconciles persisted balances against the exchange,
6. restores markets with remaining inventory,
7. begins quoting only after account state is confirmed.

The bot therefore does not trade from stale persisted balances.

It assumes a **dedicated trading account without parallel bots or manual orders**.

---

## Order Safety and Failure Handling

Order state is treated conservatively.

The bot:

- validates cancellation responses,
- tracks fills and settlements before reusing updated balances,
- pauses trading after failed balance reconciliation,
- treats unknown account orders as a safety condition,
- prevents new placement while settlement state remains unresolved.

A particularly important case is an order-placement timeout or connection failure.

The exchange may have accepted the order even though the bot never received a response. The bot therefore does **not** blindly retry. It enters the safety path, cancels account-wide orders, and stops.

This avoids duplicate orders when placement outcome is unknown.

---

## Geographic Trading Eligibility

Live trading checks Polymarket trading eligibility during startup.

If the detected location is not eligible, startup stops before any trading order can be placed.

Dry-run mode skips the geographic eligibility check because it does not submit real orders.

The bot does not implement VPN detection or mechanisms intended to bypass geographic restrictions.

---

## Configuration

Copy `.env.example` and configure the bot locally. Existing `.env` values override defaults.

### Trading

| Variable | Default | Meaning |
|---|---:|---|
| `DRY_RUN` | `true` | Disable live order placement |
| `ORDER_SIZE` | `5` | Fixed size of each new order |
| `MIN_PRICE / MAX_PRICE` | `0.05 / 0.95` | Allowed trading price range |
| `MIN_SPREAD / MAX_SPREAD` | `0.03 / 0.10` | Eligible external spread range |

### Risk

| Variable | Default | Meaning |
|---|---:|---|
| `MAX_POSITION_PER_SIDE` | `10` | Maximum exposure per token |
| `MAX_TOTAL_CAPITAL` | `100` | Conservative account-wide capital limit |
| `MAX_OPEN_ORDERS` | `10` | Global open-order limit |
| `MOVEMENT_THRESHOLD` | `0.03` | Market-health movement threshold |
| `SETTLEMENT_TIMEOUT_SECONDS` | `120` | Maximum unresolved settlement duration |

### Market Selection

| Variable | Default | Meaning |
|---|---:|---|
| `MIN_LIQUIDITY` | `1000` | Minimum Gamma liquidity |
| `MIN_VOLUME_24H / MAX_VOLUME_24H` | `1000 / 100000` | Eligible 24h volume range |
| `MARKET_SCAN_INTERVAL_SECONDS` | `300` | Regular market scan interval |

### Runtime

| Variable | Default | Meaning |
|---|---:|---|
| `INVENTORY_REFRESH_SECONDS` | `30` | Periodic balance reconciliation |
| `QUOTE_REFRESH_SECONDS` | `10` | Normal quote refresh interval |
| `MAX_CONSECUTIVE_ERRORS` | `5` | Errors before controlled shutdown |

See `.env.example` for the complete set of available configuration parameters.

---

## Running

Install the dependencies from `requirements.txt`.

Run the test suite:

```bash
python -m pytest -q
```

Run the bot:

```bash
python main.py
```

Always verify `DRY_RUN` before starting.

### Dry-run mode

Dry-run still uses authenticated read operations and real account/balance information, but it does **not** submit live trading orders.

It is not a complete paper-trading simulator and does not simulate fills.

---

## Tests and CI

The offline test suite covers core behaviour across:

- quoting,
- risk,
- inventory,
- order management,
- recovery,
- orchestration.

```bash
python -m pytest -q
```

GitHub Actions automatically runs the suite on pushes and pull requests.

Tests do not require live trading or submit real Polymarket orders.

---

## Sanity Check

`sanity_check.py` can validate the configured environment without placing an order.

The explicit live-order test requires:

```text
--place-test-order
```

and `DRY_RUN=false`.

It places one five-share post-only order and immediately attempts to cancel it. An immediate fill is still possible before cancellation completes.

---

## Current Limitations

Not implemented:

- WebSockets
- automatic liquidation
- merge / split / redemption
- PnL accounting and daily-loss stop
- simultaneous normal trading across multiple active markets
- dynamic order sizing
- Avellaneda–Stoikov quoting

The project intentionally prioritizes explicit state management, conservative risk controls, and understandable execution behaviour over strategy complexity.

---

## Disclaimer

This project is an experimental personal software project intended for educational and portfolio purposes.

It is not financial advice and comes with no guarantee of profitability, correctness, availability, or suitability for live trading.

Use of live trading functionality is entirely at the user's own risk and must comply with applicable platform rules and geographic restrictions.

### AI-assisted development

AI coding tools were used for parts of the implementation, refactoring, documentation, and testing.

The trading logic, system design, risk rules, project scope, and final review were defined and validated by the author.
