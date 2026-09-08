import time
from datetime import datetime

import config
import scanner
import settings_manager
from paper_broker import PaperBroker


def log(msg: str):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}")


def _broad_scan_settings(global_settings, accounts):
    """Union of thresholds across the global settings and every enabled account's own
    filters, so a single market scan covers what every independent account might want."""
    price_min = global_settings.get("price_min", 0.97)
    price_max = global_settings.get("price_max", 0.995)
    min_volume = global_settings.get("min_volume", 5000.0)
    min_liquidity = global_settings.get("min_liquidity", 1000.0)
    sports_types = set(global_settings.get("sports_market_types", ["moneyline"]) or [])

    for acc in accounts:
        acc_settings = settings_manager.get_account_settings(acc["name"])
        price_min = min(price_min, acc_settings.get("price_min") or price_min)
        price_max = max(price_max, acc_settings.get("price_max") or price_max)
        min_volume = min(min_volume, acc_settings.get("min_volume") if acc_settings.get("min_volume") is not None else min_volume)
        min_liquidity = min(min_liquidity, acc_settings.get("min_liquidity") if acc_settings.get("min_liquidity") is not None else min_liquidity)
        sports_types.update(acc_settings.get("sports_market_types") or [])

    broad = dict(global_settings)
    broad.update({
        "price_min": price_min,
        "price_max": price_max,
        "min_volume": min_volume,
        "min_liquidity": min_liquidity,
        "sports_market_types": list(sports_types) or ["moneyline"],
    })
    return broad


def _opportunity_matches_account(opp, acc_settings) -> bool:
    price_min = acc_settings.get("price_min")
    price_max = acc_settings.get("price_max")
    if price_min is not None and price_max is not None and not (price_min <= opp.confirmed_price <= price_max):
        return False
    min_volume = acc_settings.get("min_volume")
    if min_volume is not None and opp.volume < min_volume:
        return False
    min_liquidity = acc_settings.get("min_liquidity")
    if min_liquidity is not None and opp.liquidity < min_liquidity:
        return False
    sports_types = acc_settings.get("sports_market_types")
    if sports_types and opp.market_type not in sports_types:
        return False
    return True


def run():
    broker = PaperBroker()
    live = None

    broker.add_log("Polymarket Sureshot Bot initialized.")
    log("Polymarket Sureshot Bot started.")

    while True:
        try:
            settings = settings_manager.load_settings()
            status = settings.get("bot_status", "RUNNING")
            manual_trigger = settings.get("manual_scan_requested", False)
            poll_interval = settings.get("poll_interval_seconds", 60)
            live_enabled = settings.get("live_trading", False)
            kill_switch = settings.get("entry_kill_switch", False)

            # Check if execution mode changed dynamically via control panel
            if live_enabled and live is None:
                try:
                    import live_broker
                    live = live_broker.LiveBroker()
                    log(">>> LIVE TRADING ACTIVATED DYNAMICALLY VIA DASHBOARD <<<")
                    broker.add_log(">>> LIVE TRADING ACTIVATED DYNAMICALLY <<<", level="WARNING")
                except Exception as live_init_err:
                    log(f"Failed to activate live trading: {live_init_err}")
                    broker.add_log(f"Live trading activation failed: {live_init_err}", level="ERROR")
                    live = None
            elif not live_enabled and live is not None:
                log(">>> LIVE TRADING DEACTIVATED: Switched to PAPER mode <<<")
                broker.add_log("Live trading deactivated: switched to PAPER mode.")
                live = None

            # 1. Check resolutions on existing positions
            settled = broker.check_resolutions()
            for trade in settled:
                log(f"SETTLED   {trade['question'][:50]!r} [{trade['outcome_label']}] "
                    f"pnl={trade['pnl']:+.2f} ({trade['note']})")

                # If live trading is active and the position won, claim collateral via on-chain CTF redemption
                if live is not None and trade.get("resolved_price") == 1.0:
                    trade_acc = trade.get("account_name")
                    try:
                        log(f"LIVE REDEEM: Claiming CTF collateral for market {trade['market_id']} ({trade_acc or 'All'})...")
                        outcome = live.redeem_winning_position(market_id=trade["market_id"], account_name=trade_acc)
                        tx_hash = getattr(outcome, "transaction_hash", outcome)
                        log(f"LIVE REDEEM SUCCESS: TxHash={tx_hash}")
                        broker.add_log(f"Live CTF redeemed [{trade_acc}]: {trade['question'][:40]} | Tx: {tx_hash}")
                    except Exception as redeem_err:
                        log(f"LIVE REDEEM ERROR [{trade_acc}]: {redeem_err}")
                        broker.add_log(f"CTF Redeem failed for {trade['market_id']} [{trade_acc}]: {redeem_err}", level="ERROR")

            # 2. Check if paused (unless manual scan is triggered)
            if status == "PAUSED" and not manual_trigger:
                log("Bot is PAUSED via Dashboard. Waiting for resume or manual trigger...")
                time.sleep(min(5, poll_interval))
                continue

            if manual_trigger:
                log("Manual scan triggered from Dashboard!")
                settings_manager.update_setting("manual_scan_requested", False)

            # 3. Scan active markets (filtered for sports moneyline)
            enabled_accounts = [a for a in config.get_configured_accounts() if a.get("enabled", True)] if live is not None else []
            if enabled_accounts:
                scan_settings = _broad_scan_settings(settings, enabled_accounts)
                # Held tokens are per-account below, so accounts can independently hold the
                # same token -- don't let one account's position hide the opportunity from another.
                opportunities = scanner.find_opportunities(held_token_ids=set(), settings_override=scan_settings)
            else:
                opportunities = scanner.find_opportunities(held_token_ids=broker.held_token_ids)
            broker.save_signals(opportunities)
            log(f"Scan complete: {len(opportunities)} qualifying signal(s) found.")

            # 4. If running, execute qualifying trades
            if status == "RUNNING":
                if kill_switch:
                    log("ENTRY KILL SWITCH ACTIVE: Blocking all new order entries.")
                else:
                    default_stake = settings.get("stake_per_trade", 25.0)

                    if live is not None:
                        for opp in opportunities:
                            # Each account independently decides (own pause/kill-switch/filters/limits)
                            # whether it wants this opportunity; eligible accounts still fire in parallel.
                            eligible_stakes = {}
                            for acc in enabled_accounts:
                                acc_name = acc["name"]
                                acc_settings = settings_manager.get_account_settings(acc_name)

                                if acc_settings.get("bot_status", "RUNNING") == "PAUSED":
                                    continue
                                if acc_settings.get("entry_kill_switch", False):
                                    continue
                                if not _opportunity_matches_account(opp, acc_settings):
                                    continue

                                acc_held_tokens = {str(p.get("token_id")) for p in broker.positions_for_account(acc_name).values()}
                                if opp.token_id in acc_held_tokens:
                                    continue

                                stake = settings_manager.get_account_stake(acc_name, fallback=acc.get("stake", default_stake))
                                limits = {
                                    "max_open_positions": acc_settings.get("max_open_positions"),
                                    "max_total_exposure": acc_settings.get("max_total_exposure"),
                                    "max_trades_per_day": acc_settings.get("max_trades_per_day"),
                                }
                                ok, reason = broker.can_open(stake, account_name=acc_name, limits=limits)
                                if not ok:
                                    broker.record_intention()
                                    broker.record_rejection()
                                    log(f"SKIP      [{acc_name}] {opp.question[:40]!r} [{opp.outcome_label}] -- {reason}")
                                    continue

                                eligible_stakes[acc_name] = stake

                            if not eligible_stakes:
                                continue

                            results = live.place_buy_selected(opp.token_id, opp.confirmed_price, eligible_stakes)
                            for res in results:
                                acc_name = res["account_name"]
                                wallet_addr = res["wallet"]
                                stake_used = res["stake"]

                                if res["success"]:
                                    log(f"LIVE BUY [{acc_name}] {opp.question[:45]!r} [{opp.outcome_label}] "
                                        f"@ {opp.confirmed_price:.3f} (${stake_used}) -> {res['response']}")
                                    acc_settings = settings_manager.get_account_settings(acc_name)
                                    limits = {
                                        "max_open_positions": acc_settings.get("max_open_positions"),
                                        "max_total_exposure": acc_settings.get("max_total_exposure"),
                                        "max_trades_per_day": acc_settings.get("max_trades_per_day"),
                                    }
                                    pos, _ = broker.open_position(
                                        opp,
                                        stake=stake_used,
                                        mode="LIVE",
                                        account_name=acc_name,
                                        wallet_address=wallet_addr,
                                        limits=limits,
                                    )
                                else:
                                    log(f"LIVE ERR [{acc_name}] {opp.question[:45]!r} [{opp.outcome_label}] -- {res['error']}")
                                    broker.add_log(f"Live order failed [{acc_name}]: {res['error']}", level="ERROR")
                    else:
                        # Paper trading simulation
                        for opp in opportunities:
                            ok, reason = broker.can_open(default_stake)
                            if not ok:
                                broker.record_intention()
                                broker.record_rejection()
                                log(f"SKIP      {opp.question[:50]!r} [{opp.outcome_label}] -- {reason}")
                                continue

                            position, _ = broker.open_position(opp, stake=default_stake, mode="PAPER")
                            if position is not None:
                                log(f"PAPER BUY {opp.question[:50]!r} [{opp.outcome_label}] "
                                    f"@ {opp.confirmed_price:.3f} stake=${position['stake']:.2f}")

            # 5. Print summary
            summary = broker.summary()
            log(f"Mode={'LIVE' if live else 'PAPER'} Balance=${summary['balance']:.2f} "
                f"open={summary['open_positions']} exposure=${summary['open_exposure']:.2f} "
                f"today_trades={summary['today_trades']} closed={summary['closed_trades']} "
                f"(W{summary['wins']}/L{summary['losses']}) pnl={summary['realized_pnl']:+.2f}")

        except Exception as exc:
            log(f"ERROR in loop: {exc}")
            broker.add_log(f"Loop error: {exc}", level="ERROR")

        time.sleep(poll_interval)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log("Stopped by user.")
