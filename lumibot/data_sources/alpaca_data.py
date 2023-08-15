import logging
import re
import time
from datetime import datetime, timedelta, timezone

import alpaca_trade_api as tradeapi
import pandas as pd
from alpaca_trade_api.common import URL
from alpaca_trade_api.entity import Bar
from alpaca_trade_api.rest import TimeFrame, TimeFrameUnit
from lumibot.entities import Asset, Bars

from .data_source import DataSource


class AlpacaData(DataSource):
    SOURCE = "ALPACA"
    MIN_TIMESTEP = "minute"
    TIMESTEP_MAPPING = [
        {
            "timestep": "minute",
            "representations": [TimeFrame(1, TimeFrameUnit.Minute), "minute"],
        },
        {
            "timestep": "day",
            "representations": [TimeFrame(1, TimeFrameUnit.Day), "day"],
        },
    ]

    """Common base class for data_sources/alpaca and brokers/alpaca"""

    @staticmethod
    def _format_datetime(dt):
        return pd.Timestamp(dt).isoformat()

    def __init__(self, config, max_workers=20, chunk_size=100, **kwargs):
        # Alpaca authorize 200 requests per minute and per API key
        # Setting the max_workers for multithreading with a maximum
        # of 200
        self.name = "alpaca"
        self.max_workers = min(max_workers, 200)

        # When requesting data for assets for example,
        # if there is too many assets, the best thing to do would
        # be to split it into chunks and request data for each chunk
        self.chunk_size = min(chunk_size, 100)

        # Connection to alpaca REST API
        self.config = config

        if type(config) == dict and "API_KEY" in config:
            self.api_key = config["API_KEY"]
        elif hasattr(config, "API_KEY"):
            self.api_key = config.API_KEY
        else:
            raise ValueError("API_KEY not found in config")

        if type(config) == dict and "API_SECRET" in config:
            self.api_secret = config["API_SECRET"]
        elif hasattr(config, "API_SECRET"):
            self.api_secret = config.API_SECRET
        else:
            raise ValueError("API_SECRET not found in config")

        if type(config) == dict and "ENDPOINT" in config:
            self.endpoint = config["ENDPOINT"]
        elif hasattr(config, "ENDPOINT"):
            self.endpoint = URL(config.ENDPOINT)
        else:
            self.endpoint = URL("https://paper-api.alpaca.markets")

        if type(config) == dict and "VERSION" in config:
            self.version = config["VERSION"]
        elif hasattr(config, "VERSION"):
            self.version = config.VERSION
        else:
            self.version = "v2"

        self.api = tradeapi.REST(
            self.api_key, self.api_secret, self.endpoint, self.version
        )

    def get_last_price(self, asset, quote=None, exchange=None, **kwargs):
        if quote is not None:
            # If the quote is not None, we use it even if the asset is a tuple
            if type(asset) == Asset and asset.asset_type == "stock":
                symbol = asset.symbol
            elif isinstance(asset, tuple):
                symbol = f"{asset[0].symbol}{quote.symbol}"
            else:
                symbol = f"{asset.symbol}{quote.symbol}"
        elif isinstance(asset, tuple):
            symbol = f"{asset[0].symbol}{asset[1].symbol}"
        else:
            symbol = asset.symbol

        if isinstance(asset, tuple) and asset[0].asset_type == "crypto":
            try:
                trade = self.api.get_latest_crypto_trade(symbol, exchange="CBSE")
            except:
                # Fallback exchange if the crypto trade is not found
                trade = self.api.get_latest_crypto_trade(symbol, exchange="FTXU")
        elif isinstance(asset, Asset) and asset.asset_type == "crypto":
            try:
                trade = self.api.get_latest_crypto_trade(symbol, exchange="CBSE")
            except:
                # Fallback exchange if the crypto trade is not found
                trade = self.api.get_latest_crypto_trade(symbol, exchange="FTXU")
        else:
            trade = self.api.get_latest_trade(symbol)

        return trade.p

    def get_barset_from_api(
        self, api, asset, freq, limit=None, end=None, start=None, quote=None
    ):
        """
        gets historical bar data for the given stock symbol
        and time params.

        outputs a dataframe open, high, low, close columns and
        a UTC timezone aware index.
        """
        if isinstance(asset, tuple):
            if quote is None:
                quote = asset[1]
            asset = asset[0]

        if limit is None:
            limit = 1000

        if end is None:
            end = datetime.now(timezone.utc) - timedelta(minutes=15) # alpaca limitation of not getting the most recent 15 minutes

        if start is None:
            if str(freq) == "1Min":
                if datetime.now().weekday() == 0: # for Mondays as prior days were off
                    loop_limit = limit + 4896 # subtract 4896 minutes to take it from Monday to Friday, as there is no data between Friday 4:00 pm and Monday 9:30 pm causing an incomplete or empty dataframe
                else:
                    loop_limit = limit
        
            elif str(freq) == "1Day":
                loop_limit = limit * 1.5 # number almost perfect for normal weeks where only weekends are off

        end = end.isoformat(timespec="seconds")
        df = [] # to use len(df) below without an error

        while loop_limit / limit <= 64 and len(df) < limit: # arbitrary limit of upto 4 calls after which it will give up
            if str(freq) == "1Min":
                start = datetime.fromisoformat(end) - timedelta(minutes=loop_limit)
                start = start.isoformat(timespec="seconds")

            elif str(freq) == "1Day":
                start = datetime.fromisoformat(end) - timedelta(days=loop_limit)
                start = start.isoformat(timespec="seconds")

            if asset.asset_type == "crypto":
                symbol = f"{asset.symbol}{quote.symbol}"
                barset = api.get_crypto_bars(
                    symbol,
                    freq,
                    start=start, 
                    end=end
                )

            else:
                symbol = asset.symbol
                barset = api.get_bars(
                    symbol,
                    freq,
                    start=start,
                    end=end
                )
            df = barset.df

            if df.empty:
                logging.error(
                    f"Could not get any pricing data from Alpaca for {symbol}, the DataFrame came back empty"
                )
                return None

            df = df[~df.index.duplicated(keep="first")]
            df = df.iloc[-limit:]
            df = df[df.close > 0]
            loop_limit *= 2
        
        if len(df) < limit:
            logging.warning(
                f"Dataframe for {symbol} has {len(df)} rows while {limit} were requested. Further data does not exist for Alpaca"
            )

        return df

    def _pull_source_bars(
        self, assets, length, timestep=MIN_TIMESTEP, timeshift=None, quote=None,  include_after_hours=True
    ):
        """pull broker bars for a list assets"""
        if timeshift is None and timestep == "day":
            # Alpaca throws an error if we don't do this and don't have a data subscription because
            # they require a subscription for historical data less than 15 minutes old
            timeshift = timedelta(minutes=16)

        parsed_timestep = self._parse_source_timestep(timestep, reverse=True)
        kwargs = dict(limit=length)
        if timeshift:
            end = datetime.now() - timeshift
            end = self.to_default_timezone(end)
            kwargs["end"] = end

        result = {}
        for asset in assets:
            data = self.get_barset_from_api(
                self.api, asset, parsed_timestep, quote=quote, **kwargs
            )
            result[asset] = data

        return result

    def _pull_source_symbol_bars(
        self,
        asset,
        length,
        timestep=MIN_TIMESTEP,
        timeshift=None,
        quote=None,
        exchange=None,
        include_after_hours=True
    ):
        if exchange is not None:
            logging.warning(
                f"the exchange parameter is not implemented for AlpacaData, but {exchange} was passed as the exchange"
            )

        """pull broker bars for a given asset"""
        response = self._pull_source_bars(
            [asset], length, timestep=timestep, timeshift=timeshift, quote=quote
        )
        return response[asset]

    def _parse_source_symbol_bars(self, response, asset, quote=None, length=None):
        # TODO: Alpaca return should also include dividend yield
        response["return"] = response["close"].pct_change()
        bars = Bars(response, self.SOURCE, asset, raw=response, quote=quote)
        return bars
