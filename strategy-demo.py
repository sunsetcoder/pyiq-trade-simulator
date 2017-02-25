from simulator import Simulator
from random import random
import argparse


def get_signal_a(bars):
    # Process bars to create a signal

    # Just a random signal generator
    signal = 1 if random() > .5 else -1
    return signal


def get_signal_b(bars):
    # Process bars to create a signal

    # Just a random signal generator
    signal = 1 if random() > .5 else -1
    return signal


def get_final_signal(signals):
    # Process all signals to create a final signal

    # Ex. If all signals are positive, return 1 (buy signal)
    if sum(signals.values()) == len(signals):
        return 1
    # Ex. If all signals are negative, return -1 (sell signal)
    if sum(signals.values()) == -len(signals):
        return -1
    # Ex. Otherwise, return 0 (hold signal)
    return 0


if __name__ == "__main__":
    # Arg parsing allows passing parameters via the command line
    parser = argparse.ArgumentParser(description="This demonstrates use of pyiq-trade-simulator to test custom\n" +
                                                 "trading strategies with a live data feed through iqFeed using\n" +
                                                 "pyiqfeed.")
    parser.add_argument('-t', dest='ticker', help="ticker", default="@JY#")  # @ESH17
    parser.add_argument('-b', dest='backtest', help="enable backtesting", action='store_true')
    pargs = parser.parse_args()

    # Create simulator as live or backtesting
    daysBack = 1  # Days of historical data to download if minute_bars.csv is missing
    if pargs.backtest:
        sim = Simulator(pargs.ticker, days_back=daysBack, backtest=True, offline=True)
    else:
        sim = Simulator(pargs.ticker, days_back=daysBack)

    # Examples of chart adjustments
    # sim.bar_up_color = '#66f4f2'  # Use color picker online, e.g. http://www.colorpicker.com/
    # sim.bar_down_color = '#7541d8'
    # sim.max_bars = 120
    # sim.bar_width = .0004

    sim.market_hours_only = False  # Limit feed updates to market hours?
    # sim.charting_enabled = False  # Use this to disable charting
    sim.start()
    mySignals = {}
    stop = 3 * .0000005  # tick count * tick value (e.g. $.25 is for s&p emini) .0000005
    target = 3 * .0000005  # tick count * tick value

    # Check if market hours have begun, wait if not. Default is 8AM - 4PM EST. Times are set using:
    #     sim.set_market_hours(start_hour=int, start_minute=int, end_hour=int, end_minute=int)
    sim.wait_market_hours()

    while True:

        # Grab the most recent number of bars as necessary for signal generation
        # bars = npArray[Date, Time, Open, High, Low, Close, UpVol, DownVol, TotalVol, UpTicks, DownTicks, TotalTicks]
        bars = sim.get_minute_bars(count=10)
        last_close = bars[-1][5]

        # Calculate signals based on custom functions
        mySignals['a'] = get_signal_a(bars)
        mySignals['b'] = get_signal_b(bars)
        finalSignal = get_final_signal(mySignals)

        filled = False

        # Simulate order actions
        if finalSignal == 1:
            # Limit buy
            filled = sim.limit_buy(last_close)
            # If limit long was filled, create limit sell
            if filled:
                sim.limit_sell(last_close + target, last_close - stop)
        elif finalSignal == -1:
            # Limit short
            filled = sim.limit_short(last_close)
            # If limit short was filled, create limit cover
            if filled:
                sim.limit_cover(last_close - target, last_close + stop)

        # Wait for close of bar
        sim.wait_next_bar()


