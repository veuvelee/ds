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

# binance +带市场情绪+指标版本 + 完整止盈止损逻辑

load_dotenv()

# 初始化DeepSeek客户端
deepseek_client = OpenAI(
    api_key=os.getenv('DEEPSEEK_API_KEY'),
    base_url="https://api.deepseek.com"
)

# 初始化交易所
exchange = ccxt.binance({
    'options': {'defaultType': 'future'},
    'apiKey': os.getenv('BINANCE_API_KEY'),
    'secret': os.getenv('BINANCE_SECRET'),
})

# 交易参数配置 - 优化版本
TRADE_CONFIG = {
    'symbol': 'SOL/USDT',
    'leverage': 10,
    'timeframe': '15m',
    'test_mode': False,
    'data_points': 96,
    'execution_interval': 5,  # 执行间隔分钟数
    
    # 优化的止盈止损参数
    'stop_loss_take_profit': {
        'base_stop_loss_percent': 2.0,  # 基础止损百分比
        'base_take_profit_percent': 4.0,  # 基础止盈百分比
        'high_confidence_sl_multiplier': 0.8,  # 高信心时止损放宽
        'high_confidence_tp_multiplier': 1.2,  # 高信心时止盈提高
        'low_confidence_sl_multiplier': 1.2,  # 低信心时止损收紧
        'low_confidence_tp_multiplier': 0.8,  # 低信心时止盈降低
        'trend_following_sl_buffer': 0.5,  # 趋势跟踪时的止损缓冲
        'volatility_adjusted_sl': True,  # 是否根据波动率调整止损
        'enable_exchange_sl_tp': True,  # 是否在交易所设置止盈止损
    },
    
    'analysis_periods': {
        'short_term': 20,
        'medium_term': 50,
        'long_term': 96
    },
    
    'position_management': {
        'enable_intelligent_position': True,
        'base_usdt_amount': 100,
        'high_confidence_multiplier': 1.5,
        'medium_confidence_multiplier': 1.0,
        'low_confidence_multiplier': 0.5,
        'max_position_ratio': 10,
        'trend_strength_multiplier': 1.2
    }
}

# 全局变量存储历史数据和交易状态
price_history = []
signal_history = []
position = None
last_trade_time = None
trade_stats = {
    'consecutive_same_signals': 0,
    'last_signal': None,
    'position_hold_time': 0,
    'active_orders': []  # 跟踪活跃订单
}

def setup_exchange():
    """设置交易所参数 - 强制全仓模式"""
    try:
        print("🔍 获取SOL合约规格...")
        markets = exchange.load_markets()
        sol_market = markets[TRADE_CONFIG['symbol']]

        contract_size = 1
        print(f"✅ 合约规格: 1张 = {contract_size} SOL")

        TRADE_CONFIG['contract_size'] = contract_size
        TRADE_CONFIG['min_amount'] = sol_market['limits']['amount']['min']
        print(f"📏 最小交易量: {TRADE_CONFIG['min_amount']} 张")

        # 检查现有持仓
        print("🔍 检查现有持仓模式...")
        positions = exchange.fetch_positions([TRADE_CONFIG['symbol']])

        has_isolated_position = False
        isolated_position_info = None

        for pos in positions:
            if pos['symbol'] == TRADE_CONFIG['symbol'] + ':USDT':
                contracts = float(pos.get('contracts', 0))
                mode = pos.get('mgnMode')

                if contracts > 0 and mode == 'isolated':
                    has_isolated_position = True
                    isolated_position_info = {
                        'side': pos.get('side'),
                        'size': contracts,
                        'entry_price': pos.get('entryPrice'),
                        'mode': mode
                    }
                    break

        if has_isolated_position:
            print("❌ 检测到逐仓持仓，程序无法继续运行！")
            print(f"📊 逐仓持仓详情:")
            print(f"   - 方向: {isolated_position_info['side']}")
            print(f"   - 数量: {isolated_position_info['size']}")
            print(f"   - 入场价: {isolated_position_info['entry_price']}")
            print(f"   - 模式: {isolated_position_info['mode']}")
            print("\n🚨 解决方案:")
            print("1. 手动平掉所有逐仓持仓")
            print("2. 或者将逐仓持仓转为全仓模式")
            print("3. 然后重新启动程序")
            return False

        # 设置单向持仓模式
        print("🔄 设置单向持仓模式...")
        try:
            exchange.set_position_mode(False, TRADE_CONFIG['symbol'])
            print("✅ 已设置单向持仓模式")
        except Exception as e:
            print(f"⚠️ 设置单向持仓模式失败 (可能已设置): {e}")

        # 设置全仓模式和杠杆
        print("⚙️ 设置全仓模式和杠杆...")
        exchange.set_leverage(
            TRADE_CONFIG['leverage'],
            TRADE_CONFIG['symbol'],
            {'mgnMode': 'cross'}
        )
        print(f"✅ 已设置全仓模式，杠杆倍数: {TRADE_CONFIG['leverage']}x")

        # 验证设置
        print("🔍 验证账户设置...")
        balance = exchange.fetch_balance()
        usdt_balance = balance['USDT']['free']
        print(f"💰 当前USDT余额: {usdt_balance:.2f}")

        current_pos = get_current_position()
        if current_pos:
            print(f"📦 当前持仓: {current_pos['side']}仓 {current_pos['size']}张")
        else:
            print("📦 当前无持仓")

        print("🎯 程序配置完成：全仓模式 + 单向持仓")
        return True

    except Exception as e:
        print(f"❌ 交易所设置失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def calculate_volatility_adjusted_stop_loss(price_data, base_sl_percent):
    """根据波动率调整止损"""
    try:
        df = price_data['full_data']
        # 计算ATR（平均真实波幅）
        high_low = df['high'] - df['low']
        high_close = abs(df['high'] - df['close'].shift())
        low_close = abs(df['low'] - df['close'].shift())
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = true_range.rolling(14).mean().iloc[-1]
        
        # 波动率调整系数
        current_price = price_data['price']
        volatility_ratio = atr / current_price
        
        # 高波动率时适当放宽止损
        if volatility_ratio > 0.03:  # 3%以上的波动率
            adjustment = 1.2
        elif volatility_ratio < 0.01:  # 1%以下的低波动率
            adjustment = 0.8
        else:
            adjustment = 1.0
            
        adjusted_sl = base_sl_percent * adjustment
        print(f"📊 波动率调整: ATR={atr:.4f}, 比率={volatility_ratio:.3%}, 调整系数={adjustment:.2f}")
        
        return adjusted_sl
    except Exception as e:
        print(f"波动率调整计算失败: {e}")
        return base_sl_percent

def calculate_intelligent_stop_loss_take_profit(signal_data, price_data, current_position):
    """智能计算止盈止损价格"""
    try:
        config = TRADE_CONFIG['stop_loss_take_profit']
        current_price = price_data['price']
        
        # 基础止盈止损百分比
        base_sl_percent = config['base_stop_loss_percent']
        base_tp_percent = config['base_take_profit_percent']
        
        # 根据信心程度调整
        confidence = signal_data.get('confidence', 'MEDIUM')
        sl_multiplier = {
            'HIGH': config['high_confidence_sl_multiplier'],
            'MEDIUM': 1.0,
            'LOW': config['low_confidence_sl_multiplier']
        }.get(confidence, 1.0)
        
        tp_multiplier = {
            'HIGH': config['high_confidence_tp_multiplier'],
            'MEDIUM': 1.0,
            'LOW': config['low_confidence_tp_multiplier']
        }.get(confidence, 1.0)
        
        # 波动率调整
        if config['volatility_adjusted_sl']:
            base_sl_percent = calculate_volatility_adjusted_stop_loss(price_data, base_sl_percent)
        
        # 最终止盈止损百分比
        final_sl_percent = base_sl_percent * sl_multiplier
        final_tp_percent = base_tp_percent * tp_multiplier
        
        # 趋势跟踪缓冲
        trend = price_data['trend_analysis'].get('overall', '震荡整理')
        if trend in ['强势上涨', '强势下跌']:
            final_sl_percent += config['trend_following_sl_buffer']
        
        # 计算具体价格
        if signal_data['signal'] == 'BUY':
            stop_loss_price = current_price * (1 - final_sl_percent / 100)
            take_profit_price = current_price * (1 + final_tp_percent / 100)
        elif signal_data['signal'] == 'SELL':
            stop_loss_price = current_price * (1 + final_sl_percent / 100)
            take_profit_price = current_price * (1 - final_tp_percent / 100)
        else:
            # HOLD信号使用保守值
            stop_loss_price = current_price * 0.98
            take_profit_price = current_price * 1.02
        
        print(f"🎯 止盈止损计算:")
        print(f"   - 基础止损: {base_sl_percent:.2f}%, 止盈: {base_tp_percent:.2f}%")
        print(f"   - 信心调整: SL×{sl_multiplier:.2f}, TP×{tp_multiplier:.2f}")
        print(f"   - 最终止损: {final_sl_percent:.2f}%, 止盈: {final_tp_percent:.2f}%")
        print(f"   - 具体价格: 止损=${stop_loss_price:.2f}, 止盈=${take_profit_price:.2f}")
        
        return stop_loss_price, take_profit_price
        
    except Exception as e:
        print(f"❌ 止盈止损计算失败: {e}")
        # 备用计算
        current_price = price_data['price']
        if signal_data['signal'] == 'BUY':
            return current_price * 0.98, current_price * 1.04
        elif signal_data['signal'] == 'SELL':
            return current_price * 1.02, current_price * 0.96
        else:
            return current_price * 0.98, current_price * 1.02

def set_exchange_stop_loss_take_profit(signal_data, position_size, current_position):
    """在交易所设置止盈止损订单"""
    try:
        if not TRADE_CONFIG['stop_loss_take_profit']['enable_exchange_sl_tp']:
            print("🔧 交易所止盈止损功能已禁用")
            return True
            
        if TRADE_CONFIG['test_mode']:
            print("🔧 测试模式 - 模拟设置止盈止损")
            return True
            
        symbol = TRADE_CONFIG['symbol']
        stop_loss_price = signal_data['stop_loss']
        take_profit_price = signal_data['take_profit']
        
        # 首先取消所有现有的止盈止损订单
        print("🔄 取消现有止盈止损订单...")
        try:
            open_orders = exchange.fetch_open_orders(symbol)
            for order in open_orders:
                if order['type'] in ['stop_market', 'take_profit_market']:
                    exchange.cancel_order(order['id'], symbol)
                    print(f"   - 已取消订单: {order['type']} #{order['id']}")
                    time.sleep(0.1)
        except Exception as e:
            print(f"⚠️ 取消现有订单时出错: {e}")
        
        time.sleep(1)
        
        # 设置止损订单
        print("🛡️ 设置止损订单...")
        if signal_data['signal'] == 'BUY':
            # 多头持仓：止损价低于当前价
            sl_order = exchange.create_order(
                symbol=symbol,
                type='stop_market',
                side='sell',
                amount=position_size,
                price=None,
                params={
                    'stopPrice': stop_loss_price,
                    'reduceOnly': True,
                    'workingType': 'MARK_PRICE'
                }
            )
            print(f"   ✅ 止损订单设置成功: {stop_loss_price:.2f}")
            
        elif signal_data['signal'] == 'SELL':
            # 空头持仓：止损价高于当前价
            sl_order = exchange.create_order(
                symbol=symbol,
                type='stop_market',
                side='buy',
                amount=position_size,
                price=None,
                params={
                    'stopPrice': stop_loss_price,
                    'reduceOnly': True,
                    'workingType': 'MARK_PRICE'
                }
            )
            print(f"   ✅ 止损订单设置成功: {stop_loss_price:.2f}")
        
        time.sleep(0.5)
        
        # 设置止盈订单
        print("🎯 设置止盈订单...")
        if signal_data['signal'] == 'BUY':
            # 多头持仓：止盈价高于当前价
            tp_order = exchange.create_order(
                symbol=symbol,
                type='take_profit_market',
                side='sell',
                amount=position_size,
                price=None,
                params={
                    'stopPrice': take_profit_price,
                    'reduceOnly': True,
                    'workingType': 'MARK_PRICE'
                }
            )
            print(f"   ✅ 止盈订单设置成功: {take_profit_price:.2f}")
            
        elif signal_data['signal'] == 'SELL':
            # 空头持仓：止盈价低于当前价
            tp_order = exchange.create_order(
                symbol=symbol,
                type='take_profit_market',
                side='buy',
                amount=position_size,
                price=None,
                params={
                    'stopPrice': take_profit_price,
                    'reduceOnly': True,
                    'workingType': 'MARK_PRICE'
                }
            )
            print(f"   ✅ 止盈订单设置成功: {take_profit_price:.2f}")
        
        # 更新活跃订单记录
        global trade_stats
        trade_stats['active_orders'] = [
            {'type': 'stop_loss', 'price': stop_loss_price},
            {'type': 'take_profit', 'price': take_profit_price}
        ]
        
        print("🎉 止盈止损订单设置完成")
        return True
        
    except Exception as e:
        print(f"❌ 设置止盈止损订单失败: {e}")
        return False

def cancel_existing_sl_tp_orders():
    """取消所有现有的止盈止损订单"""
    try:
        symbol = TRADE_CONFIG['symbol']
        open_orders = exchange.fetch_open_orders(symbol)
        
        cancelled_count = 0
        for order in open_orders:
            if order['type'] in ['stop_market', 'take_profit_market']:
                exchange.cancel_order(order['id'], symbol)
                print(f"   - 已取消订单: {order['type']} #{order['id']}")
                cancelled_count += 1
                time.sleep(0.1)
        
        if cancelled_count > 0:
            print(f"✅ 已取消 {cancelled_count} 个止盈止损订单")
        else:
            print("ℹ️ 没有找到需要取消的止盈止损订单")
            
        # 清空活跃订单记录
        trade_stats['active_orders'] = []
        
        return True
    except Exception as e:
        print(f"❌ 取消止盈止损订单失败: {e}")
        return False

def calculate_intelligent_position(signal_data, price_data, current_position):
    """计算智能仓位大小 - 优化版"""
    config = TRADE_CONFIG['position_management']

    if not config.get('enable_intelligent_position', True):
        fixed_contracts = 0.1
        print(f"🔧 智能仓位已禁用，使用固定仓位: {fixed_contracts} 张")
        return fixed_contracts

    try:
        balance = exchange.fetch_balance()
        usdt_balance = balance['USDT']['free']
        base_usdt = config['base_usdt_amount']
        print(f"💰 可用USDT余额: {usdt_balance:.2f}, 下单基数{base_usdt}")

        confidence_multiplier = {
            'HIGH': config['high_confidence_multiplier'],
            'MEDIUM': config['medium_confidence_multiplier'],
            'LOW': config['low_confidence_multiplier']
        }.get(signal_data['confidence'], 1.0)

        trend = price_data['trend_analysis'].get('overall', '震荡整理')
        if trend in ['强势上涨', '强势下跌']:
            trend_multiplier = config['trend_strength_multiplier']
        else:
            trend_multiplier = 1.0

        rsi = price_data['technical_data'].get('rsi', 50)
        if rsi > 75 or rsi < 25:
            rsi_multiplier = 0.7
        else:
            rsi_multiplier = 1.0

        suggested_usdt = base_usdt * confidence_multiplier * trend_multiplier * rsi_multiplier
        max_usdt = usdt_balance * (config['max_position_ratio'] / 100)
        final_usdt = min(suggested_usdt, max_usdt)

        contract_size = max(final_usdt / (price_data['price'] * TRADE_CONFIG['contract_size']), 1)

        print(f"📊 仓位计算详情:")
        print(f"   - 基础USDT: {base_usdt}")
        print(f"   - 信心倍数: {confidence_multiplier}")
        print(f"   - 趋势倍数: {trend_multiplier}")
        print(f"   - RSI倍数: {rsi_multiplier}")
        print(f"   - 建议USDT: {suggested_usdt:.2f}")
        print(f"   - 最终USDT: {final_usdt:.2f}")
        print(f"   - 合约乘数: {TRADE_CONFIG['contract_size']}")
        print(f"   - 计算合约: {contract_size:.4f} 张")

        contract_size = round(contract_size, 0)
        min_contracts = TRADE_CONFIG.get('min_amount', 1)
        if contract_size < min_contracts:
            contract_size = min_contracts
            print(f"⚠️ 仓位小于最小值，调整为: {contract_size} 张")

        print(f"🎯 最终仓位: {final_usdt:.2f} USDT → {contract_size:.2f} 张合约")
        return contract_size

    except Exception as e:
        print(f"❌ 仓位计算失败，使用基础仓位: {e}")
        base_usdt = config['base_usdt_amount']
        contract_size = (base_usdt * TRADE_CONFIG['leverage']) / (price_data['price'] * TRADE_CONFIG.get('contract_size', 1))
        return round(max(contract_size, TRADE_CONFIG.get('min_amount', 1)), 0)

def calculate_technical_indicators(df):
    """计算技术指标"""
    try:
        # 移动平均线
        df['sma_5'] = df['close'].rolling(window=5, min_periods=1).mean()
        df['sma_20'] = df['close'].rolling(window=20, min_periods=1).mean()
        df['sma_50'] = df['close'].rolling(window=50, min_periods=1).mean()

        # 指数移动平均线
        df['ema_12'] = df['close'].ewm(span=12).mean()
        df['ema_26'] = df['close'].ewm(span=26).mean()
        df['macd'] = df['ema_12'] - df['ema_26']
        df['macd_signal'] = df['macd'].ewm(span=9).mean()
        df['macd_histogram'] = df['macd'] - df['macd_signal']

        # 相对强弱指数 (RSI)
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))

        # 布林带
        df['bb_middle'] = df['close'].rolling(20).mean()
        bb_std = df['close'].rolling(20).std()
        df['bb_upper'] = df['bb_middle'] + (bb_std * 2)
        df['bb_lower'] = df['bb_middle'] - (bb_std * 2)
        df['bb_position'] = (df['close'] - df['bb_lower']) / (df['bb_upper'] - df['bb_lower'])

        # 成交量均线
        df['volume_ma'] = df['volume'].rolling(20).mean()
        df['volume_ratio'] = df['volume'] / df['volume_ma']

        # 支撑阻力位
        df['resistance'] = df['high'].rolling(20).max()
        df['support'] = df['low'].rolling(20).min()

        # 填充NaN值
        df = df.bfill().ffill()

        return df
    except Exception as e:
        print(f"技术指标计算失败: {e}")
        return df

def get_support_resistance_levels(df, lookback=20):
    """计算支撑阻力位"""
    try:
        recent_high = df['high'].tail(lookback).max()
        recent_low = df['low'].tail(lookback).min()
        current_price = df['close'].iloc[-1]

        resistance_level = recent_high
        support_level = recent_low

        bb_upper = df['bb_upper'].iloc[-1]
        bb_lower = df['bb_lower'].iloc[-1]

        return {
            'static_resistance': resistance_level,
            'static_support': support_level,
            'dynamic_resistance': bb_upper,
            'dynamic_support': bb_lower,
            'price_vs_resistance': ((resistance_level - current_price) / current_price) * 100,
            'price_vs_support': ((current_price - support_level) / support_level) * 100
        }
    except Exception as e:
        print(f"支撑阻力计算失败: {e}")
        return {}

def get_sentiment_indicators():
    """获取情绪指标 - 简洁版本"""
    try:
        API_URL = "https://service.cryptoracle.network/openapi/v2/endpoint"
        API_KEY = "7ad48a56-8730-4238-a714-eebc30834e3e"

        end_time = datetime.now()
        start_time = end_time - timedelta(hours=4)

        request_body = {
            "apiKey": API_KEY,
            "endpoints": ["CO-A-02-01", "CO-A-02-02"],
            "startTime": start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "endTime": end_time.strftime("%Y-%m-%d %H:%M:%S"),
            "timeType": "15m",
            "token": ["SOL"]
        }

        headers = {"Content-Type": "application/json", "X-API-KEY": API_KEY}
        response = requests.post(API_URL, json=request_body, headers=headers)

        if response.status_code == 200:
            data = response.json()
            if data.get("code") == 200 and data.get("data"):
                time_periods = data["data"][0]["timePeriods"]

                for period in time_periods:
                    period_data = period.get("data", [])

                    sentiment = {}
                    valid_data_found = False

                    for item in period_data:
                        endpoint = item.get("endpoint")
                        value = item.get("value", "").strip()

                        if value:
                            try:
                                if endpoint in ["CO-A-02-01", "CO-A-02-02"]:
                                    sentiment[endpoint] = float(value)
                                    valid_data_found = True
                            except (ValueError, TypeError):
                                continue

                    if valid_data_found and "CO-A-02-01" in sentiment and "CO-A-02-02" in sentiment:
                        positive = sentiment['CO-A-02-01']
                        negative = sentiment['CO-A-02-02']
                        net_sentiment = positive - negative

                        data_delay = int((datetime.now() - datetime.strptime(
                            period['startTime'], '%Y-%m-%d %H:%M:%S')).total_seconds() // 60)

                        print(f"✅ 使用情绪数据时间: {period['startTime']} (延迟: {data_delay}分钟)")

                        return {
                            'positive_ratio': positive,
                            'negative_ratio': negative,
                            'net_sentiment': net_sentiment,
                            'data_time': period['startTime'],
                            'data_delay_minutes': data_delay
                        }

                print("❌ 所有时间段数据都为空")
                return None

        return None
    except Exception as e:
        print(f"情绪指标获取失败: {e}")
        return None

def get_market_trend(df):
    """判断市场趋势"""
    try:
        current_price = df['close'].iloc[-1]

        trend_short = "上涨" if current_price > df['sma_20'].iloc[-1] else "下跌"
        trend_medium = "上涨" if current_price > df['sma_50'].iloc[-1] else "下跌"

        macd_trend = "bullish" if df['macd'].iloc[-1] > df['macd_signal'].iloc[-1] else "bearish"

        if trend_short == "上涨" and trend_medium == "上涨":
            overall_trend = "强势上涨"
        elif trend_short == "下跌" and trend_medium == "下跌":
            overall_trend = "强势下跌"
        else:
            overall_trend = "震荡整理"

        return {
            'short_term': trend_short,
            'medium_term': trend_medium,
            'macd': macd_trend,
            'overall': overall_trend,
            'rsi_level': df['rsi'].iloc[-1]
        }
    except Exception as e:
        print(f"趋势分析失败: {e}")
        return {}

def get_sol_ohlcv_enhanced():
    """增强版：获取SOL K线数据并计算技术指标"""
    try:
        ohlcv = exchange.fetch_ohlcv(TRADE_CONFIG['symbol'], TRADE_CONFIG['timeframe'],
                                     limit=TRADE_CONFIG['data_points'])

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

        df = calculate_technical_indicators(df)

        current_data = df.iloc[-1]
        previous_data = df.iloc[-2]

        trend_analysis = get_market_trend(df)
        levels_analysis = get_support_resistance_levels(df)

        return {
            'price': current_data['close'],
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'high': current_data['high'],
            'low': current_data['low'],
            'volume': current_data['volume'],
            'timeframe': TRADE_CONFIG['timeframe'],
            'price_change': ((current_data['close'] - previous_data['close']) / previous_data['close']) * 100,
            'kline_data': df[['timestamp', 'open', 'high', 'low', 'close', 'volume']].tail(10).to_dict('records'),
            'technical_data': {
                'sma_5': current_data.get('sma_5', 0),
                'sma_20': current_data.get('sma_20', 0),
                'sma_50': current_data.get('sma_50', 0),
                'rsi': current_data.get('rsi', 0),
                'macd': current_data.get('macd', 0),
                'macd_signal': current_data.get('macd_signal', 0),
                'macd_histogram': current_data.get('macd_histogram', 0),
                'bb_upper': current_data.get('bb_upper', 0),
                'bb_lower': current_data.get('bb_lower', 0),
                'bb_position': current_data.get('bb_position', 0),
                'volume_ratio': current_data.get('volume_ratio', 0)
            },
            'trend_analysis': trend_analysis,
            'levels_analysis': levels_analysis,
            'full_data': df
        }
    except Exception as e:
        print(f"获取增强K线数据失败: {e}")
        return None

def generate_technical_analysis_text(price_data):
    """生成技术分析文本"""
    if 'technical_data' not in price_data:
        return "技术指标数据不可用"

    tech = price_data['technical_data']
    trend = price_data.get('trend_analysis', {})
    levels = price_data.get('levels_analysis', {})

    def safe_float(value, default=0):
        return float(value) if value and pd.notna(value) else default

    analysis_text = f"""
    【技术指标分析】
    📈 移动平均线:
    - 5周期: {safe_float(tech['sma_5']):.2f} | 价格相对: {(price_data['price'] - safe_float(tech['sma_5'])) / safe_float(tech['sma_5']) * 100:+.2f}%
    - 20周期: {safe_float(tech['sma_20']):.2f} | 价格相对: {(price_data['price'] - safe_float(tech['sma_20'])) / safe_float(tech['sma_20']) * 100:+.2f}%
    - 50周期: {safe_float(tech['sma_50']):.2f} | 价格相对: {(price_data['price'] - safe_float(tech['sma_50'])) / safe_float(tech['sma_50']) * 100:+.2f}%

    🎯 趋势分析:
    - 短期趋势: {trend.get('short_term', 'N/A')}
    - 中期趋势: {trend.get('medium_term', 'N/A')}
    - 整体趋势: {trend.get('overall', 'N/A')}
    - MACD方向: {trend.get('macd', 'N/A')}

    📊 动量指标:
    - RSI: {safe_float(tech['rsi']):.2f} ({'超买' if safe_float(tech['rsi']) > 70 else '超卖' if safe_float(tech['rsi']) < 30 else '中性'})
    - MACD: {safe_float(tech['macd']):.4f}
    - 信号线: {safe_float(tech['macd_signal']):.4f}

    🎚️ 布林带位置: {safe_float(tech['bb_position']):.2%} ({'上部' if safe_float(tech['bb_position']) > 0.7 else '下部' if safe_float(tech['bb_position']) < 0.3 else '中部'})

    💰 关键水平:
    - 静态阻力: {safe_float(levels.get('static_resistance', 0)):.2f}
    - 静态支撑: {safe_float(levels.get('static_support', 0)):.2f}
    """
    return analysis_text

def get_current_position():
    """获取当前持仓情况 - BINANCE版本"""
    try:
        positions = exchange.fetch_positions([TRADE_CONFIG['symbol']])

        for pos in positions:
            if pos['symbol'] == TRADE_CONFIG['symbol'] + ':USDT':
                contracts = float(pos['contracts']) if pos['contracts'] else 0

                if contracts > 0:
                    return {
                        'side': pos['side'],
                        'size': contracts,
                        'entry_price': float(pos['entryPrice']) if pos['entryPrice'] else 0,
                        'unrealized_pnl': float(pos['unrealizedPnl']) if pos['unrealizedPnl'] else 0,
                        'leverage': float(pos['leverage']) if pos['leverage'] else TRADE_CONFIG['leverage'],
                        'symbol': pos['symbol']
                    }

        return None

    except Exception as e:
        print(f"获取持仓失败: {e}")
        import traceback
        traceback.print_exc()
        return None

def safe_json_parse(json_str):
    """安全解析JSON，处理格式不规范的情况"""
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        try:
            json_str = json_str.replace("'", '"')
            json_str = re.sub(r'(\w+):', r'"\1":', json_str)
            json_str = re.sub(r',\s*}', '}', json_str)
            json_str = re.sub(r',\s*]', ']', json_str)
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            print(f"JSON解析失败，原始内容: {json_str}")
            print(f"错误详情: {e}")
            return None

def create_fallback_signal(price_data):
    """创建备用交易信号"""
    return {
        "signal": "HOLD",
        "reason": "因技术分析暂时不可用，采取保守策略",
        "stop_loss": price_data['price'] * 0.98,
        "take_profit": price_data['price'] * 1.02,
        "confidence": "LOW",
        "is_fallback": True
    }

def should_reverse_position(current_position, signal_data, price_data):
    """判断是否应该反转持仓"""
    if not current_position:
        return True
        
    current_side = current_position['side']
    signal_side = 'long' if signal_data['signal'] == 'BUY' else 'short' if signal_data['signal'] == 'SELL' else None
    
    if current_side == signal_side:
        return False  # 同方向，不需要反转
        
    # 检查持仓时间
    global trade_stats
    if trade_stats['position_hold_time'] < 2:  # 持仓时间少于2个周期
        print(f"🔒 持仓时间较短({trade_stats['position_hold_time']}周期)，暂不反转")
        return False
        
    # 检查信号强度
    if signal_data['confidence'] != 'HIGH':
        print("🔒 非高信心反转信号，保持现有持仓")
        return False
        
    # 检查技术指标确认
    tech = price_data['technical_data']
    trend = price_data['trend_analysis']
    
    # 需要多个指标确认反转
    confirmation_count = 0
    
    # RSI极端值确认
    if (signal_side == 'long' and tech['rsi'] < 30) or (signal_side == 'short' and tech['rsi'] > 70):
        confirmation_count += 1
        print("✅ RSI确认反转信号")
        
    # 趋势确认
    if trend['overall'] in ['强势上涨', '强势下跌']:
        confirmation_count += 1
        print("✅ 趋势确认反转信号")
        
    # MACD确认
    macd_histogram = tech['macd_histogram']
    if (signal_side == 'long' and macd_histogram > 0) or (signal_side == 'short' and macd_histogram < 0):
        confirmation_count += 1
        print("✅ MACD确认反转信号")
        
    # 需要至少2个确认信号才执行反转
    if confirmation_count >= 2:
        print(f"🎯 反转条件满足({confirmation_count}/3)，执行反转")
        return True
    else:
        print(f"🔒 反转条件不足({confirmation_count}/3)，保持现有持仓")
        return False

def analyze_with_deepseek(price_data):
    """使用DeepSeek分析市场并生成交易信号（优化版）"""

    technical_analysis = generate_technical_analysis_text(price_data)

    kline_text = f"【最近5根{TRADE_CONFIG['timeframe']}K线数据】\n"
    for i, kline in enumerate(price_data['kline_data'][-5:]):
        trend = "阳线" if kline['close'] > kline['open'] else "阴线"
        change = ((kline['close'] - kline['open']) / kline['open']) * 100
        kline_text += f"K线{i + 1}: {trend} 开盘:{kline['open']:.2f} 收盘:{kline['close']:.2f} 涨跌:{change:+.2f}%\n"

    signal_text = ""
    if signal_history:
        last_signal = signal_history[-1]
        signal_text = f"\n【上次交易信号】\n信号: {last_signal.get('signal', 'N/A')}\n信心: {last_signal.get('confidence', 'N/A')}"

    sentiment_data = get_sentiment_indicators()
    if sentiment_data:
        sign = '+' if sentiment_data['net_sentiment'] >= 0 else ''
        sentiment_text = f"【市场情绪】乐观{sentiment_data['positive_ratio']:.1%} 悲观{sentiment_data['negative_ratio']:.1%} 净值{sign}{sentiment_data['net_sentiment']:.3f}"
    else:
        sentiment_text = "【市场情绪】数据暂不可用"

    current_pos = get_current_position()
    position_text = "无持仓" if not current_pos else f"{current_pos['side']}仓, 数量: {current_pos['size']}, 盈亏: {current_pos['unrealized_pnl']:.2f}USDT"
    pnl_text = f", 持仓盈亏: {current_pos['unrealized_pnl']:.2f} USDT" if current_pos else ""

    # 优化的提示词
    prompt = f"""
    你是一个专业的加密货币交易分析师。请基于以下SOL/USDT {TRADE_CONFIG['timeframe']}周期数据进行分析：

    {kline_text}

    {technical_analysis}

    {signal_text}

    {sentiment_text}

    【当前行情】
    - 当前价格: ${price_data['price']:,.2f}
    - 时间: {price_data['timestamp']}
    - 本K线最高: ${price_data['high']:,.2f}
    - 本K线最低: ${price_data['low']:,.2f}
    - 本K线成交量: {price_data['volume']:.2f} SOL
    - 价格变化: {price_data['price_change']:+.2f}%
    - 当前持仓: {position_text}{pnl_text}

    【核心交易原则 - 必须严格遵守】
    1. **趋势持续性优先**: 不要因单根K线或短期波动改变整体趋势判断
    2. **持仓稳定性**: 除非趋势明确强烈反转，否则保持现有持仓方向
    3. **反转确认**: 需要至少2-3个技术指标同时确认趋势反转才改变信号
    4. **预测性思维**: 基于技术形态预测未来1-3个周期的价格走势，而不是仅看当前价格

    【智能止盈止损策略】
    1. **趋势跟踪止盈**: 在强势趋势中，可以适当提高止盈目标
    2. **动态止损调整**: 根据波动率和市场状况调整止损位置
    3. **风险回报比**: 确保止盈/止损比例至少为2:1

    【技术分析权重分配】
    1. **主要指标** (权重70%): 
       - 趋势分析(均线排列、MACD趋势)
       - 支撑阻力位突破
       - K线形态组合
    2. **辅助指标** (权重20%):
       - RSI超买超卖
       - 布林带位置
       - 成交量确认
    3. **情绪指标** (权重10%):
       - 仅作为验证信号使用

    【持仓管理逻辑】
    - 现有持仓且趋势延续 → 保持或同方向加仓信号
    - 趋势明确反转且多重确认 → 及时反向信号  
    - 窄幅震荡无方向 → HOLD信号
    - 避免因小幅波动频繁反转持仓

    【信号生成规则】
    BUY信号条件(满足3条以上):
    ✓ 价格突破关键阻力位 + 成交量放大
    ✓ 均线呈多头排列(5>20>50)
    ✓ MACD金叉且柱状图转正
    ✓ RSI从超卖区域回升

    SELL信号条件(满足3条以上):
    ✓ 价格跌破关键支撑位 + 成交量放大  
    ✓ 均线呈空头排列(5<20<50)
    ✓ MACD死叉且柱状图转负
    ✓ RSI从超买区域回落

    HOLD信号条件:
    ✓ 技术指标矛盾无明确方向
    ✓ 价格在窄幅区间震荡
    ✓ 需要更多确认信号

    【重要】基于技术分析做出明确判断，要有预测性思维，避免过度谨慎！

    请用以下JSON格式回复：
    {{
        "signal": "BUY|SELL|HOLD",
        "reason": "详细分析理由(包含趋势判断、技术依据和预测逻辑)",
        "stop_loss": 具体价格,
        "take_profit": 具体价格, 
        "confidence": "HIGH|MEDIUM|LOW"
    }}
    """

    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system",
                 "content": f"您是一位专业的交易员，专注于{TRADE_CONFIG['timeframe']}周期趋势分析和价格预测。请结合技术形态预测未来走势，并严格遵循JSON格式要求。"},
                {"role": "user", "content": prompt}
            ],
            stream=False,
            temperature=0.1
        )

        result = response.choices[0].message.content
        print(f"DeepSeek原始回复: {result}")

        start_idx = result.find('{')
        end_idx = result.rfind('}') + 1

        if start_idx != -1 and end_idx != 0:
            json_str = result[start_idx:end_idx]
            signal_data = safe_json_parse(json_str)

            if signal_data is None:
                signal_data = create_fallback_signal(price_data)
        else:
            signal_data = create_fallback_signal(price_data)

        required_fields = ['signal', 'reason', 'stop_loss', 'take_profit', 'confidence']
        if not all(field in signal_data for field in required_fields):
            signal_data = create_fallback_signal(price_data)

        # 更新交易统计
        global trade_stats
        if signal_history:
            last_signal = signal_history[-1]['signal']
            if signal_data['signal'] == last_signal:
                trade_stats['consecutive_same_signals'] += 1
            else:
                trade_stats['consecutive_same_signals'] = 0
        else:
            trade_stats['consecutive_same_signals'] = 1
            
        trade_stats['last_signal'] = signal_data['signal']

        signal_data['timestamp'] = price_data['timestamp']
        signal_history.append(signal_data)
        if len(signal_history) > 30:
            signal_history.pop(0)

        signal_count = len([s for s in signal_history if s.get('signal') == signal_data['signal']])
        total_signals = len(signal_history)
        print(f"信号统计: {signal_data['signal']} (最近{total_signals}次中出现{signal_count}次)")

        if len(signal_history) >= 3:
            last_three = [s['signal'] for s in signal_history[-3:]]
            if len(set(last_three)) == 1:
                print(f"⚠️ 注意：连续3次{signal_data['signal']}信号")

        return signal_data

    except Exception as e:
        print(f"DeepSeek分析失败: {e}")
        return create_fallback_signal(price_data)

def execute_intelligent_trade(signal_data, price_data):
    """执行智能交易 - 完整止盈止损逻辑"""
    global position, trade_stats

    current_position = get_current_position()

    # 使用智能反转判断
    if current_position and signal_data['signal'] != 'HOLD':
        current_side = current_position['side']
        signal_side = 'long' if signal_data['signal'] == 'BUY' else 'short' if signal_data['signal'] == 'SELL' else None
        
        if current_side != signal_side:
            if not should_reverse_position(current_position, signal_data, price_data):
                print(f"🔒 反转条件不满足，保持现有{current_side}仓")
                return

    # 智能计算止盈止损
    stop_loss, take_profit = calculate_intelligent_stop_loss_take_profit(
        signal_data, price_data, current_position
    )
    signal_data['stop_loss'] = stop_loss
    signal_data['take_profit'] = take_profit

    position_size = calculate_intelligent_position(signal_data, price_data, current_position)

    print(f"交易信号: {signal_data['signal']}")
    print(f"信心程度: {signal_data['confidence']}")
    print(f"智能仓位: {position_size:.2f} 张")
    print(f"止损价格: ${stop_loss:.2f}")
    print(f"止盈价格: ${take_profit:.2f}")
    print(f"理由: {signal_data['reason']}")
    print(f"当前持仓: {current_position}")

    if signal_data['confidence'] == 'LOW' and not TRADE_CONFIG['test_mode']:
        print("⚠️ 低信心信号，跳过执行")
        return

    if TRADE_CONFIG['test_mode']:
        print("测试模式 - 仅模拟交易")
        return

    try:
        # 先取消所有现有的止盈止损订单
        cancel_existing_sl_tp_orders()
        
        if signal_data['signal'] == 'BUY':
            if current_position and current_position['side'] == 'short':
                if current_position['size'] > 0:
                    print(f"平空仓 {current_position['size']:.2f} 张并开多仓 {position_size:.2f} 张...")
                    exchange.create_market_order(
                        TRADE_CONFIG['symbol'],
                        'buy',
                        current_position['size'],
                        params={'reduceOnly': True, 'tag': '60bb4a8d3416BCDE'}
                    )
                    time.sleep(1)
                    exchange.create_market_order(
                        TRADE_CONFIG['symbol'],
                        'buy',
                        position_size,
                        params={'tag': '60bb4a8d3416BCDE'}
                    )
                    trade_stats['position_hold_time'] = 0  # 重置持仓时间
                else:
                    print("⚠️ 检测到空头持仓但数量为0，直接开多仓")
                    exchange.create_market_order(
                        TRADE_CONFIG['symbol'],
                        'buy',
                        position_size,
                        params={'tag': '60bb4a8d3416BCDE'}
                    )
                    trade_stats['position_hold_time'] = 0

            elif current_position and current_position['side'] == 'long':
                size_diff = position_size - current_position['size']
                if abs(size_diff) >= 0.01:
                    if size_diff > 0:
                        add_size = round(size_diff, 2)
                        print(f"多仓加仓 {add_size:.2f} 张 (当前:{current_position['size']:.2f} → 目标:{position_size:.2f})")
                        exchange.create_market_order(
                            TRADE_CONFIG['symbol'],
                            'buy',
                            add_size,
                            params={'tag': '60bb4a8d3416BCDE'}
                        )
                    else:
                        reduce_size = round(abs(size_diff), 2)
                        print(f"多仓减仓 {reduce_size:.2f} 张 (当前:{current_position['size']:.2f} → 目标:{position_size:.2f})")
                        exchange.create_market_order(
                            TRADE_CONFIG['symbol'],
                            'sell',
                            reduce_size,
                            params={'reduceOnly': True, 'tag': '60bb4a8d3416BCDE'}
                        )
                else:
                    print(f"已有多头持仓，仓位合适保持现状 (当前:{current_position['size']:.2f}, 目标:{position_size:.2f})")
                    trade_stats['position_hold_time'] += 1  # 增加持仓时间
            else:
                print(f"开多仓 {position_size:.2f} 张...")
                exchange.create_market_order(
                    TRADE_CONFIG['symbol'],
                    'buy',
                    position_size,
                    params={'tag': '60bb4a8d3416BCDE'}
                )
                trade_stats['position_hold_time'] = 0

            # 设置止盈止损订单
            if signal_data['signal'] == 'BUY':
                set_exchange_stop_loss_take_profit(signal_data, position_size, current_position)

        elif signal_data['signal'] == 'SELL':
            if current_position and current_position['side'] == 'long':
                if current_position['size'] > 0:
                    print(f"平多仓 {current_position['size']:.2f} 张并开空仓 {position_size:.2f} 张...")
                    exchange.create_market_order(
                        TRADE_CONFIG['symbol'],
                        'sell',
                        current_position['size'],
                        params={'reduceOnly': True, 'tag': '60bb4a8d3416BCDE'}
                    )
                    time.sleep(1)
                    exchange.create_market_order(
                        TRADE_CONFIG['symbol'],
                        'sell',
                        position_size,
                        params={'tag': '60bb4a8d3416BCDE'}
                    )
                    trade_stats['position_hold_time'] = 0
                else:
                    print("⚠️ 检测到多头持仓但数量为0，直接开空仓")
                    exchange.create_market_order(
                        TRADE_CONFIG['symbol'],
                        'sell',
                        position_size,
                        params={'tag': '60bb4a8d3416BCDE'}
                    )
                    trade_stats['position_hold_time'] = 0

            elif current_position and current_position['side'] == 'short':
                size_diff = position_size - current_position['size']
                if abs(size_diff) >= 0.01:
                    if size_diff > 0:
                        add_size = round(size_diff, 2)
                        print(f"空仓加仓 {add_size:.2f} 张 (当前:{current_position['size']:.2f} → 目标:{position_size:.2f})")
                        exchange.create_market_order(
                            TRADE_CONFIG['symbol'],
                            'sell',
                            add_size,
                            params={'tag': '60bb4a8d3416BCDE'}
                        )
                    else:
                        reduce_size = round(abs(size_diff), 2)
                        print(f"空仓减仓 {reduce_size:.2f} 张 (当前:{current_position['size']:.2f} → 目标:{position_size:.2f})")
                        exchange.create_market_order(
                            TRADE_CONFIG['symbol'],
                            'buy',
                            reduce_size,
                            params={'reduceOnly': True, 'tag': '60bb4a8d3416BCDE'}
                        )
                else:
                    print(f"已有空头持仓，仓位合适保持现状 (当前:{current_position['size']:.2f}, 目标:{position_size:.2f})")
                    trade_stats['position_hold_time'] += 1
            else:
                print(f"开空仓 {position_size:.2f} 张...")
                exchange.create_market_order(
                    TRADE_CONFIG['symbol'],
                    'sell',
                    position_size,
                    params={'tag': '60bb4a8d3416BCDE'}
                )
                trade_stats['position_hold_time'] = 0

            # 设置止盈止损订单
            if signal_data['signal'] == 'SELL':
                set_exchange_stop_loss_take_profit(signal_data, position_size, current_position)

        elif signal_data['signal'] == 'HOLD':
            print("建议观望，不执行交易")
            if current_position:
                trade_stats['position_hold_time'] += 1  # 持仓时间增加
            # HOLD时也检查是否需要更新止盈止损
            if current_position and trade_stats['active_orders']:
                print("🔍 检查现有止盈止损订单是否需要更新...")
                # 这里可以添加逻辑来检查是否需要调整现有的止盈止损
            return

        print("智能交易执行成功")
        time.sleep(2)
        position = get_current_position()
        print(f"更新后持仓: {position}")

    except Exception as e:
        print(f"交易执行失败: {e}")

        if "don't have any positions" in str(e):
            print("尝试直接开新仓...")
            try:
                if signal_data['signal'] == 'BUY':
                    exchange.create_market_order(
                        TRADE_CONFIG['symbol'],
                        'buy',
                        position_size,
                        params={'tag': '60bb4a8d3416BCDE'}
                    )
                    trade_stats['position_hold_time'] = 0
                    # 设置止盈止损
                    set_exchange_stop_loss_take_profit(signal_data, position_size, None)
                elif signal_data['signal'] == 'SELL':
                    exchange.create_market_order(
                        TRADE_CONFIG['symbol'],
                        'sell',
                        position_size,
                        params={'tag': '60bb4a8d3416BCDE'}
                    )
                    trade_stats['position_hold_time'] = 0
                    # 设置止盈止损
                    set_exchange_stop_loss_take_profit(signal_data, position_size, None)
                print("直接开仓成功")
            except Exception as e2:
                print(f"直接开仓也失败: {e2}")

        import traceback
        traceback.print_exc()

def analyze_with_deepseek_with_retry(price_data, max_retries=2):
    """带重试的DeepSeek分析"""
    for attempt in range(max_retries):
        try:
            signal_data = analyze_with_deepseek(price_data)
            if signal_data and not signal_data.get('is_fallback', False):
                return signal_data

            print(f"第{attempt + 1}次尝试失败，进行重试...")
            time.sleep(1)

        except Exception as e:
            print(f"第{attempt + 1}次尝试异常: {e}")
            if attempt == max_retries - 1:
                return create_fallback_signal(price_data)
            time.sleep(1)

    return create_fallback_signal(price_data)

def check_active_orders():
    """检查当前活跃的止盈止损订单状态"""
    try:
        symbol = TRADE_CONFIG['symbol']
        open_orders = exchange.fetch_open_orders(symbol)
        
        sl_tp_orders = []
        for order in open_orders:
            if order['type'] in ['stop_market', 'take_profit_market']:
                sl_tp_orders.append({
                    'id': order['id'],
                    'type': order['type'],
                    'side': order['side'],
                    'stopPrice': order.get('stopPrice', 0),
                    'amount': order['amount'],
                    'status': order['status']
                })
        
        print(f"🔍 当前活跃止盈止损订单: {len(sl_tp_orders)} 个")
        for order in sl_tp_orders:
            print(f"   - {order['type']} #{order['id']}: {order['side']} {order['amount']}张 @ ${order['stopPrice']:.2f}")
        
        return sl_tp_orders
    except Exception as e:
        print(f"检查活跃订单失败: {e}")
        return []

def wait_for_next_period():
    """等待到下一个执行周期"""
    interval = TRADE_CONFIG['execution_interval']
    now = datetime.now()
    current_minute = now.minute
    current_second = now.second

    # 计算下一个执行时间
    next_period_minute = ((current_minute // interval) + 1) * interval
    if next_period_minute >= 60:
        next_period_minute = 0

    if next_period_minute > current_minute:
        minutes_to_wait = next_period_minute - current_minute
    else:
        minutes_to_wait = 60 - current_minute + next_period_minute

    seconds_to_wait = minutes_to_wait * 60 - current_second

    display_minutes = minutes_to_wait - 1 if current_second > 0 else minutes_to_wait
    display_seconds = 60 - current_second if current_second > 0 else 0

    if display_minutes > 0:
        print(f"🕒 等待 {display_minutes} 分 {display_seconds} 秒到下一个{interval}分钟周期...")
    else:
        print(f"🕒 等待 {display_seconds} 秒到下一个{interval}分钟周期...")

    return seconds_to_wait

def trading_bot():
    """主交易机器人函数"""
    # 等待到执行时间
    wait_seconds = wait_for_next_period()
    if wait_seconds > 0:
        time.sleep(wait_seconds)

    print("\n" + "=" * 60)
    print(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 检查当前活跃订单
    check_active_orders()

    price_data = get_sol_ohlcv_enhanced()
    if not price_data:
        return

    print(f"SOL当前价格: ${price_data['price']:,.2f}")
    print(f"数据周期: {TRADE_CONFIG['timeframe']}")
    print(f"价格变化: {price_data['price_change']:+.2f}%")
    print(f"持仓时间: {trade_stats['position_hold_time']}个周期")

    signal_data = analyze_with_deepseek_with_retry(price_data)

    if signal_data.get('is_fallback', False):
        print("⚠️ 使用备用交易信号")

    execute_intelligent_trade(signal_data, price_data)

def main():
    """主函数"""
    print("SOL/USDT BINANCE自动交易机器人启动成功！")
    print("完整版本：智能止盈止损 + 交易所同步 + 参数化执行间隔")
    
    if TRADE_CONFIG['test_mode']:
        print("当前为模拟模式，不会真实下单")
    else:
        print("实盘交易模式，请谨慎操作！")

    print(f"交易周期: {TRADE_CONFIG['timeframe']}")
    print(f"执行间隔: {TRADE_CONFIG['execution_interval']}分钟")
    print("已启用完整止盈止损功能和防频繁反转功能")

    if not setup_exchange():
        print("交易所初始化失败，程序退出")
        return

    print("开始执行交易循环...")

    while True:
        trading_bot()
        time.sleep(60)

if __name__ == "__main__":
    main()