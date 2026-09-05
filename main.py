import time
from datetime import datetime

import config
import scanner
import settings_manager
from paper_broker import PaperBroker


def log(msg: str):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}")


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
                    try:
                        log(f"LIVE REDEEM: Claiming on-chain CTF collateral for market {trade['market_id']}...")
                        outcome = live.redeem_winning_position(market_id=trade["market_id"])
                        tx_hash = getattr(outcome, "transaction_hash", outcome)
                        log(f"LIVE REDEEM SUCCESS: TxHash={tx_hash}")
                        broker.add_log(f"Live CTF redeemed: {trade['question'][:40]} | Tx: {tx_hash}")
                    except Exception as redeem_err:
                        log(f"LIVE REDEEM ERROR: {redeem_err}")
                        broker.add_log(f"CTF Redeem failed for {trade['market_id']}: {redeem_err}", level="ERROR")

            # 2. Check if paused (unless manual scan is triggered)
            if status == "PAUSED" and not manual_trigger:
                log("Bot is PAUSED via Dashboard. Waiting for resume or manual trigger...")
                time.sleep(min(5, poll_interval))
                continue

            if manual_trigger:
                log("Manual scan triggered from Dashboard!")
                settings_manager.update_setting("manual_scan_requested", False)

            # 3. Scan active markets (filtered for sports moneyline)
            opportunities = scanner.find_opportunities(held_token_ids=broker.held_token_ids)
            broker.save_signals(opportunities)
            log(f"Scan complete: {len(opportunities)} qualifying signal(s) found.")

            # 4. If running, execute qualifying trades
            if status == "RUNNING":
                if kill_switch:
                    log("ENTRY KILL SWITCH ACTIVE: Blocking all new order entries.")
                else:
                    for opp in opportunities:
                        stake = settings.get("stake_per_trade", 25.0)
                        ok, reason = broker.can_open(stake)
                        if not ok:
                            broker.record_intention()
                            broker.record_rejection()
                            log(f"SKIP      {opp.question[:50]!r} [{opp.outcome_label}] -- {reason}")
                            continue

                        current_mode = "LIVE" if live is not None else "PAPER"
                        if live is not None:
                            try:
                                resp = live.place_buy(opp.token_id, opp.confirmed_price, stake)
                                log(f"LIVE BUY  {opp.question[:50]!r} [{opp.outcome_label}] "
                                    f"@ {opp.confirmed_price:.3f} -> {resp}")
                            except Exception as live_err:
                                log(f"LIVE ERR  {opp.question[:50]!r} [{opp.outcome_label}] -- {live_err}")
                                broker.add_log(f"Live order failed: {live_err}", level="ERROR")
                                continue

                        position, _ = broker.open_position(opp, stake=stake, mode=current_mode)
                        if live is None and position is not None:
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
