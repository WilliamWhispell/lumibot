import logging
import re
import traceback
from datetime import date, timedelta

from lumibot.data_sources import PandasData
from lumibot.entities import Asset, Data
from lumibot.tools import thetadata_helper
import subprocess

START_BUFFER = timedelta(days=5)


class ThetaDataBacktesting(PandasData):
    """
    Backtesting implementation of ThetaData
    """

    def __init__(
        self,
        datetime_start,
        datetime_end,
        pandas_data=None,
        username=None,
        password=None,
        **kwargs,
    ):
        super().__init__(datetime_start=datetime_start, datetime_end=datetime_end, pandas_data=pandas_data, **kwargs)

        self._username = username
        self._password = password
        self.kill_processes_by_name("ThetaTerminal.jar")

    def kill_processes_by_name(self, keyword):
        try:
            # Find all processes related to the keyword
            result = subprocess.run(['pgrep', '-f', keyword], capture_output=True, text=True)
            pids = result.stdout.strip().split('\n')

            if pids:
                for pid in pids:
                    if pid:  # Ensure the PID is not empty
                        logging.info(f"Killing process with PID: {pid}")
                        subprocess.run(['kill', '-9', pid])
                logging.info(f"All processes related to '{keyword}' have been killed.")
            else:
                logging.info(f"No processes found related to '{keyword}'.")

        except Exception as e:
            print(f"An error occurred during kill process: {e}")

    def update_pandas_data(self, asset, quote, length, timestep, start_dt=None):
        """
        Get asset data and update the self.pandas_data dictionary.

        Parameters
        ----------
        asset : Asset
            The asset to get data for.
        quote : Asset
            The quote asset to use. For example, if asset is "SPY" and quote is "USD", the data will be for "SPY/USD".
        length : int
            The number of data points to get.
        timestep : str
            The timestep to use. For example, "1minute" or "1hour" or "1day".

        Returns
        -------
        dict
            A dictionary with the keys being the asset and the values being the PandasData objects.
        """
        search_asset = asset
        asset_separated = asset
        quote_asset = quote if quote is not None else Asset("USD", "forex")

        if isinstance(search_asset, tuple):
            asset_separated, quote_asset = search_asset
        else:
            search_asset = (search_asset, quote_asset)

        # Get the start datetime and timestep unit
        start_datetime, ts_unit = self.get_start_datetime_and_ts_unit(
            length, timestep, start_dt, start_buffer=START_BUFFER
        )

        # Check if we have data for this asset
        if search_asset in self.pandas_data:
            asset_data = self.pandas_data[search_asset]
            asset_data_df = asset_data.df
            data_start_datetime = asset_data_df.index[0]

            # Get the timestep of the data
            data_timestep = asset_data.timestep

            # If the timestep is the same, we don't need to update the data
            if data_timestep == ts_unit:
                # Check if we have enough data (5 days is the buffer we subtracted from the start datetime)
                if (data_start_datetime - start_datetime) < START_BUFFER:
                    return None

            # Always try to get the lowest timestep possible because we can always resample
            # If day is requested then make sure we at least have data that's less than a day
            if ts_unit == "day":
                if data_timestep == "minute":
                    # Check if we have enough data (5 days is the buffer we subtracted from the start datetime)
                    if (data_start_datetime - start_datetime) < START_BUFFER:
                        return None
                    else:
                        # We don't have enough data, so we need to get more (but in minutes)
                        ts_unit = "minute"
                elif data_timestep == "hour":
                    # Check if we have enough data (5 days is the buffer we subtracted from the start datetime)
                    if (data_start_datetime - start_datetime) < START_BUFFER:
                        return None
                    else:
                        # We don't have enough data, so we need to get more (but in hours)
                        ts_unit = "hour"

            # If hour is requested then make sure we at least have data that's less than an hour
            if ts_unit == "hour":
                if data_timestep == "minute":
                    # Check if we have enough data (5 days is the buffer we subtracted from the start datetime)
                    if (data_start_datetime - start_datetime) < START_BUFFER:
                        return None
                    else:
                        # We don't have enough data, so we need to get more (but in minutes)
                        ts_unit = "minute"

        # Download data from ThetaData
        try:
            # Get data from ThetaData
            date_time_now = self.get_datetime()
            df = thetadata_helper.get_price_data(
                self._username,
                self._password,
                asset_separated,
                start_datetime,
                self.datetime_end,
                timespan=ts_unit,
                quote_asset=quote_asset,
                dt=date_time_now
            )
            # save df to csv file
            # df.to_csv(f"theta_csv/{date_time_now}_{asset.strike}_{asset.expiration}_{asset.right}.csv")
        except Exception as e:
            logging.info(traceback.format_exc())
            raise Exception("Error getting data from ThetaData") from e

        if df is None:
            return None

        pandas_data = []
        data = Data(asset_separated, df, timestep=ts_unit, quote=quote_asset)
        pandas_data.append(data)
        pandas_data_updated = self._set_pandas_data_keys(pandas_data)

        return pandas_data_updated

    def _pull_source_symbol_bars(
        self,
        asset,
        length,
        timestep=None,
        timeshift=None,
        quote=None,
        exchange=None,
        include_after_hours=True,
    ):
        pandas_data_update = self.update_pandas_data(asset, quote, length, timestep)

        if pandas_data_update is not None:
            # Add the keys to the self.pandas_data dictionary
            self.pandas_data.update(pandas_data_update)

        return super()._pull_source_symbol_bars(
            asset, length, timestep, timeshift, quote, exchange, include_after_hours
        )

    # Get pricing data for an asset for the entire backtesting period
    def get_historical_prices_between_dates(
        self,
        asset,
        timestep="minute",
        quote=None,
        exchange=None,
        include_after_hours=True,
        start_date=None,
        end_date=None,
    ):
        pandas_data_update = self.update_pandas_data(asset, quote, 1, timestep)
        if pandas_data_update is not None:
            # Add the keys to the self.pandas_data dictionary
            self.pandas_data.update(pandas_data_update)

        response = super()._pull_source_symbol_bars_between_dates(
            asset, timestep, quote, exchange, include_after_hours, start_date, end_date
        )

        if response is None:
            return None

        bars = self._parse_source_symbol_bars(response, asset, quote=quote)
        return bars

    def get_last_price(self, asset, timestep="minute", quote=None, exchange=None, **kwargs):
        try:
            dt = self.get_datetime()
            pandas_data_update = self.update_pandas_data(asset, quote, 1, timestep, dt)
            if pandas_data_update is not None:
                # Add the keys to the self.pandas_data dictionary
                self.pandas_data.update(pandas_data_update)
                self._data_store.update(pandas_data_update)
        except Exception as e:
            logging.info(f"\nError get_last_price from ThetaData: {e}, {dt}, asset:{asset}")

        return super().get_last_price(asset=asset, quote=quote, exchange=exchange)

    def get_chains(self, asset):
        """
        Integrates the ThetaData client library into the LumiBot backtest for Options Data in the same
        structure as Interactive Brokers options chain data

        Parameters
        ----------
        asset : Asset
            The asset to get data for.

        Returns
        -------
        dictionary:
            A dictionary nested with a dictionary of ThetaData Option Contracts information broken out by Exchange,
            with embedded lists for Expirations and Strikes.
            {'SMART': {'TradingClass': 'SPY', 'Multiplier': 100, 'Expirations': [], 'Strikes': []}}

            - `TradingClass` (str) eg: `FB`
            - `Multiplier` (str) eg: `100`
            - `Expirations` (list of str) eg: [`20230616`, ...]
            - `Strikes` (list of floats) eg: [`100.0`, ...]
        """

        # All Option Contracts | get_chains matching IBKR |
        # {'SMART': {'TradingClass': 'SPY', 'Multiplier': 100, 'Expirations': [], 'Strikes': []}}
        option_contracts = {"SMART": {"TradingClass": None, "Multiplier": None, "Expirations": [], "Strikes": []}}
        contracts = option_contracts["SMART"]  # initialize contracts
        today = self.get_datetime().date()

        # Get expirations from thetadata_helper
        expirations = thetadata_helper.get_expirations(self._username, self._password, asset.symbol, today)

        # Get the first of the expirations and convert to datetime
        expiration = expirations[0].replace("-", "")
        expiration_dt = date(int(expiration[:4]), int(expiration[4:6]), int(expiration[6:8]))

        # Get strikes from thetadata_helper
        strikes = thetadata_helper.get_strikes(self._username, self._password, asset.symbol, expiration_dt)

        # Add the data to the contracts dictionary
        contracts["TradingClass"] = asset.symbol
        contracts["Multiplier"] = 100
        contracts["Expirations"] = expirations
        contracts["Strikes"] = strikes

        # Add the data to the option_contracts dictionary
        option_contracts["SMART"] = contracts

        return option_contracts