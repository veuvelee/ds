import os
import time
import schedule
from openai import OpenAI
import ccxt
import pandas as pd
import re
from dotenv import load_dotenv
import json
import requests
from datetime import datetime, timedelta
import hmac
import hashlib
import base64
import urllib.parse

load_dotenv()

# 初始化Binance交易所
exchange = ccxt.binance({
    'options': {
        'defaultType': 'future',  # Binance使用future表示永续合约
    },
    'apiKey': os.getenv('BINANCE_API_KEY'),
    'secret': os.getenv('BINANCE_SECRET'),
})

# 交易参数配置 - 针对SOL优化
TRADE_CONFIG = {
    'symbol': 'SOL/USDT:USDT'
}

params = {'status': 'open'}
# orders = exchange.fetch_open_orders(TRADE_CONFIG['symbol'])
orders = exchange.fetch_orders(symbol=TRADE_CONFIG['symbol'], since=None, limit=None, params=params)
# params = {'algoType': 'conditional'}
print(f"orders: {orders}")