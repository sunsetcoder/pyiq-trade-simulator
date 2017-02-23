# coding=utf-8

import iqfeed as iq
import numpy as np
import sys
import logging
from time import sleep
from pytz import timezone
from datetime import datetime, timedelta, time
from pandas import DataFrame, read_csv, Series, concat
from localconfig import dtn_product_id, dtn_login, dtn_password
from multiprocessing import Queue
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
from matplotlib.finance import candlestick_ohlc
from matplotlib import style as mplStyle


TIMEZONE = timezone('US/Eastern')
TIME_DATE_FORMAT = '%Y-%m-%d %H:%M:%S.%f'
TIME_FORMAT = '%H:%M:%S.%f'
DATE_FORMAT = '%Y-%m-%d'
LISTEN_LABELS = ['Symbol', 'Last', 'Bid', 'Ask', 'Tick', 'Size', 'Datetime', 'Open', 'Close',
                 'High', 'Low', 'UpTicks', 'DownTicks', 'TotalTicks', 'UpVol', 'DownVol', 'TotalVol']
UPDATES_LABELS = ['Symbol', 'Last', 'Bid', 'Ask', 'Size', 'Datetime', 'Open', 'High', 'Low',
                  'Close', 'UpVol', 'DownVol', 'TotalVol', 'UpTicks', 'DownTicks', 'TotalTicks']
mplStyle.use('dark_background')
lgr = logging.getLogger('simulator')
lgr.setLevel(logging.DEBUG)
FORMATTER = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
fh = logging.FileHandler("simulator.log")
fh.setLevel(logging.DEBUG)
fh.setFormatter(FORMATTER)
lgr.addHandler(fh)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(FORMATTER)
lgr.addHandler(ch)


class HandyListener(iq.VerboseQuoteListener):

    def __init__(self, name: str, queue: Queue, output_type: str='queue'):
        super().__init__(name)
        self.queue = queue
        self.output_type = output_type  # 'queue', 'console'
        self.lgr = logging.getLogger("HandyListener")
        self.lgr.setLevel(logging.DEBUG)
        fh2 = logging.FileHandler("simulator_iqfeed_updates.log")
        fh2.setLevel(logging.DEBUG)
        fh2.setFormatter(FORMATTER)
        self.lgr.addHandler(fh2)

    def process_update(self, update: np.array) -> None:
        assert len(update) == 1, "Received multiple updates. This is unexpected."
        update = update[0]
        data = Series(list(update) + [0] * 10, index=LISTEN_LABELS)

        # If trade occurs at ask, then add volume to UpVolume
        if data.Last == data.Ask:
            data.UpVol += data.Size
        # If trade occurs at bid, then add volume to DownVolume
        elif data.Last == data.Bid:
            data.DownVol -= data.Size

        # If Tick direction is 1, add to UpTicks
        if data.Tick == 1:
            data.UpTicks += 1
        # If Tick direction is -1, add to DownTicks
        elif data.Tick == -1:
            data.DownTicks -= 1

        # Every tick is added to TotalTicks
        data.TotalTicks = abs(data.Tick)
        data.TotalVol = abs(data.Size)

        # Open, Close, High, and Low are set to trade price in preparation for re-sampling into minute bars
        data.loc[['Open', 'Close', 'High', 'Low']] = data.Last

        # Configure datetime
        date = datetime.now(TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        data.Datetime = date + timedelta(microseconds=int(data.Datetime))

        # Add update data to debug output
        t1 = data.Datetime.strftime(TIME_DATE_FORMAT)
        debug_labels = UPDATES_LABELS.copy()
        debug_labels.remove("Datetime")
        update_debug_str = "{}, {}, {}".format(t1, data.loc[debug_labels].values, update[6])
        self.lgr.debug("qsize: {}, update: {}".format(self.queue.qsize(), update_debug_str))

        # Place update data in queue for retrieval from simulator
        self.queue.put(data.loc[UPDATES_LABELS])


class Simulator(object):
    FIELD_NAMES = ['Symbol', 'Last', 'Bid', 'Ask', 'Tick', 'Last Size', 'Last Time']
    MARKET_OPEN = time(hour=8)
    MARKET_CLOSE = time(hour=16)

    def __init__(self, ticker: str, days_back: int=1, backtest: bool=False, offline: bool=False):
        self.ticker = ticker
        self.daysBack = days_back
        self.backtest = backtest
        self.backtest_period = 1
        self.bt_minutes = None
        self.offline = offline
        self.watching = False
        self.queue = Queue()
        self.marketHoursOnly = True
        self.trades = DataFrame(columns=['Price', 'Type'])
        self.minute_bars = DataFrame(columns=['Open', 'High', 'Low', 'Close', 'UpVol', 'DownVol',
                                              'TotalVol', 'UpTicks', 'DownTicks', 'TotalTicks'])
        self.updates = DataFrame(columns=UPDATES_LABELS)
        self.received_updates = False
        self.connector = None
        self.quote_conn = None
        self.trade_listener = None
        lgr.info("Starting new session...\n"+"#"*90+"\n\n"+"#"*90)
        lgr.info("Market hours are set to 8AM - 4PM EST")
        self.load_minute_data()
        self.load_trades()
        self.max_bars = 120
        self.bar_width = .0004
        self.currentChartTime = None
        self.fig = plt.figure()
        self.ax1 = plt.subplot2grid((1, 1), (0, 0)) #
        self.ax1.set_autoscaley_on(True)

    def start(self, output_type: str='queue'):
        if not self.offline:
            Simulator.launch_service()
            # sleep(10)
            self.quote_conn = iq.QuoteConn(name="Simulator-trades_only")
            self.trade_listener = HandyListener("Trades Listener", self.queue, output_type)
            self.quote_conn.add_listener(self.trade_listener)

        try:
            self.connector = iq.ConnConnector([self.quote_conn])
            self.quote_conn.connect()
            # sleep(2)
        except Exception as e:
            if not self.offline:
                lgr.critical("Failed to connect to with quote_conn!")
                lgr.critical(e)
                sys.exit()
            return False
        self.quote_conn.select_update_fieldnames(Simulator.FIELD_NAMES)
        self.quote_conn.timestamp_off()
        self.quote_conn.trades_watch(self.ticker)
        self.watching = True
        sleep(4)
        self.download_missing(self.daysBack)
        self.update_minute_bars()
        return True

    def stop(self):
        if self.watching:
            self.quote_conn.unwatch(self.ticker)
            self.quote_conn.disconnect()
            self.watching = False
            lgr.info("Done watching for trades.")

    def get_minute_bars(self, count: int, as_dataframe: bool=False):
        """
        Returns: npArray[Date, Time, Open, High, Low, Close, UpVol, DownVol, TotalVol, UpTicks, DownTicks, TotalTicks]
        """
        lgr.debug("Getting minute bars. count={}".format(count))
        if self.backtest and len(self.minute_bars) < count:
            for _ in range(count - len(self.minute_bars)):
                self.wait_next_bar()

        if not self.offline:
            self.update_minute_bars()
            ts = datetime.now(TIMEZONE).replace(tzinfo=None)
            timeSLT = ts - self.minute_bars.index[-1] - timedelta(minutes=1)  # time since last bar closed
            if timeSLT > timedelta(minutes=5):
                lgr.warning("It has been {} day(s), {:.0f} hour(s), and {:.0f} minute(s) since the last close on record!".
                            format(timeSLT.days, timeSLT.seconds / 3600, timeSLT.seconds % 3600 / 60))

        if as_dataframe:
            return self.minute_bars.tail(count).copy()
        else:
            to_send = self.minute_bars.tail(count).copy()
            # to_send.insert(0, 'Time', [datetime.strftime(x, TIME_FORMAT) for x in to_send.index])
            # to_send.insert(0, 'Date', [datetime.strftime(x, DATE_FORMAT) for x in to_send.index])
            to_send.insert(0, 'Time', [x.time() for x in to_send.index])
            to_send.insert(0, 'Date', [x.date() for x in to_send.index])
            return to_send.values

    def update_minute_bars(self):
        lgr.debug("Updating minute bars. len(updates)={}, len(minutes)={}".format(len(self.updates), 
                                                                                  len(self.minute_bars)))
        ts = datetime.now(TIMEZONE).replace(tzinfo=None)
        self.get_updates()
        if self.updates is None or len(self.updates) == 0:
            lgr.debug("No longer updating minute bars because no new updates are available to create them")
            return

        minuteMask = None
        if len(self.minute_bars) != 0:
            ltt = self.updates.iloc[-1].Datetime  # last trade time
            startTime = (self.minute_bars.index[-1] + timedelta(minutes=1))
            delta = ltt - startTime
            if delta < timedelta(minutes=1):
                lgr.debug("Less than a minute({}s) has passed since {} until {}".format(delta, startTime, ltt))
                lgr.debug("No longer updating minute bars")
                return
            end_time = ltt.replace(second=0, microsecond=0)
            lgr.debug("Filtering updates({}) by start={}, end={}".format(len(self.updates), startTime, end_time))
            minuteMask = (startTime < self.updates.Datetime) & (self.updates.Datetime < end_time)
            toMinutes = self.updates.loc[minuteMask, :].copy()

            if len(toMinutes) > 0:
                # Add last minute bar to beginning of updates so that when resampling no gaps are created
                m = self.minute_bars.iloc[-1]
                self.minute_bars = self.minute_bars.iloc[:-1]
                toMinutes.loc[(toMinutes.index[-1] + 1), :] = [0] * 16
                toMinutes = toMinutes.shift(1)
                toMinutes.loc[toMinutes.iloc[0].name] = ['', m.name, m.Open, m.High, m.Low, m.Close, .0, 0, .0, .0, m.UpVol,
                                                         m.DownVol, m.TotalVol, m.UpTicks, m.DownTicks, m.TotalTicks]
        else:
            # lgr.debug("No minute da")
            toMinutes = self.updates

        lgr.debug("Updates To Minutes: {}\n{}".format(len(toMinutes), glimpse(toMinutes)))
        if len(toMinutes) < 1:
            lgr.debug("No longer updating minute bars due to a lack of data")
            return

        resampled = DataFrame()
        resampled['Open'] = toMinutes.loc[:, ['Open', 'Datetime']].resample(
            'T', on='Datetime').first().Open
        resampled['High'] = toMinutes.loc[:, ['High', 'Datetime']].resample(
            'T', on='Datetime').max().High
        resampled['Low'] = toMinutes.loc[:, ['Low', 'Datetime']].resample(
            'T', on='Datetime').min().Low
        resampled['Close'] = toMinutes.loc[:, ['Close', 'Datetime']].resample(
            'T', on='Datetime').last().Close
        rem_labels = ['UpVol', 'DownVol', 'TotalVol', 'UpTicks', 'DownTicks', 'TotalTicks']
        resampled[rem_labels] = \
            toMinutes.loc[:, rem_labels + ['Datetime']].resample('T', on='Datetime').sum().loc[:, rem_labels]

        # Remove minutes for non market hours
        if self.marketHoursOnly:
            lgr.debug("Filtering for market hours.")
            # Resample minute bars to 15 minutes
            min15 = resampled.resample('15T').first()
            # Create mask where rows are not null and not equal to zero
            nanMask1 = ~(min15.Open.isnull()) & (min15.TotalVol != 0)
            # Append a row to mask that matches last minute bar of resampled in prep for upsampling
            nanMask1.loc[resampled.index[-1]] = nanMask1.iloc[-1]
            # Upsample the mask to minutes and fill values forward
            nanMask1 = nanMask1.resample('T').ffill()
            # Filter mask to only include rows in resampled
            nanMask1 = nanMask1.loc[resampled.index[0]:, ]
            # Filter minute bars by mask, keeping only those that have activity within fifteen minute time span
            resampled = resampled[nanMask1]
            # hoursMask = ((Simulator.MARKET_OPEN < resampled.index.time) |
            #              (Simulator.MARKET_OPEN == resampled.index.time)) & (resampled.index.time < Simulator.MARKET_CLOSE)
            # finalMask = (resampled.index.dayofweek < 5) & hoursMask
            # resampled = resampled.loc[finalMask, :]

        # Fill empty bars
        nanMask2 = resampled.Open.isnull()
        resampled.loc[:, 'Close'] = resampled.loc[:, 'Close'].ffill()
        resampled.loc[nanMask2, 'Open'] = resampled.loc[nanMask2, 'Close']
        resampled.loc[nanMask2, 'High'] = resampled.loc[nanMask2, 'Close']
        resampled.loc[nanMask2, 'Low'] = resampled.loc[nanMask2, 'Close']
        resampled.fillna(0, inplace=True)

        if minuteMask is None:
            self.updates = self.updates[[False] * len(self.updates)]
        else:
            self.updates = self.updates[~minuteMask]
        lgr.debug("Adding {} bars to minute data. updates={}\n{}".format(len(resampled),
                                                                         len(self.updates), glimpse(resampled)))
        self.minute_bars = concat([self.minute_bars, resampled])

        if not self.offline:
            self.save_minute_data()

    def get_updates(self):
        update_count = self.queue.qsize()
        lgr.debug("Getting updates. qsize={}, current updates={}".format(self.queue.qsize(), len(self.updates)))
        if update_count > 0:
            self.received_updates = True
            for _ in range(update_count):
                i = 0 if len(self.updates) == 0 else self.updates.iloc[-1].name
                self.updates.loc[i + 1] = self.queue.get()
            self.update_chart()
        else:
            self.received_updates = False

    def wait_next_bar(self):
        if self.backtest:
            # Grab next bar from self.bt_minutes and add to self.minutes
            if len(self.bt_minutes) != 0:
                self.minute_bars = self.minute_bars.append(self.bt_minutes.iloc[0])
                self.bt_minutes = self.bt_minutes.iloc[1:]
            else:
                lgr.info("Finished backtesting!")
                sys.exit()
        else:
            dt = datetime.now(TIMEZONE).replace(tzinfo=None)
            sec = dt.second + dt.microsecond / 1e6
            to_wait = 60 - sec
            lgr.info("Waiting {:.1f} seconds for next bar. updates={}, qsize={}".format(to_wait,
                                                                                        len(self.updates),
                                                                                        self.queue.qsize()))
            while to_wait > 0:
                sleep(1)
                self.update_minute_bars()
                dt = datetime.now(TIMEZONE).replace(tzinfo=None)
                sec = dt.second + dt.microsecond / 1e6
                to_wait = 60 - sec
                lgr.debug("Waiting {:.1f} seconds for next bar. updates={}, qsize={}".format(to_wait,
                                                                                             len(self.updates),
                                                                                             self.queue.qsize()))

    def buy(self, price):
        if self.backtest:
            dt = self.minute_bars.iloc[-1].name
        else:
            dt = datetime.now(TIMEZONE).replace(tzinfo=None)
        self.trades.loc[dt] = [rnd(price), 'long-entry']
        lgr.info("Long entry at {}".format(rnd(price)))
        self.save_trades()

    def limit_buy(self, price, delay=0.1):
        lgr.info("Waiting to enter long position at price={}".format(rnd(price)))
        while True:
            if self.backtest:
                # Get next bar()
                self.wait_next_bar()

                # Check for target price
                close = self.minute_bars.iloc[-1].Close
                if price >= close:
                    self.buy(close)
                    return
            else:
                # Get updates
                self.get_updates()

                if self.received_updates:

                    # Check for target price
                    for k, row in self.updates.iterrows():
                        if price >= row.Last:
                            self.buy(row.Last)
                            return

                    log_args = rnd(row.Last), rnd(price), len(self.updates), self.queue.qsize()
                    lgr.info("Awaiting long entry: [current price={}, target={}, updates={}, qsize={}]".
                             format(*log_args))

                    self.update_minute_bars()

                # Update minutes
                sleep(delay)

    def cover(self, price):
        if self.backtest:
            dt = self.minute_bars.iloc[-1].name
        else:
            dt = datetime.now(TIMEZONE).replace(tzinfo=None)
        self.trades.loc[dt] = [rnd(price), 'short-exit']
        lgr.info("Short exit at {}".format(rnd(price)))
        self.save_trades()

    def limit_cover(self, target, stop, delay=0.1):
        lgr.info("Waiting to exit short position at target={}, stop={}".format(rnd(target), rnd(stop)))
        while True:
            if self.backtest:
                # Get next bar()
                self.wait_next_bar()

                # Check for target or stop
                close = self.minute_bars.iloc[-1].Close
                if target >= close or close >= stop:
                    self.cover(close)
                    return
            else:
                # Get updates
                self.get_updates()

                if self.received_updates:

                    # Check for target or stop
                    for k, row in self.updates.iterrows():
                        if target >= row.Last or row.Last >= stop:
                            self.cover(row.Last)
                            return

                    log_args = rnd(row.Last), rnd(target), rnd(stop), len(self.updates), self.queue.qsize()
                    lgr.info("Awaiting short exit: [price={}, target={}, stop={}, updates={}, qsize={}]".
                             format(*log_args))

                    self.update_minute_bars()

                # Update minutes
                sleep(delay)

    def limit_sell(self, target, stop, delay=0.1):
        lgr.info("Waiting to exit long position at target={} ,stop={}".format(rnd(target), rnd(stop)))
        while True:
            if self.backtest:
                # Get next bar()
                self.wait_next_bar()

                # Check for target or stop
                close = self.minute_bars.iloc[-1].Close
                if stop >= close or close >= target:
                    self.sell(close)
                    return
            else:
                # Get updates
                self.get_updates()

                if self.received_updates:

                    # Check for target or stop
                    for k, row in self.updates.iterrows():
                        if stop >= row.Last or row.Last >= target:
                            self.sell(row.Last)
                            return

                    log_args = rnd(row.Last), rnd(target), rnd(stop), len(self.updates), self.queue.qsize()
                    lgr.info("Awaiting long exit: [price={}, target={}, stop={}, updates={}, qsize={}]".
                             format(*log_args))
                    self.update_minute_bars()

                # Update minutes
                sleep(delay)

    def sell(self, price):
        if self.backtest:
            dt = self.minute_bars.iloc[-1].name
        else:
            dt = datetime.now(TIMEZONE).replace(tzinfo=None)
        self.trades.loc[dt] = [rnd(price), 'long-exit']
        lgr.info("Long exit at {}".format(rnd(price)))
        self.save_trades()

    def short(self, price):
        if self.backtest:
            dt = self.minute_bars.iloc[-1].name
        else:
            dt = datetime.now(TIMEZONE).replace(tzinfo=None)
        self.trades.loc[dt] = [rnd(price), 'short-entry']
        lgr.info("Short entry at {}".format(rnd(price)))
        self.save_trades()

    def limit_short(self, price, delay=0.1):
        lgr.info("Waiting to enter short position at price={}".format(rnd(price)))
        while True:
            if self.backtest:
                # Get next bar()
                self.wait_next_bar()

                # Check for target price
                close = self.minute_bars.iloc[-1].Close
                if price <= close:
                    self.short(close)
                    return
            else:
                # Get updates
                self.get_updates()

                if self.received_updates:

                    # Check for target price
                    for k, row in self.updates.iterrows():
                        if price <= row.Last:
                            self.short(row.Last)
                            return

                    log_args = rnd(row.Last), rnd(price), len(self.updates), self.queue.qsize()
                    lgr.info("Awaiting short entry: [current price={}, target={}, updates={}, qsize={}]".
                             format(*log_args))

                    self.update_minute_bars()

                # Update minutes
                sleep(delay)

    def save_minute_data(self):
        # print("minutes.csv[", end='')
        if self.minute_bars is not None:
            if not self.backtest:
                # self.minutes.Datetime = self.minutes.Datetime.apply(lambda x: datetime.strftime(x, TIME_DATE_FORMAT))
                self.minute_bars.to_csv("minute_bars.csv")
                lgr.debug("Saved minute data to minute_bars.csv")
        else:
            lgr.error("Failed to save minute data, value=None")
        # print("finished.")

    def load_minute_data(self):
        try:
            if self.backtest:
                self.bt_minutes = read_csv("minute_bars.csv", index_col=0, parse_dates=True)
            else:
                self.minute_bars = read_csv("minute_bars.csv", index_col=0, parse_dates=True)
            lgr.info("Loaded minute_bars.csv")
        except Exception as e:
            lgr.error("Failed to load minute_bars.csv! {} ".format(e.args))

    def save_trades(self):
        if self.trades is not None:
            if self.backtest:
                self.trades.to_csv("backtest_trades.csv")
            else:
                self.trades.to_csv("trades.csv")
        else:
            lgr.error("Failed to save trades: value is None ")

    def load_trades(self):
        if not self.backtest:
            try:
                self.trades = read_csv("trades.csv", index_col=0, parse_dates=True)
                lgr.info("Loaded previous trades from trades.csv")
            except Exception as e:
                lgr.error("Failed to read trades.csv! {} ".format(e.args))

    def download_missing(self, max_days: int=1):
        if self.offline:
            lgr.info("Simulator is in offline mode...")
            return
        if len(self.minute_bars) == 0:
            startTime = datetime.now(TIMEZONE).replace(tzinfo=None) - timedelta(days=max_days)
        else:
            startTime = self.minute_bars.index[-1] + timedelta(seconds=60)
            # startTime = startTime.tz_localize(TIMEZONE)

        qsize = self.queue.qsize()
        # endTime = datetime.now(TIMEZONE).replace(tzinfo=None)
        for _ in range(qsize):
            self.queue.get()
        self.updates = self.get_ticks_for_period(start=startTime, end=None)
        # What if as part of update minute bars we check to see if there is any gap between currently loaded minute bars
        # and the current time stamp.
        # If there is a gap, then we download tick data for the period starting after loaded minutes end up until now

    def get_ticks_for_period(self, start: datetime, end: datetime):
        """Return tick data for specified period."""

        if self.offline:
            lgr.info("Simulator is in offline mode...")
            return np.array([])
        if end is None:
            periodLength = datetime.now(TIMEZONE).replace(tzinfo=None) - start
        else:
            periodLength = end - start
        days, hours, minutes = (periodLength.days,
                                int(periodLength.seconds / 3600),
                                int(periodLength.seconds % 3600 / 60))
        if days > 5:
            ans = input("Request tick data for {} days?[y]/n: ".format(days))
            if ans.lower() not in ['', 'y', 'yes']:
                lgr.critical("Consider deleting minute_bars.csv and trying again!")
                sys.exit()

        lgr.info("Downloading tick data for {} day(s), {} hour(s) and {} minute(s). Please wait...".format(
            days, hours, minutes))

        hist_conn = iq.HistoryConn(name="iqfeed-get-ticks-for-period")
        hist_listener = iq.VerboseIQFeedListener("History Tick Listener")
        hist_conn.add_listener(hist_listener)

        with iq.ConnConnector([hist_conn]) as connector:
            try:
                # Get all ticks between start time and end time
                tick_data = hist_conn.request_ticks_in_period(ticker=self.ticker,
                                                              bgn_prd=start,
                                                              end_prd=end)

                lgr.info("Download finished. Calculating volume and tick data...")
                df = DataFrame(np.flipud(tick_data))
                df.drop(['cond1', 'cond2', 'cond3', 'cond4', 'mkt_ctr', 'last_type', 'tick_id', 'tot_vlm'], axis=1, inplace=True)
                df.columns = ['date', 'time', 'Last', 'Size', 'Bid', 'Ask']
                df['prev'] = [0] * len(df)
                df.loc[1:, 'prev'] = list(df.iloc[:-1]['Last'])
                df.loc[:, 'change'] = df['Last'] - df['prev']
                df['UpVol'] = [0] * len(df)
                df['DownVol'] = [0] * len(df)
                df.loc[:, 'UpVol'] = df.Size.astype(dtype='int') * ((df['Last'] == df.Ask) & (df.Bid != df.Ask))
                df.loc[:, 'DownVol'] = -1 * df.Size.astype(dtype='int') * ((df['Last'] == df.Bid) & (df.Bid != df.Ask))
                df.loc[:, 'TotalVol'] = df.UpVol + df.DownVol.abs()
                df['UpTicks'] = [0] * len(df)
                df['DownTicks'] = [0] * len(df)
                df.loc[:, 'UpTicks'] = 1 * (df.change > 0)
                df.loc[:, 'DownTicks'] = -1 * (df.change < 0)
                df.loc[:, 'TotalTicks'] = df.UpTicks + df.DownTicks.abs()
                df.insert(0, 'Datetime', df.date + df.time)
                df.insert(0, 'Symbol', self.ticker)
                df.drop(['date', 'time', 'prev', 'change'], axis=1, inplace=True)
                for label in ['Close', 'Low', 'High', 'Open']:
                    df.insert(2, label, df.Last)
                lgr.info("Done calculating new fields.")
                return df

            except (iq.NoDataError, iq.UnauthorizedError) as err:
                lgr.critical("No data returned because {0}".format(err))
                sys.exit()

    @staticmethod
    def launch_service():
        """Check if IQFeed.exe is running and start if not"""

        svc = iq.FeedService(product=dtn_product_id,
                             version="Debugging",
                             login=dtn_login,
                             password=dtn_password)
        svc.launch()

        # If you are running headless comment out the line above and uncomment
        # the line below instead. This runs IQFeed.exe using the xvfb X Framebuffer
        # server since IQFeed.exe runs under wine and always wants to create a GUI
        # window.
        # svc.launch(headless=True)

    def set_market_hours(self, start_hour: int=8, start_minute: int=0, end_hour: int=16, end_minute: int=0):
        Simulator.MARKET_OPEN = time(hour=start_hour, minute=start_minute)
        Simulator.MARKET_CLOSE = time(hour=end_hour, minute=end_minute)
        lgr.info("Market hours have been changed to start={}, end={}".format(Simulator.MARKET_OPEN,
                                                                             Simulator.MARKET_CLOSE))

    def wait_market_hours(self):
        firstTime = True
        if self.marketHoursOnly:
            waiting = True
        else:
            waiting = False
        while waiting:
            currentTime = datetime.now(TIMEZONE).replace(tzinfo=None)
            if Simulator.MARKET_OPEN < currentTime.time() < Simulator.MARKET_CLOSE:
                waiting = False
                break
            if firstTime:
                if currentTime.time() < Simulator.MARKET_OPEN:
                    timeTillStart = datetime.combine(currentTime.date(), Simulator.MARKET_OPEN) - currentTime
                else:
                    timeTillStart = datetime.combine(currentTime.date(),
                                                     Simulator.MARKET_OPEN) + timedelta(hours=24) - currentTime
                h, m, s = (timeTillStart.seconds / 3600,
                           timeTillStart.seconds % 3600 / 60,
                           timeTillStart.seconds % 60)
                lgr.info("Waiting on market hours to begin. {:.0f} hr(s), {:.0f} min(s), {:.1f} sec(s)".format(h, m, s))
                firstTime = False
            sleep(1)

    def update_chart(self):
        self.ax1.clear()
        # self.ax1.plot

        ohlc = [(mdates.date2num(k), row.Open, row.High, row.Low, row.Close)
                for k, row in self.minute_bars.iloc[-self.max_bars:].iterrows()]

        if len(self.updates) > 0:
            lastMinute = self.minute_bars.index[-1]
            if self.currentChartTime is None or self.currentChartTime == lastMinute:
                # self.currentMinute = lastMinute.name
                self.currentChartTime = lastMinute + timedelta(minutes=1)
                self.currentChartOpen = self.updates.iloc[0].Open
                self.currentChartHigh = self.updates.High.max()
                self.currentChartLow = self.updates.Low.min()
                self.currentChartClose = self.updates.iloc[-1].Close
            else:
                self.currentChartHigh = self.updates.High.max()
                self.currentChartLow = self.updates.Low.min()
                self.currentChartClose = self.updates.iloc[-1].Close

            ohlc.append((mdates.date2num(self.currentChartTime), self.currentChartOpen,
                         self.currentChartHigh, self.currentChartLow, self.currentChartClose))
            # print("updates={}, latest price:{}".format(len(self.updates), self.currentChartClose))

        candlestick_ohlc(self.ax1, ohlc, width=self.bar_width, colorup='#77d879', colordown='#db3f3f')

        for label in self.ax1.xaxis.get_ticklabels():
            label.set_rotation(45)

        self.ax1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:00'))
        self.ax1.xaxis.set_major_locator(mticker.MaxNLocator(10))
        self.ax1.grid(color='#232323', linestyle='dashed')
        self.ax1.set_axisbelow(True)

        plt.xlabel('Time')
        plt.ylabel('Price')
        plt.title(self.ticker)
        plt.legend()
        plt.subplots_adjust(left=0.16, bottom=0.20, right=0.94, top=0.90, wspace=0.2, hspace=0)
        plt.draw()
        plt.pause(1e-7)


def glimpse(df: DataFrame, size: int=5):
    aGlimpse = None
    if len(df) > size * 2:
        aGlimpse = concat([df.head(size), df.tail(size)])
    elif len(df) <= size * 2:
        aGlimpse = df
    return aGlimpse


def rnd(val, n: int=7):
    return round(val, n)


if __name__ == "__main__":
    # TODO: Consider implementing backtesting with trade data not just minute data
    # For debugging code
    # sim1 = Simulator("@ESH17")
    # sim1.launch_service()
    # # tick_data = sim1.get_ticks_for_period(datetime(2017, 2, 16, 15, 50), datetime(2017, 2, 16, 16, 2))
    # tick_data = sim1.get_ticks_for_period(datetime(2017, 2, 16, 15, 50), datetime.now(TIMEZONE).replace(tzinfo=None))
    # tick_data.to_csv("simulator-temp-data.csv")
    pass
