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
    'execution_interval': 15,  # 🆕 新增：执行间隔分钟数
    
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
        'enable_exchange_sl_tp': True,  # 🆕 是否在交易所设置止盈止损
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
    'active_orders': []  # 🆕 跟踪活跃订单
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

        contract_size = final_usdt / (price_data['price'] * TRADE_CONFIG['contract_size'])

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

# ... (中间的技术指标计算函数保持不变，为节省篇幅省略) ...

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

def execute_intelligent_trade(signal_data, price_data):
    """执行智能交易 - 完整止盈止损逻辑"""
    global position, trade_stats

    current_position = get_current_position()

    # 🆕 使用智能反转判断
    if current_position and signal_data['signal'] != 'HOLD':
        current_side = current_position['side']
        signal_side = 'long' if signal_data['signal'] == 'BUY' else 'short' if signal_data['signal'] == 'SELL' else None
        
        if current_side != signal_side:
            if not should_reverse_position(current_position, signal_data, price_data):
                print(f"🔒 反转条件不满足，保持现有{current_side}仓")
                return

    # 🆕 智能计算止盈止损
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

            # 🆕 设置止盈止损订单
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

            # 🆕 设置止盈止损订单
            if signal_data['signal'] == 'SELL':
                set_exchange_stop_loss_take_profit(signal_data, position_size, current_position)

        elif signal_data['signal'] == 'HOLD':
            print("建议观望，不执行交易")
            if current_position:
                trade_stats['position_hold_time'] += 1  # 持仓时间增加
            # 🆕 HOLD时也检查是否需要更新止盈止损
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

# ... (其他辅助函数保持不变，为节省篇幅省略) ...

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

def trading_bot():
    """主交易机器人函数"""
    # 等待到执行时间
    wait_seconds = wait_for_next_period()
    if wait_seconds > 0:
        time.sleep(wait_seconds)

    print("\n" + "=" * 60)
    print(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 🆕 检查当前活跃订单
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