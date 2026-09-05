import time
from datetime import datetime

import config
import scanner
from paper_broker import PaperBroker


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def run():
    broker = PaperBroker()

    live = None
    if config.LIVE_TRADING:
        import live_broker
        live = live_broker.LiveBroker()

    log("Polymarket sureshot bot started (paper trading, no real funds at risk)"
        if live is None else "Polymarket sureshot bot started -- LIVE TRADING ACTIVE")
    log(f"Filter: price in [{config.PRICE_MIN}, {config.PRICE_MAX}], "
        f"min volume ${config.MIN_VOLUME}, min liquidity ${config.MIN_LIQUIDITY}")

    while True:
        try:
            settled = broker.check_resolutions()
            for trade in settled:
                log(f"SETTLED  {trade['question'][:60]!r} [{trade['outcome_label']}] "
                    f"pnl={trade['pnl']:+.2f} ({trade['note']})")

            opportunities = scanner.find_opportunities(held_token_ids=broker.held_token_ids)
            for opp in opportunities:
                if live is not None:
                    resp = live.place_buy(opp.token_id, opp.confirmed_price, config.STAKE_PER_TRADE)
                    log(f"LIVE BUY {opp.question[:60]!r} [{opp.outcome_label}] "
                        f"@ {opp.confirmed_price:.3f} -> {resp}")
                    continue

                position, reason = broker.open_position(opp)
                if position is None:
                    log(f"SKIP     {opp.question[:60]!r} [{opp.outcome_label}] -- {reason}")
                    continue
                log(f"PAPER BUY {opp.question[:60]!r} [{opp.outcome_label}] "
                    f"@ {opp.confirmed_price:.3f} stake=${position['stake']:.2f}")

            summary = broker.summary()
            log(f"Balance=${summary['balance']:.2f} open={summary['open_positions']} "
                f"exposure=${summary['open_exposure']:.2f} closed={summary['closed_trades']} "
                f"(W{summary['wins']}/L{summary['losses']}) realized_pnl={summary['realized_pnl']:+.2f}")

        except Exception as exc:
            log(f"ERROR: {exc}")

        time.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log("Stopped.")
