# -*- coding: utf-8 -*-
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

# binance + 带市场情绪 + 指标版本 + 优化止盈止损

load_dotenv()

# 初始化DeepSeek客户端
deepseek_client = OpenAI(
    api_key=os.getenv('DEEPSEEK_API_KEY'),
    base_url="https://api.deepseek.com"
)

exchange = ccxt.binance({
    'options': {'defaultType': 'future'},
    'apiKey': os.getenv('BINANCE_API_KEY'),
    'secret': os.getenv('BINANCE_SECRET'),
})

# 交易参数配置 - 优化版
TRADE_CONFIG = {
    'symbol': 'SOL/USDT',
    'leverage': 10,
    'timeframe': '15m',
    'execution_interval': 15,  # 🆕 新增：执行间隔分钟数
    'test_mode': False,
    'data_points': 96,
    'analysis_periods': {
        'short_term': 20,
        'medium_term': 50,
        'long_term': 96
    },
    # 优化止盈止损参数
    'stop_loss_take_profit': {
        'base_stop_loss_pct': 0.02,  # 基础止损百分比
        'base_take_profit_pct': 0.04,  # 基础止盈百分比
        'high_confidence_sl_pct': 0.015,  # 高信心止损
        'high_confidence_tp_pct': 0.05,  # 高信心止盈
        'low_confidence_sl_pct': 0.025,  # 低信心止损
        'low_confidence_tp_pct': 0.03,  # 低信心止盈
        'trend_adjustment_factor': 1.2,  # 趋势强度调整系数
        'volatility_adjustment': True,  # 是否根据波动率调整
    },
    'position_management': {
        'enable_intelligent_position': True,
        'base_usdt_amount': 100,
        'high_confidence_multiplier': 1.5,
        'medium_confidence_multiplier': 1.0,
        'low_confidence_multiplier': 0.5,
        'max_position_ratio': 10,
        'trend_strength_multiplier': 1.2
    },
    # 🆕 新增：防频繁交易参数
    'anti_whipsaw': {
        'min_hold_periods': 2,  # 最小持仓周期数
        'signal_confirmation_periods': 2,  # 信号确认周期数
        'max_reversals_per_hour': 1,  # 每小时最大反转次数
        'profit_threshold_for_early_close': 0.02,  # 提前平仓的盈利阈值
    }
}

# 全局变量
price_history = []
signal_history = []
position = None
active_orders = {}  # 🆕 新增：活跃订单跟踪
last_reversal_time = None  # 🆕 新增：上次反转时间


def setup_exchange():
    """设置交易所参数 - 强制全仓模式"""
    try:
        print("🔍 获取SOL合约规格...")
        markets = exchange.load_markets()
        sol_market = markets[TRADE_CONFIG['symbol']]

        contract_size = 1
        #print(f"✅ 合约规格: 1张 = {contract_size} SOL")

        TRADE_CONFIG['contract_size'] = contract_size
        TRADE_CONFIG['min_amount'] = sol_market['limits']['amount']['min']

        print(f"📏 最小交易量: {TRADE_CONFIG['min_amount']} 张")

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

        print("🔄 设置单向持仓模式...")
        try:
            exchange.set_position_mode(False, TRADE_CONFIG['symbol'])
            print("✅ 已设置单向持仓模式")
        except Exception as e:
            print(f"⚠️ 设置单向持仓模式失败 (可能已设置): {e}")

        print("⚙️ 设置全仓模式和杠杆...")
        exchange.set_leverage(
            TRADE_CONFIG['leverage'],
            TRADE_CONFIG['symbol'],
            {'mgnMode': 'cross'}
        )
        print(f"✅ 已设置全仓模式，杠杆倍数: {TRADE_CONFIG['leverage']}x")

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


def calculate_volatility_adjusted_stop_loss_take_profit(price_data, signal_data, current_price):
    """计算基于波动率调整的止盈止损价格"""
    sl_config = TRADE_CONFIG['stop_loss_take_profit']
    
    try:
        # 获取近期价格波动率
        df = price_data['full_data']
        recent_high = df['high'].tail(20).max()
        recent_low = df['low'].tail(20).min()
        volatility = (recent_high - recent_low) / current_price
        
        # 基础止盈止损比例
        if signal_data['confidence'] == 'HIGH':
            base_sl_pct = sl_config['high_confidence_sl_pct']
            base_tp_pct = sl_config['high_confidence_tp_pct']
        elif signal_data['confidence'] == 'LOW':
            base_sl_pct = sl_config['low_confidence_sl_pct']
            base_tp_pct = sl_config['low_confidence_tp_pct']
        else:
            base_sl_pct = sl_config['base_stop_loss_pct']
            base_tp_pct = sl_config['base_take_profit_pct']
        
        # 根据波动率调整
        if sl_config['volatility_adjustment']:
            volatility_factor = min(max(volatility / 0.05, 0.8), 1.5)  # 波动率在5%基准上下调整
            base_sl_pct *= volatility_factor
            base_tp_pct *= volatility_factor
        
        # 根据趋势强度调整
        trend = price_data['trend_analysis'].get('overall', '震荡整理')
        if trend in ['强势上涨', '强势下跌']:
            trend_factor = sl_config['trend_adjustment_factor']
            base_tp_pct *= trend_factor
            base_sl_pct *= (2 - trend_factor)  # 止损相对收紧
        
        # 计算具体价格
        if signal_data['signal'] == 'BUY':
            stop_loss = current_price * (1 - base_sl_pct)
            take_profit = current_price * (1 + base_tp_pct)
        else:  # SELL
            stop_loss = current_price * (1 + base_sl_pct)
            take_profit = current_price * (1 - base_tp_pct)
        
        print(f"📊 止盈止损计算:")
        print(f"   - 波动率: {volatility:.3f}")
        print(f"   - 基础止损: {base_sl_pct:.3%}")
        print(f"   - 基础止盈: {base_tp_pct:.3%}")
        print(f"   - 最终止损: {stop_loss:.2f}")
        print(f"   - 最终止盈: {take_profit:.2f}")
        
        return stop_loss, take_profit
        
    except Exception as e:
        print(f"❌ 止盈止损计算失败，使用默认值: {e}")
        # 备用计算
        if signal_data['signal'] == 'BUY':
            return current_price * 0.98, current_price * 1.04
        else:
            return current_price * 1.02, current_price * 0.96


def should_avoid_reversal(current_position, signal_data, price_data):
    """判断是否应该避免频繁反转"""
    anti_config = TRADE_CONFIG['anti_whipsaw']
    global last_reversal_time
    
    # 如果没有持仓或者信号是HOLD，不需要检查
    if not current_position or signal_data['signal'] == 'HOLD':
        return False
        
    current_side = current_position['side']
    new_side = 'long' if signal_data['signal'] == 'BUY' else 'short'
    
    # 如果是同方向，不需要反转检查
    if current_side == new_side:
        return False
    
    # 检查最小持仓周期
    if len(signal_history) >= anti_config['min_hold_periods']:
        recent_signals = [s['signal'] for s in signal_history[-anti_config['min_hold_periods']:]]
        if all(s == signal_data['signal'] for s in recent_signals):
            print(f"🔒 信号已持续{anti_config['min_hold_periods']}周期，允许反转")
            return False
    
    # 检查反转频率限制
    if last_reversal_time:
        time_since_last_reversal = (datetime.now() - last_reversal_time).total_seconds() / 60
        if time_since_last_reversal < 60 / anti_config['max_reversals_per_hour']:
            print(f"🔒 距离上次反转仅{time_since_last_reversal:.1f}分钟，跳过本次反转")
            return True
    
    # 检查当前持仓盈亏状况
    unrealized_pnl = current_position.get('unrealized_pnl', 0)
    entry_price = current_position.get('entry_price', 0)
    current_price = price_data['price']
    
    if entry_price > 0:
        pnl_pct = abs(current_price - entry_price) / entry_price
        if unrealized_pnl > 0 and pnl_pct < anti_config['profit_threshold_for_early_close']:
            print(f"🔒 当前盈利{pnl_pct:.2%}未达提前平仓阈值，保持持仓")
            return True
    
    # 如果是低信心反转信号，更加谨慎
    if signal_data['confidence'] == 'LOW':
        print("🔒 低信心反转信号，保持现有持仓")
        return True
        
    return False


def manage_stop_loss_take_profit_orders(current_position, signal_data, price_data):
    """管理止盈止损订单 - 同步到交易所"""
    global active_orders
    
    try:
        # 取消所有未完成的止损止盈订单
        if active_orders:
            print("🔄 取消现有止盈止损订单...")
            for order_id, order_info in list(active_orders.items()):
                try:
                    exchange.cancel_order(order_id, TRADE_CONFIG['symbol'])
                    print(f"   - 已取消订单: {order_id}")
                except Exception as e:
                    print(f"   - 取消订单失败 {order_id}: {e}")
            active_orders = {}
        
        # 如果没有持仓或者是HOLD信号，不需要设置新订单
        if not current_position or signal_data['signal'] == 'HOLD':
            return
        
        # 计算新的止盈止损价格
        current_price = price_data['price']
        stop_loss, take_profit = calculate_volatility_adjusted_stop_loss_take_profit(
            price_data, signal_data, current_price
        )
        
        # 根据持仓方向创建止损止盈订单
        if current_position['side'] == 'long':
            # 多头持仓：止损卖单，止盈卖单
            try:
                # 止损单
                sl_order = exchange.create_order(
                    TRADE_CONFIG['symbol'],
                    'stop_market',
                    'sell',
                    current_position['size'],
                    None,
                    {
                        'stopPrice': stop_loss,
                        'reduceOnly': True,
                        'tag': 'SL_60bb4a8d3416BCDE'
                    }
                )
                active_orders[sl_order['id']] = {'type': 'stop_loss', 'price': stop_loss}
                print(f"✅ 设置止损单: {stop_loss:.2f}")
                
                # 止盈单
                tp_order = exchange.create_order(
                    TRADE_CONFIG['symbol'],
                    'take_profit_market',
                    'sell',
                    current_position['size'],
                    None,
                    {
                        'stopPrice': take_profit,
                        'reduceOnly': True,
                        'tag': 'TP_60bb4a8d3416BCDE'
                    }
                )
                active_orders[tp_order['id']] = {'type': 'take_profit', 'price': take_profit}
                print(f"✅ 设置止盈单: {take_profit:.2f}")
                
            except Exception as e:
                print(f"⚠️ 设置止盈止损单失败: {e}")
                
        else:  # 空头持仓
            try:
                # 止损单
                sl_order = exchange.create_order(
                    TRADE_CONFIG['symbol'],
                    'stop_market',
                    'buy',
                    current_position['size'],
                    None,
                    {
                        'stopPrice': stop_loss,
                        'reduceOnly': True,
                        'tag': 'SL_60bb4a8d3416BCDE'
                    }
                )
                active_orders[sl_order['id']] = {'type': 'stop_loss', 'price': stop_loss}
                print(f"✅ 设置止损单: {stop_loss:.2f}")
                
                # 止盈单
                tp_order = exchange.create_order(
                    TRADE_CONFIG['symbol'],
                    'take_profit_market',
                    'buy',
                    current_position['size'],
                    None,
                    {
                        'stopPrice': take_profit,
                        'reduceOnly': True,
                        'tag': 'TP_60bb4a8d3416BCDE'
                    }
                )
                active_orders[tp_order['id']] = {'type': 'take_profit', 'price': take_profit}
                print(f"✅ 设置止盈单: {take_profit:.2f}")
                
            except Exception as e:
                print(f"⚠️ 设置止盈止损单失败: {e}")
                
    except Exception as e:
        print(f"❌ 止盈止损订单管理失败: {e}")


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

        max_usdt = usdt_balance * config['max_position_ratio'] / 100  # 修正比例计算
        final_usdt = min(suggested_usdt, max_usdt)

        contract_size = final_usdt / (price_data['price'] * TRADE_CONFIG['contract_size'])

        print(f"📊 仓位计算详情:")
        print(f"   - 基础USDT: {base_usdt}")
        print(f"   - 信心倍数: {confidence_multiplier}")
        print(f"   - 趋势倍数: {trend_multiplier}")
        print(f"   - RSI倍数: {rsi_multiplier}")
        print(f"   - 建议USDT: {suggested_usdt:.2f}")
        print(f"   - 最终USDT: {final_usdt:.2f}")
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
        contract_size = base_usdt / (price_data['price'] * TRADE_CONFIG.get('contract_size', 1))
        return round(max(contract_size, TRADE_CONFIG.get('min_amount', 1)), 0)


def calculate_technical_indicators(df):
    """计算技术指标"""
    try:
        df['sma_5'] = df['close'].rolling(window=5, min_periods=1).mean()
        df['sma_20'] = df['close'].rolling(window=20, min_periods=1).mean()
        df['sma_50'] = df['close'].rolling(window=50, min_periods=1).mean()

        df['ema_12'] = df['close'].ewm(span=12).mean()
        df['ema_26'] = df['close'].ewm(span=26).mean()
        df['macd'] = df['ema_12'] - df['ema_26']
        df['macd_signal'] = df['macd'].ewm(span=9).mean()
        df['macd_histogram'] = df['macd'] - df['macd_signal']

        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))

        df['bb_middle'] = df['close'].rolling(20).mean()
        bb_std = df['close'].rolling(20).std()
        df['bb_upper'] = df['bb_middle'] + (bb_std * 2)
        df['bb_lower'] = df['bb_middle'] - (bb_std * 2)
        df['bb_position'] = (df['close'] - df['bb_lower']) / (df['bb_upper'] - df['bb_lower'])

        df['volume_ma'] = df['volume'].rolling(20).mean()
        df['volume_ratio'] = df['volume'] / df['volume_ma']

        df['resistance'] = df['high'].rolling(20).max()
        df['support'] = df['low'].rolling(20).min()

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
    """获取情绪指标"""
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
            print(f"pos: {pos}")
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


def analyze_with_deepseek(price_data):
    """使用DeepSeek分析市场并生成交易信号（优化提示词版）"""

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

    # 🆕 优化提示词
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
    1. **趋势持续性优先**: 趋势一旦形成，大概率会延续。不要因单根K线或短期波动改变整体趋势判断
    2. **减少频繁交易**: 避免在窄幅震荡中频繁开平仓，只在趋势明确时行动
    3. **持仓稳定性**: 除非出现明确的趋势反转信号，否则保持现有持仓方向
    4. **反转确认要求**: 需要至少2-3个技术指标同时确认趋势反转，且价格必须突破关键支撑/阻力位

    【智能止盈止损策略】
    - 趋势行情中：放宽止盈目标，收紧止损（让利润奔跑）
    - 震荡行情中：缩小止盈止损范围，快进快出
    - 根据波动率动态调整：高波动率时适当扩大止盈止损范围

    【交易信号生成规则】
    1. **强势趋势信号** (权重最高):
       - 价格突破关键阻力 + 成交量放大 → 高信心BUY
       - 价格跌破关键支撑 + 成交量放大 → 高信心SELL
       - 均线呈多头/空头排列 → 相应方向信号

    2. **震荡市场信号**:
       - 布林带收窄 + RSI接近50 → HOLD信号
       - 无明显方向时 → 保持观望

    3. **反转信号确认条件** (严格要求):
       - 价格必须突破关键支撑/阻力位
       - RSI出现背离信号
       - MACD出现金叉/死叉确认
       - 成交量配合突破

    4. **持仓优化逻辑**:
       - 已有持仓且趋势延续 → 保持或同方向加仓信号
       - 趋势明确反转 → 及时反向信号
       - 不要因为已有持仓而过度HOLD

    【当前技术状况分析】
    - 整体趋势: {price_data['trend_analysis'].get('overall', 'N/A')}
    - 短期趋势: {price_data['trend_analysis'].get('short_term', 'N/A')} 
    - RSI状态: {price_data['technical_data'].get('rsi', 0):.1f} ({'超买' if price_data['technical_data'].get('rsi', 0) > 70 else '超卖' if price_data['technical_data'].get('rsi', 0) < 30 else '中性'})
    - MACD方向: {price_data['trend_analysis'].get('macd', 'N/A')}

    【重要提醒】
    - 避免因小幅波动而频繁改变持仓方向
    - 趋势明确时要有持仓勇气
    - 震荡行情中保持耐心，减少不必要的交易
    - 每次交易都要有明确的止盈止损计划

    请基于以上分析，给出明确的交易信号，并详细说明理由。

    请用以下JSON格式回复：
    {{
        "signal": "BUY|SELL|HOLD",
        "reason": "详细分析理由(包含趋势判断、技术依据和风险考量)",
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
                 "content": f"您是一位专业的趋势交易员，专注于{TRADE_CONFIG['timeframe']}周期趋势跟踪。请避免频繁交易，只在趋势明确时行动。"},
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
    """执行智能交易 - 优化止盈止损版"""
    global position, last_reversal_time

    current_position = get_current_position()

    # 🆕 新增：防频繁反转检查
    if should_avoid_reversal(current_position, signal_data, price_data):
        print("🔒 防频繁反转机制生效，跳过本次交易")
        return

    # 计算智能仓位
    position_size = calculate_intelligent_position(signal_data, price_data, current_position)

    print(f"交易信号: {signal_data['signal']}")
    print(f"信心程度: {signal_data['confidence']}")
    print(f"智能仓位: {position_size:.2f} 张")
    print(f"理由: {signal_data['reason']}")
    print(f"当前持仓: {current_position}")

    if signal_data['confidence'] == 'LOW' and not TRADE_CONFIG['test_mode']:
        print("⚠️ 低信心信号，跳过执行")
        return

    if TRADE_CONFIG['test_mode']:
        print("测试模式 - 仅模拟交易")
        return

    try:
        # 🆕 先管理止盈止损订单
        manage_stop_loss_take_profit_orders(current_position, signal_data, price_data)

        # 执行交易逻辑
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
                    last_reversal_time = datetime.now()  # 🆕 记录反转时间
                else:
                    print("⚠️ 检测到空头持仓但数量为0，直接开多仓")
                    exchange.create_market_order(
                        TRADE_CONFIG['symbol'],
                        'buy',
                        position_size,
                        params={'tag': '60bb4a8d3416BCDE'}
                    )

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
            else:
                print(f"开多仓 {position_size:.2f} 张...")
                exchange.create_market_order(
                    TRADE_CONFIG['symbol'],
                    'buy',
                    position_size,
                    params={'tag': '60bb4a8d3416BCDE'}
                )

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
                    last_reversal_time = datetime.now()  # 🆕 记录反转时间
                else:
                    print("⚠️ 检测到多头持仓但数量为0，直接开空仓")
                    exchange.create_market_order(
                        TRADE_CONFIG['symbol'],
                        'sell',
                        position_size,
                        params={'tag': '60bb4a8d3416BCDE'}
                    )

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
            else:
                print(f"开空仓 {position_size:.2f} 张...")
                exchange.create_market_order(
                    TRADE_CONFIG['symbol'],
                    'sell',
                    position_size,
                    params={'tag': '60bb4a8d3416BCDE'}
                )

        elif signal_data['signal'] == 'HOLD':
            print("建议观望，不执行交易")
            return

        print("智能交易执行成功")
        
        # 🆕 交易完成后重新设置止盈止损
        time.sleep(2)
        new_position = get_current_position()
        if new_position:
            manage_stop_loss_take_profit_orders(new_position, signal_data, price_data)
        
        position = new_position
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
                elif signal_data['signal'] == 'SELL':
                    exchange.create_market_order(
                        TRADE_CONFIG['symbol'],
                        'sell',
                        position_size,
                        params={'tag': '60bb4a8d3416BCDE'}
                    )
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


def wait_for_next_period():
    """等待到下一个执行周期 - 参数化版本"""
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
        print(f"🕒 等待 {display_minutes} 分 {display_seconds} 秒到下个{interval}分钟周期...")
    else:
        print(f"🕒 等待 {display_seconds} 秒到下个{interval}分钟周期...")

    return seconds_to_wait


def trading_bot():
    # 等待到执行周期
    wait_seconds = wait_for_next_period()
    if wait_seconds > 0:
        time.sleep(wait_seconds)

    """主交易机器人函数"""
    print("\n" + "=" * 60)
    print(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    price_data = get_sol_ohlcv_enhanced()
    if not price_data:
        return

    print(f"SOL当前价格: ${price_data['price']:,.2f}")
    print(f"数据周期: {TRADE_CONFIG['timeframe']}")
    print(f"价格变化: {price_data['price_change']:+.2f}%")

    signal_data = analyze_with_deepseek_with_retry(price_data)

    if signal_data.get('is_fallback', False):
        print("⚠️ 使用备用交易信号")

    execute_intelligent_trade(signal_data, price_data)


def main():
    """主函数"""
    print("SOL/USDT BINANCE自动交易机器人启动成功！")
    print("优化版：智能止盈止损 + 防频繁交易")

    if TRADE_CONFIG['test_mode']:
        print("当前为模拟模式，不会真实下单")
    else:
        print("实盘交易模式，请谨慎操作！")

    print(f"交易周期: {TRADE_CONFIG['timeframe']}")
    print(f"执行间隔: {TRADE_CONFIG['execution_interval']}分钟")
    print("已启用智能止盈止损和防频繁交易功能")

    if not setup_exchange():
        print("交易所初始化失败，程序退出")
        return

    print(f"执行频率: 每{TRADE_CONFIG['execution_interval']}分钟执行")

    while True:
        trading_bot()
        time.sleep(60)


if __name__ == "__main__":
    main()