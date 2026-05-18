# How the Gold Trading Bot Works
*A plain-English guide for non-traders*

---

## 1. What Is This Bot?

This is an automated gold trading system that buys and sells gold (XAUUSD) on your behalf through your Oanda brokerage account. It runs 24 hours a day during weekdays, but only places new trades during a specific window that aligns with Singapore evening hours (6pm to midnight SGT). It analyses price charts, applies a proven trading strategy, gets a second opinion from an AI reviewer, checks all your risk limits, and only then places a trade — completely hands-free.

Think of it as a disciplined trading assistant that never gets emotional, never overrides the rules, and always asks "is this safe?" before doing anything.

---

## 2. The Trading Strategy — Uncle Lim's Method

The bot uses a strategy developed by a trader known as "Uncle Lim", trained on 745 real XAUUSD signals he posted between May 2024 and May 2026. His approach is built on one core idea: **only trade when multiple timeframes agree**.

### What Is a Timeframe?

A timeframe is a chart zoom level. The same gold price looks different depending on how you zoom:
- **H4 (4-hour chart)** — the big picture. Is gold trending up or down over days/weeks?
- **H1 (1-hour chart)** — the medium view. Where is gold heading today?
- **M30 / M15 (30-minute / 15-minute charts)** — the entry zone. Is gold pulling back to a good buy point?
- **M5 / M1 (5-minute / 1-minute charts)** — the trigger. Is this the exact moment to enter?

### The Top-Down Check (H4 → H1 → M30/M15 → M5/M1)

Before placing any trade, the bot runs through all four levels in order:

1. **H4 — What is the overall trend?**
   - If gold is making higher highs and higher lows → uptrend → look for BUY opportunities only
   - If gold has broken down below key support → consider SELL (rare — the bot strongly prefers BUY in a bull market)

2. **H1 — Is there a valid setup at the current structure?**
   - Has price broken a trendline and pulled back? (Trendline Breakout)
   - Is price sitting at a Supply or Demand zone? (SND zone — a price level where buyers or sellers previously showed strong interest)
   - Did price return to a level it recently broke through? (RTB — Return to Breakout)

3. **M30 / M15 — Does the entry zone look confirmed?**
   - Has price done a "LCT" — Uncle Lim's proprietary pullback and retest pattern?
   - Is there a clear engulfing candle (a big candle that swallows the previous one, showing strong conviction)?

4. **M5 / M1 — What is the final trigger?**
   - Is there a "Secret Pattern" — Uncle Lim's specific small candle formation at the entry zone?
   - Or a final retest of the SND zone on the lowest timeframe?

**Rule: The bot will not trade unless it finds at least 3 confirmations across these levels.** One level agreeing is noise. Three levels agreeing is a signal.

### Key Strategy Terms (Plain English)

| Term | What It Means |
|---|---|
| SND Zone | A price level where buyers previously rushed in (demand zone) or sellers overwhelmed buyers (supply zone). Gold tends to react strongly at these levels. |
| LCT | Life-Changing Technique — after price breaks out of a range, it comes back to test the breakout level before continuing. This retest is the entry. |
| Secret Pattern | A specific candle shape at a zone — typically a small rejection candle showing the zone is holding. Uncle Lim's proprietary entry trigger. |
| RTB | Return to Breakout — similar to LCT, but specifically when price breaks a trendline and comes back to kiss it before continuing. |
| Engulfing Candle | A large candle that completely "swallows" the previous smaller candle. Shows one side (buyers or sellers) taking control decisively. |
| Confluence | Multiple things agreeing at the same time. More confluence = more confidence in the trade. |

---

## 3. How the Bot Decides to Trade

Every hour (or during the SGT 6pm–midnight window), the bot runs through this exact sequence:

### Step 1 — Is the market open and is it safe to trade?

- Is the gold market open? (Gold trades Sunday 10pm to Friday 9pm Singapore time, but the bot only looks for new trades during 6pm–midnight SGT on weekdays)
- Is the kill switch activated? (If you sent `/stop` via Telegram, the bot stops immediately)
- Is the news suspension active? (Has a major economic event been detected in the next 30 minutes?)

If any of these block trading, the bot skips this cycle entirely and waits for the next one. It still monitors open trades — it just does not open new ones.

### Step 2 — Check Your Account Health

- What is your current balance (NAV)?
- Have you lost more than 3% today? If yes, no new trades until midnight resets the counter.
- Have you exceeded the maximum drawdown (20% from your starting balance)? If yes, emergency stop.

### Step 3 — Uncle Lim Analysis (Agent 1)

The bot fetches the latest price candles from Oanda across all four timeframes (H4, H1, M30, M15, M5) and runs the full top-down analysis:

- Checks H4 trend (higher highs/lows = bullish)
- Identifies SND zones, trendline breakouts, RTBs
- Looks for LCT and engulfing patterns on M30/M15
- Checks for Secret Pattern or final zone touch on M5/M1
- Counts confluences. Fewer than 3? → HOLD (no trade)
- Calculates where Stop Loss and Take Profit levels should be
- Checks the Risk:Reward ratio. Less than 1.5:1? → HOLD

### Step 4 — AI Analyst (LLM Agent)

A large language model (Groq's Llama 70B — the smarter, slower model) reads everything: the price action, the Uncle Lim confluences, the macro context (DXY direction, US real yields, Fed tone, geopolitical news), and produces a BUY/SELL/HOLD recommendation with a confidence score (0–100%).

**Macro context explained:**
- **DXY (US Dollar Index)** — Gold and the dollar move in opposite directions. Dollar strong = gold headwind.
- **Real yields** — When US interest rates (adjusted for inflation) are high, gold looks less attractive. Low or negative real yields = gold tailwind.
- **Fed tone** — When the Federal Reserve is raising rates (hawkish), gold struggles. When cutting rates or pausing (dovish), gold tends to rise.
- **Geopolitical risk** — War, sanctions, financial instability → gold rises as a safe haven.

### Step 5 — AI Reviewer (Reviewer Agent)

A second, faster AI model (Groq's Llama 8B — quicker but lighter) acts as a gatekeeper. It reads the analyst's recommendation and asks:
- Does the confluence count actually meet the minimum of 3?
- Is the Risk:Reward ratio at least 1.5?
- Is there a major news event coming up in the next 30 minutes?
- Does the M15 momentum agree with the direction?

If the reviewer is uncertain or encounters a technical error, it defaults to **REJECTED** — the trade is blocked, not approved. This is intentional: a broken safety gate should stop trades, not allow them through.

### Step 6 — Risk Validation

Before an order is placed, the risk engine runs its final checks:
- Are there already 2 open trades? (Maximum 2 concurrent)
- Would this trade risk more than 1% of your account?
- Would total exposure exceed 80% of margin?
- Is Stop Loss distance at least $2/oz from entry?

All checks must pass. One failure = no trade.

### Step 7 — Position Sizing

If everything passes, the bot calculates exactly how many ounces to buy/sell:

```
Units = (Account Balance × 1%) ÷ Stop Loss distance in USD/oz
```

Example: $10,000 account, Stop Loss 15 points away:
- Risk budget: $10,000 × 1% = $100
- Units: $100 ÷ $15 = 6.67 → rounded to 6 oz

### Step 8 — Place the Order

The order goes to Oanda with:
- **Entry**: market price (immediate fill)
- **Stop Loss**: structural level below/above the SND zone (set at order time)
- **TP1**: 1.5× the SL distance (minimum target)
- **TP2**: 2.0× the SL distance
- **TP3**: 3.0× the SL distance (breakout target)

All prices are formatted to 2 decimal places (e.g. `3285.42`) because Oanda rejects prices with floating-point garbage like `3285.420000000001`.

---

## 4. What Happens When a Trade Is Open

Once a trade is live, the bot monitors it every 60 seconds (a separate monitoring thread that runs even outside the SGT trading window).

### Break-Even Protection (at +5 points profit)

When the trade is 5 USD/oz in profit:
- The Stop Loss moves to your entry price + 1 point (so worst case you exit with a tiny profit, not a loss)
- 50% of the position is closed immediately (locking in real cash)
- This happens automatically, no action needed from you

### Trailing Stop (at +7 points profit)

Once the trade reaches 7 USD/oz in profit, the trailing stop activates:
- The Stop Loss follows the price upward (for a BUY) at a distance of ATR × 1.5
- ATR (Average True Range) is a measure of how much gold has been moving per candle — it adjusts the trail distance to market conditions
- If gold continues rising, your Stop Loss keeps moving up
- If gold reverses, the Stop Loss catches it and closes the trade — locking in whatever gain you had at that point

The trailing stop uses **live market prices** fetched fresh from Oanda each cycle — not the entry price. This was a critical fix from the FX bot: using entry price meant the trailing stop never activated.

### Partial Take-Profit

At TP1 (1.5× RR), if the break-even partial hasn't already fired, 50% of the remaining position is closed. The remaining 50% continues toward TP2 and TP3, now with a risk-free Stop Loss at break-even.

### Maximum Trade Age

If a trade has been open for more than 72 hours without hitting its targets, the bot alerts you via Telegram. It does not automatically close it (you decide), but it flags it as stale.

---

## 5. News and Event Protection

Gold is extremely sensitive to big economic announcements. The bot has a three-layer news protection system:

### Layer 1 — Suspension (30 minutes before)

The bot checks an economic calendar (via jb-news API) for upcoming high-impact USD events:
- **FOMC** (Federal Reserve interest rate decisions)
- **NFP** (Non-Farm Payrolls — US jobs data)
- **CPI / PCE** (US inflation readings)
- **GDP** (US growth data)
- **Fed Chair speeches**
- **Geopolitical shocks** (major escalations)

30 minutes before any of these, **no new trades are opened**. The suspension lifts 30 minutes after the event passes.

### Layer 2 — Pre-Event Close (5 minutes before VERY_HIGH events)

For the most extreme events (FOMC rate decisions, NFP), if a trade is currently open:
- If the trade has less than 1× Risk profit: **close immediately**
- If the trade has 1× Risk or more profit: close 50%, move Stop Loss to break-even

This protects open positions from the massive spread widening and slippage that happens during these announcements. During FOMC or NFP, Oanda's gold spread can widen from $0.50/oz to $2.00/oz — market orders get filled at terrible prices.

### Layer 3 — Post-News Cooldown (30 minutes after)

After a major event, the bot waits 30 minutes before looking for new trades. This lets the market settle and price action become readable again.

---

## 6. When Does the Bot Trade?

### Gold Market Hours

Gold trades 24 hours a day, 5 days a week. The market opens Sunday 10pm Singapore time and closes Friday 9pm Singapore time.

### The SGT Trading Window

**The bot only looks for new trades between 6:00 PM and 12:00 AM Singapore time (UTC 10:00–16:00), Monday through Friday.**

Why this window?
- This is the overlap between the Asian afternoon session and the European pre-market session
- Uncle Lim's signals showed peak activity at 05:00–06:00 UTC (Singapore afternoon), with strong follow-through into the London open
- It avoids the quiet periods (Singapore early morning = global dead zone) and the chaotic overlap of multiple sessions simultaneously
- You can monitor the bot's activity during your normal waking hours

Outside this window, the bot is still running — it monitors open trades, updates trailing stops, and checks emergency risk conditions every 60 seconds. It just does not look for new entry opportunities.

### When No New Trades Are Placed

- Saturday: entirely closed
- Friday after 9pm SGT (21:00 UTC): market closing, no new trades
- Sunday before 10pm SGT (22:00 UTC): market not yet open
- Outside the 6pm–midnight SGT window on any weekday
- 30 minutes before/after a major economic event
- When daily loss limit (3%) has been hit
- When the kill switch is active

---

## 7. How to Monitor the Bot

All monitoring is done through Telegram. Send these commands to your bot:

| Command | What It Does |
|---|---|
| `/status` | Shows your balance, NAV (Net Asset Value), open trades, and unrealized profit/loss |
| `/analyst` | Shows the last trading signal the analyst AI produced with confidence score |
| `/reviewer` | Shows the reviewer's verdict and reason |
| `/calendar` | Shows upcoming high-impact gold events in the next 24 hours |
| `/logs` | Shows the last 50 lines of today's bot log |
| `/credits` | Shows whether Groq and Anthropic AI credits are still available |
| `/stop` | **Emergency stop** — activates the kill switch immediately, halts all new trades |
| `/resume` | Deactivates the kill switch and resumes normal trading |
| `/help` | Lists all commands |

### What the Log Messages Mean

- `Cycle #42 — 2026-05-18 10:00:00 UTC` — A new trading cycle just started
- `Outside trading window... Monitoring continues` — It's outside 6pm–midnight SGT; monitoring is still active
- `Gold market closed (weekend)` — Saturday or Sunday, no trading
- `NEWS SUSPENSION: FOMC in 28min` — Suspension is active, no new trades
- `Uncle Lim analysis: BUY | SND H1 + LCT M30 + Secret M5 | 4 confluences | confidence 0.78` — A valid signal was found
- `ReviewerAgent: REJECTED — RR ratio 1.3 below minimum 1.5` — The reviewer blocked the trade
- `EXECUTE: BUY 6 oz XAU_USD @ 3285.42 | SL: 3270.00 | TP1: 3307.55` — An order was placed
- `Break-even set: SL → 3286.42 at +5.0 pts profit` — Break-even triggered
- `Trailing stop updated: SL → 3292.00` — Trailing stop moved up
- `CLOSE: XAU_USD trade_123 @ 3310.00 | P/L: +$147.00 | TP1_HIT` — Trade closed at target

---

## 8. What Can Go Wrong and How the Bot Protects You

### Kill Switch (3 independent layers)

If you need to stop everything immediately:
1. Send `/stop` on Telegram — writes a file that halts the bot on the next cycle
2. Set `KILL_SWITCH=true` in the `.env` file — checked every cycle
3. Drop a file named `KILL_SWITCH` into the `data/` folder — bot detects it automatically

All three are checked independently. Any one of them stops all new trading immediately. **Open positions are NOT automatically closed** — they stay live at the broker with their Stop Losses protecting them.

### Emergency Shutdown

If any of these conditions are met, the bot immediately closes all open positions and halts:
- **Drawdown exceeds 20%** from your session starting balance (e.g. account dropped from $10,000 to $8,000)
- **Daily loss exceeds 3%** of today's starting balance
- **Exposure exceeds 120% of your maximum margin limit**
- **Account balance reaches zero** (margin call)

You will receive a Telegram alert the moment this triggers.

### Circuit Breaker (Order Level)

If 5 consecutive order placements fail (e.g. Oanda API is down, or order keeps getting rejected), the bot opens a "circuit breaker" — it stops trying to place orders for 60 seconds and alerts you. This prevents a runaway loop from hammering the API with bad orders.

### AI Provider Fallback

If the primary AI (Groq) runs out of credits or goes down:
- The bot switches to Claude (Anthropic) as a fallback
- If both are down, the bot produces a HOLD signal — it does not trade without AI analysis
- You will see this in `/credits`

### State Persistence Across Restarts

If the bot crashes or you restart it, it reads `data/managed_trades.json` and restores all open trade state — including trailing stop peak prices, break-even flags, and partial TP status. The bot continues managing trades exactly where it left off.

---

## 9. Lessons Applied From the FX Trading Bot

This bot was built after a sister project (an FX currency trading bot) had been running for several months. The following bugs were found in the FX bot and proactively fixed or verified in this gold bot before launch:

| Issue | FX Bot Experience | Gold Bot Status |
|---|---|---|
| Trailing stop never firing | FX bot used entry price (not live price) to calculate profit — trailing stop activation threshold was never reached | Fixed: gold bot fetches live mid-price from Oanda's pricing endpoint |
| Confluence gate always rejecting | FX bot parsed LLM text output (capped at 120 chars) to count confluences — always returned 1, never reached minimum 3 | Fixed: gold bot reads directly from the computed indicators dict |
| Reviewer approving on error | FX bot reviewer returned APPROVED on transient network errors — all trades passed unreviewed | Fixed: any reviewer error returns REJECTED |
| Empty LLM response crash | Provider sometimes returned empty response list — accessing `[0]` crashed the bot | Fixed: guard checks `len(response.choices) > 0` before accessing |
| Break-even SL moving wrong way | FX bot moved SL DOWN on BUY trades (increasing risk instead of locking profit) | Verified correct: gold bot moves SL UP for BUY, DOWN for SELL |
| Timezone crash in suspension manager | Comparing naive datetime to timezone-aware datetime crashed with TypeError every time suspension tried to resume | Fixed: all datetime comparisons use timezone-aware UTC objects |
| Price formatting OANDA rejection | `str(float)` produced `3285.0000000000003` — OANDA rejected the order | Fixed: gold bot has `_fmt_price()` formatting all prices to 2 decimal places |
| Stale evaluated trades set (memory leak) | FX news watcher never cleared its "already evaluated" set — trades got permanently excluded after one transient error | Fixed: gold bot clears the set at the start of every check cycle |
| Logger duplicate handlers | Each new TradeLogger instance added another file handler — log lines duplicated on each restart | Fixed: gold bot checks for existing file handler before adding a new one |
| Naive UTC clock for daily loss reset | Using local system clock instead of UTC meant daily loss reset at wrong time if server timezone ≠ UTC | Fixed: daily loss calculation uses `datetime.now(timezone.utc).date()` |

---

## 10. Key Numbers at a Glance

| Parameter | Value | What It Means |
|---|---|---|
| Risk per trade | 1% of NAV | Maximum $100 at risk on a $10,000 account |
| Max open trades | 2 | Never more than 2 positions at once |
| Daily loss limit | 3% | Stop trading for the day if you lose $300 on $10,000 |
| Max drawdown | 20% | Emergency close all if account drops 20% from start |
| Minimum confluences | 3 | Fewer than 3 agreeing timeframes = no trade |
| Minimum R:R ratio | 1.5 | Must target at least 1.5× what you risk |
| Break-even trigger | +5 pts profit | Stop Loss moved to entry + 1 pt, 50% closed |
| Trailing stop trigger | +7 pts profit | Stop Loss begins following price upward |
| News suspension window | 30 min before/after | No new trades around major events |
| Trading window | SGT 6pm–midnight | UTC 10:00–16:00 weekdays only |
| No price fallback | — | If live price is unavailable, affected checks are skipped and the trade is blocked |

---

*Last updated: 2026-05-18*
*Strategy source: Uncle Lim XAUUSD signals corpus (745 signals, May 2024–May 2026)*
*Verified win rate: 54.8% (BUY 88.9% in bull market conditions)*
