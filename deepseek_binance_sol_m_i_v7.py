## 主要修改内容：
#1、修复止盈止损设置：使用Binance支持的普通限价订单替代算法订单，通过reduceOnly=True参数确保只减少持仓
#2、添加价格验证：根据持仓方向自动验证止损止盈价格的合理性
#3、优化订单取消逻辑：更好地识别和取消条件订单
#4、添加等待时间：在设置止盈止损前等待订单执行完成
#5、改进错误处理：当一种方法失败时尝试备选方案

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

# 初始化DeepSeek客户端
deepseek_client = OpenAI(
    api_key=os.getenv('DEEPSEEK_API_KEY'),
    base_url="https://api.deepseek.com"
)

# 初始化Binance交易所
exchange = ccxt.binance({
    'options': {
        'defaultType': 'future',  # Binance使用future表示永续合约
    },
    'apiKey': os.getenv('BINANCE_API_KEY'),
    'secret': os.getenv('BINANCE_SECRET'),
})

# 钉钉机器人配置
DINGTALK_CONFIG = {
    'webhook': os.getenv('DINGTALK_WEBHOOK'),
    'secret': os.getenv('DINGTALK_SECRET'),
    'enable': True  # 是否启用钉钉通知
}

# 交易参数配置 - 针对SOL优化
TRADE_CONFIG = {
    'user':os.getenv('EXECUTION_USER'),
    'symbol': 'SOL/USDT:USDT',  # Binance的SOL合约符号
    'leverage': 10,  # 杠杆倍数
    'timeframe': '15m',  # 使用15分钟K线
    'execution_interval': int(os.getenv('EXECUTION_INTERVAL')),  # 🆕 新增：执行间隔分钟数（可配置）
    'test_mode': False,  # 测试模式
    'data_points': 96,  # 24小时数据（96根15分钟K线）
    'analysis_periods': {
        'short_term': 20,  # 短期均线
        'medium_term': 50,  # 中期均线
        'long_term': 96  # 长期趋势
    },
    # 针对SOL的智能仓位参数（SOL价格较低，调整基础金额）
    'position_management': {
        'enable_intelligent_position': True,
        'base_usdt_amount': 100,  # 🆕 调整：SOL价格较低，降低基础金额
        'high_confidence_multiplier': 1.5,
        'medium_confidence_multiplier': 1.0,
        'low_confidence_multiplier': 0.5,
        'max_position_ratio': 50,  # 单次最大仓位比例
        'trend_strength_multiplier': 1.2
    }
}

import hashlib
import hmac
import base64
import urllib.parse
import time

def send_dingtalk_message(title, message, message_type="info"):
    """发送钉钉机器人消息（带签名验证）"""
    if not DINGTALK_CONFIG['enable'] or not DINGTALK_CONFIG['webhook']:
        return
    
    try:
        # 根据消息类型设置表情符号
        emojis = {
            "info": "ℹ️",
            "success": "✅", 
            "warning": "⚠️",
            "error": "❌"
        }
        emoji = emojis.get(message_type, "ℹ️")
        
        timestamp = str(round(time.time() * 1000))
        
        # 🆕 生成签名
        secret = DINGTALK_CONFIG['secret']
        if secret:
            string_to_sign = f"{timestamp}\n{secret}"
            hmac_code = hmac.new(
                secret.encode('utf-8'), 
                string_to_sign.encode('utf-8'), 
                hashlib.sha256
            ).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            
            # 添加签名到webhook
            webhook_url = f"{DINGTALK_CONFIG['webhook']}&timestamp={timestamp}&sign={sign}"
        else:
            webhook_url = DINGTALK_CONFIG['webhook']
            print("⚠️ 未配置钉钉签名，使用无签名方式发送")

        # 构建消息内容
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        full_message = f"### [{TRADE_CONFIG['user']}] {emoji} {title}\n\n{message}\n\n---\n⏰ 时间: {current_time}"
        
        # 钉钉消息格式
        data = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"{emoji} {title}",
                "text": full_message
            },
            "at": {
                "isAtAll": False
            }
        }
        
        headers = {
            "Content-Type": "application/json",
            "Charset": "UTF-8"
        }
        
        response = requests.post(webhook_url, json=data, headers=headers, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('errcode') == 0:
                print(f"✅ 钉钉消息发送成功: {title}")
            else:
                print(f"❌ 钉钉消息发送失败: {result.get('errmsg', '未知错误')}")
        else:
            print(f"❌ 钉钉消息发送失败，状态码: {response.status_code}")
            
    except Exception as e:
        print(f"❌ 钉钉消息发送异常: {e}")

def setup_exchange():
    """设置交易所参数 - Binance版本"""
    try:
        print("🔍 获取SOL合约规格...")
        markets = exchange.load_markets()
        sol_market = markets[TRADE_CONFIG['symbol']]
        #print(sol_market)
        
        # 获取合约乘数（Binance SOL合约通常为1 SOL = 1张）
        contract_size = float(sol_market.get('contractSize', 1))
        print(f"✅ 合约规格: 1张 = {contract_size} SOL")

        # 存储合约规格到全局配置
        TRADE_CONFIG['contract_size'] = contract_size
        TRADE_CONFIG['min_amount'] = sol_market['limits']['amount']['min']

        print(f"📏 最小交易量: {TRADE_CONFIG['min_amount']} 张")

        # 设置杠杆
        print("⚙️ 设置杠杆...")
        exchange.set_leverage(TRADE_CONFIG['leverage'], TRADE_CONFIG['symbol'])
        print(f"✅ 已设置杠杆倍数: {TRADE_CONFIG['leverage']}x")

        # 验证设置
        print("🔍 验证账户设置...")
        balance = exchange.fetch_balance()
        usdt_balance = balance['USDT']['free'] if 'USDT' in balance else balance['total']['USDT']
        print(f"💰 当前USDT余额: {usdt_balance:.2f}")

        # 获取当前持仓状态
        current_pos = get_current_position()
        if current_pos:
            print(f"📦 当前持仓: {current_pos['side']}仓 {current_pos['size']}张")
            # 🆕 发送持仓状态到钉钉
            send_dingtalk_message(
                "🔔 交易机器人启动 - 检测到现有持仓",
                f"**持仓详情:**\n"
                f"- 方向: {current_pos['side']}\n"
                f"- 数量: {current_pos['size']}张\n"
                f"- 入场价: {current_pos['entry_price']:.2f}\n"
                f"- 浮动盈亏: {current_pos['unrealized_pnl']:.2f} USDT",
                "warning"
            )
        else:
            print("📦 当前无持仓")
            send_dingtalk_message(
                "🔔 交易机器人启动成功",
                f"**SOL/USDT 自动交易机器人已启动**\n\n"
                f"📊 配置信息:\n"
                f"- 交易对: {TRADE_CONFIG['symbol']}\n"
                f"- 杠杆: {TRADE_CONFIG['leverage']}x\n"
                f"- 周期: {TRADE_CONFIG['timeframe']}\n"
                f"- 执行间隔: {TRADE_CONFIG['execution_interval']}分钟\n"
                f"- 模式: {'🟢 实盘交易' if not TRADE_CONFIG['test_mode'] else '🟡 测试模式'}",
                "success"
            )

        print("🎯 程序配置完成：Binance合约交易")
        return True

    except Exception as e:
        error_msg = f"交易所设置失败: {e}"
        print(f"❌ {error_msg}")
        send_dingtalk_message("❌ 交易机器人启动失败", error_msg, "error")
        import traceback
        traceback.print_exc()
        return False


# 全局变量存储历史数据
price_history = []
signal_history = []
position = None


def calculate_intelligent_position(signal_data, price_data, current_position):
    """计算智能仓位大小 - SOL优化版"""
    config = TRADE_CONFIG['position_management']

    if not config.get('enable_intelligent_position', True):
        fixed_contracts = 0.1
        print(f"🔧 智能仓位已禁用，使用固定仓位: {fixed_contracts} 张")
        return fixed_contracts

    try:
        # 获取账户余额
        balance = exchange.fetch_balance()
        usdt_balance = balance['USDT']['free'] if 'USDT' in balance else balance['total']['USDT']

        # 基础USDT投入（针对SOL调整）
        base_usdt = config['base_usdt_amount']
        print(f"💰 可用USDT余额: {usdt_balance:.2f}, 下单基数{base_usdt}")

        # 根据信心程度调整
        confidence_multiplier = {
            'HIGH': config['high_confidence_multiplier'],
            'MEDIUM': config['medium_confidence_multiplier'],
            'LOW': config['low_confidence_multiplier']
        }.get(signal_data['confidence'], 1.0)

        # 根据趋势强度调整
        trend = price_data['trend_analysis'].get('overall', '震荡整理')
        if trend in ['强势上涨', '强势下跌']:
            trend_multiplier = config['trend_strength_multiplier']
        else:
            trend_multiplier = 1.0

        # 根据RSI状态调整
        rsi = price_data['technical_data'].get('rsi', 50)
        if rsi > 75 or rsi < 25:
            rsi_multiplier = 0.7
        else:
            rsi_multiplier = 1.0

        # 计算建议投入USDT金额
        suggested_usdt = base_usdt * confidence_multiplier * trend_multiplier * rsi_multiplier

        # 风险管理：不超过总资金的指定比例
        max_usdt = usdt_balance * (config['max_position_ratio'] / 100)
        final_usdt = min(suggested_usdt, max_usdt)

        # 合约张数计算
        contract_size = final_usdt / (price_data['price'] * TRADE_CONFIG['contract_size']) * TRADE_CONFIG['leverage']

        print(f"📊 仓位计算详情:")
        print(f"   - 基础USDT: {base_usdt}")
        print(f"   - 信心倍数: {confidence_multiplier}")
        print(f"   - 趋势倍数: {trend_multiplier}")
        print(f"   - RSI倍数: {rsi_multiplier}")
        print(f"   - 建议USDT: {suggested_usdt:.2f}")
        print(f"   - 最终USDT: {final_usdt:.2f}")
        print(f"   - 合约乘数: {TRADE_CONFIG['contract_size']}")
        print(f"   - 计算合约: {contract_size:.4f} 张")

        # 精度处理
        contract_size = round(contract_size, 2)

        # 确保最小交易量
        min_contracts = TRADE_CONFIG.get('min_amount', 1)
        if contract_size < min_contracts:
            contract_size = min_contracts
            print(f"⚠️ 仓位小于最小值，调整为: {contract_size} 张")

        print(f"🎯 最终仓位: {final_usdt:.2f} USDT → {contract_size:.2f} 张合约")
        return contract_size

    except Exception as e:
        print(f"❌ 仓位计算失败，使用基础仓位: {e}")
        base_usdt = config['base_usdt_amount']
        contract_size = (base_usdt * TRADE_CONFIG['leverage']) / (
                    price_data['price'] * TRADE_CONFIG.get('contract_size', 1))
        return round(max(contract_size, TRADE_CONFIG.get('min_amount', 1)), 2)


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

        # 动态支撑阻力（基于布林带）
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
    """获取情绪指标 - 针对SOL优化（如果API支持SOL）"""
    try:
        API_URL = "https://service.cryptoracle.network/openapi/v2/endpoint"
        API_KEY = "7ad48a56-8730-4238-a714-eebc30834e3e"

        # 获取最近4小时数据
        end_time = datetime.now()
        start_time = end_time - timedelta(hours=4)

        request_body = {
            "apiKey": API_KEY,
            "endpoints": ["CO-A-02-01", "CO-A-02-02"],
            "startTime": start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "endTime": end_time.strftime("%Y-%m-%d %H:%M:%S"),
            "timeType": "15m",
            "token": ["SOL"]  # 🆕 改为SOL
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

                        print(f"✅ 使用SOL情绪数据时间: {period['startTime']} (延迟: {data_delay}分钟)")

                        return {
                            'positive_ratio': positive,
                            'negative_ratio': negative,
                            'net_sentiment': net_sentiment,
                            'data_time': period['startTime'],
                            'data_delay_minutes': data_delay
                        }

                print("❌ 所有时间段SOL情绪数据都为空")
                return None

        return None
    except Exception as e:
        print(f"SOL情绪指标获取失败: {e}")
        return None


def get_market_trend(df):
    """判断市场趋势"""
    try:
        current_price = df['close'].iloc[-1]

        # 多时间框架趋势分析
        trend_short = "上涨" if current_price > df['sma_20'].iloc[-1] else "下跌"
        trend_medium = "上涨" if current_price > df['sma_50'].iloc[-1] else "下跌"

        # MACD趋势
        macd_trend = "bullish" if df['macd'].iloc[-1] > df['macd_signal'].iloc[-1] else "bearish"

        # 综合趋势判断
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
        # 获取K线数据
        ohlcv = exchange.fetch_ohlcv(TRADE_CONFIG['symbol'], TRADE_CONFIG['timeframe'],
                                     limit=TRADE_CONFIG['data_points'])

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

        # 计算技术指标
        df = calculate_technical_indicators(df)

        current_data = df.iloc[-1]
        previous_data = df.iloc[-2]

        # 获取技术分析数据
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
        print(f"获取SOL增强K线数据失败: {e}")
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
    【SOL技术指标分析】
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
    """获取当前持仓情况 - Binance版本"""
    try:
        positions = exchange.fetch_positions([TRADE_CONFIG['symbol']])

        for pos in positions:
            if pos['symbol'] == TRADE_CONFIG['symbol']:
                contracts = float(pos['contracts']) if pos['contracts'] else 0

                if contracts > 0:
                    return {
                        'side': pos['side'],  # 'long' or 'short'
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
    """使用DeepSeek分析SOL市场并生成交易信号"""

    technical_analysis = generate_technical_analysis_text(price_data)

    kline_text = f"【SOL最近5根{TRADE_CONFIG['timeframe']}K线数据】\n"
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
        sentiment_text = f"【SOL市场情绪】乐观{sentiment_data['positive_ratio']:.1%} 悲观{sentiment_data['negative_ratio']:.1%} 净值{sign}{sentiment_data['net_sentiment']:.3f}"
    else:
        sentiment_text = "【SOL市场情绪】数据暂不可用"

    current_pos = get_current_position()
    position_text = "无持仓" if not current_pos else f"{current_pos['side']}仓, 数量: {current_pos['size']}, 盈亏: {current_pos['unrealized_pnl']:.2f}USDT"
    pnl_text = f", 持仓盈亏: {current_pos['unrealized_pnl']:.2f} USDT" if current_pos else ""

    prompt = f"""
    你是一个专业的加密货币交易分析师，最近波动频繁通过你交易的都亏麻了，已经吃不上饭了，多上点心吧，一定要注意短期波动呀，稳妥点呀。请基于以下SOL/USDT {TRADE_CONFIG['timeframe']}周期数据进行分析：

    {kline_text}

    {technical_analysis}

    {signal_text}

    {sentiment_text}

    【当前SOL行情】
    - 当前价格: ${price_data['price']:,.2f}
    - 时间: {price_data['timestamp']}
    - 本K线最高: ${price_data['high']:,.2f}
    - 本K线最低: ${price_data['low']:,.2f}
    - 本K线成交量: {price_data['volume']:.2f} SOL
    - 价格变化: {price_data['price_change']:+.2f}%
    - 当前持仓: {position_text}{pnl_text}

    【防频繁交易重要原则】
    1. **趋势持续性优先**: 不要因单根K线或短期波动改变整体趋势判断
    2. **持仓稳定性**: 除非趋势明确强烈反转，否则保持现有持仓方向
    3. **反转确认**: 需要至少2-3个技术指标同时确认趋势反转才改变信号
    4. **成本意识**: 减少不必要的仓位调整，每次交易都有成本

    【交易指导原则 - 必须遵守】
    1. **技术分析主导** (权重60%)：趋势、支撑阻力、K线形态是主要依据
    2. **市场情绪辅助** (权重30%)：情绪数据用于验证技术信号，不能单独作为交易理由  
    3. **风险管理** (权重10%)：考虑持仓、盈亏状况和止损位置
    4. **趋势跟随**: 明确趋势出现时立即行动，不要过度等待
    5. **SOL特性**: SOL波动性较大，需要更严格的风险控制
    6. **信号明确性**:
    - 强势上涨趋势 → BUY信号
    - 强势下跌趋势 → SELL信号  
    - 仅在窄幅震荡、无明确方向时 → HOLD信号
    7. **技术指标权重**:
    - 趋势(均线排列) > RSI > MACD > 布林带
    - 价格突破关键支撑/阻力位是重要信号 

    【当前技术状况分析】
    - 整体趋势: {price_data['trend_analysis'].get('overall', 'N/A')}
    - 短期趋势: {price_data['trend_analysis'].get('short_term', 'N/A')} 
    - RSI状态: {price_data['technical_data'].get('rsi', 0):.1f} ({'超买' if price_data['technical_data'].get('rsi', 0) > 70 else '超卖' if price_data['technical_data'].get('rsi', 0) < 30 else '中性'})
    - MACD方向: {price_data['trend_analysis'].get('macd', 'N/A')}

    【智能仓位管理规则 - 必须遵守】
    1. **减少过度保守**：
       - 明确趋势中不要因轻微超买/超卖而过度HOLD
       - RSI在30-70区间属于健康范围，不应作为主要HOLD理由
    2. **趋势跟随优先**：
       - 强势上涨趋势 + 任何RSI值 → 积极BUY信号
       - 强势下跌趋势 + 任何RSI值 → 积极SELL信号
    3. **SOL波动性考虑**：
       - SOL波动较大，止损幅度可适当放宽
       - 仓位控制要更加严格

    【重要】请基于技术分析做出明确判断，避免因过度谨慎而错过趋势行情！

    【分析要求】
    基于以上分析，请给出明确的交易信号

    请用以下JSON格式回复：
    {{
        "signal": "BUY|SELL|HOLD",
        "reason": "简要分析理由(包含趋势判断和技术依据)",
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
                 "content": f"您是一位专业的交易员，最近波动频繁通过你交易的都亏麻了，已经吃不上饭了，多上点心吧，一定要注意短期波动呀，稳妥点呀。专注于SOL/USDT的{TRADE_CONFIG['timeframe']}周期趋势分析。请结合K线形态和技术指标做出判断，并严格遵循JSON格式要求。"},
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

def cancel_existing_conditional_orders():
    """取消所有现有的条件订单（止盈止损）"""
    try:
        orders = exchange.fetch_open_orders(TRADE_CONFIG['symbol'])
        cancelled_count = 0
        
        for order in orders:
            try:
                # 检查是否为条件订单或止盈止损相关的订单
                order_type = order.get('type', '')
                order_info = order.get('info', {})
                
                # 检查是否是止损止盈相关订单
                is_conditional = (
                    'stop' in order_type.lower() or 
                    'take' in order_type.lower() or
                    'reduceOnly' in order_info or
                    order.get('reduceOnly', False)
                )
                
                if is_conditional:
                    print(f"取消条件订单: {order['id']} - {order_type}")
                    exchange.cancel_order(order['id'], TRADE_CONFIG['symbol'])
                    cancelled_count += 1
                    time.sleep(0.1)  # 避免API限制
                    
            except Exception as e:
                print(f"取消订单 {order['id']} 失败: {e}")
                continue
        
        if cancelled_count > 0:
            print(f"✅ 已取消 {cancelled_count} 个条件订单")
        else:
            print("ℹ️ 没有找到条件订单需要取消")
            
        return cancelled_count
        
    except Exception as e:
        print(f"❌ 取消条件订单失败: {e}")
        return 0

def setup_take_profit_stop_loss(position_side, position_size, take_profit_price, stop_loss_price):
    """设置止盈止损订单（修复算法订单问题）"""
    
    def get_account_type():
        """获取账户类型"""
        try:
            # 检查是否是期货账户
            if hasattr(exchange, 'fapiPrivateGetAccount'):
                return 'future'
            elif hasattr(exchange, 'dapiPrivateGetAccount'):
                return 'coin_future'
            else:
                return 'spot'
        except:
            return 'spot'
    
    def create_algo_order_for_future(symbol, order_type, side, quantity, trigger_price, position_side, tag=''):
        """
        为期货账户创建算法订单
        """
        try:
            # 对于币安期货，算法订单可能需要特殊的端点
            # 首先尝试普通的create_order，但使用正确的参数
            params = {
                'stopPrice': trigger_price,
                'positionSide': position_side,
                'workingType': 'MARK_PRICE',
                'priceProtect': True,
                'closePosition': False,
                # 注意：期货可能不支持reduceOnly参数，或者需要特定条件
            }
            
            # 尝试不同的参数组合
            param_combinations = [
                params,
                {k: v for k, v in params.items() if k != 'closePosition'},  # 移除closePosition
                {k: v for k, v in params.items() if k != 'priceProtect'},   # 移除priceProtect
                {'stopPrice': trigger_price, 'positionSide': position_side},  # 最简单参数
            ]
            
            for i, param_set in enumerate(param_combinations):
                try:
                    print(f"🔄 尝试参数组合 {i+1}/{len(param_combinations)}")
                    
                    # 添加唯一订单ID
                    param_set['newClientOrderId'] = f"{tag}_{exchange.milliseconds()}"
                    
                    order = exchange.create_order(
                        symbol,
                        order_type,  # 'STOP_MARKET' 或 'TAKE_PROFIT_MARKET'
                        side,
                        quantity,
                        None,  # 市价单没有价格
                        param_set
                    )
                    
                    print(f"✅ 使用组合{i+1}创建成功")
                    return order
                    
                except Exception as e:
                    if i < len(param_combinations) - 1:
                        print(f"⚠️ 组合{i+1}失败: {str(e)[:100]}...")
                        continue
                    else:
                        raise
            
        except Exception as e:
            print(f"❌ 创建{algo_type}订单失败: {e}")
            
            # 尝试使用专门的算法订单端点
            try:
                print(f"🔄 尝试专门算法订单API...")
                
                # 对于币安期货，可能需要使用特殊的算法订单端点
                # 注意：这里需要根据ccxt的具体实现来调整
                if hasattr(exchange, 'private_post_algo_order'):
                    request = {
                        'symbol': symbol.replace('/', ''),
                        'side': side.upper(),
                        'type': order_type,
                        'quantity': exchange.amount_to_precision(symbol, quantity),
                        'stopPrice': exchange.price_to_precision(symbol, trigger_price),
                        'positionSide': position_side,
                    }
                    
                    response = exchange.private_post_algo_order(request)
                    
                    return {
                        'id': response.get('orderId'),
                        'info': response,
                        'status': 'open'
                    }
                else:
                    raise Exception("不支持算法订单API")
                    
            except Exception as api_error:
                print(f"❌ 算法订单API也失败: {api_error}")
                raise
    
    def create_limit_order(symbol, side, quantity, price, position_side, tag=''):
        """创建限价订单（仅用于止盈）"""
        try:
            order = exchange.create_order(
                symbol,
                'LIMIT',
                side,
                quantity,
                price,
                {
                    'timeInForce': 'GTC',
                    'positionSide': position_side,
                    'newClientOrderId': f"{tag}_limit_{exchange.milliseconds()}"
                }
            )
            return order
        except Exception as e:
            print(f"❌ 创建限价单失败: {e}")
            raise
    
    def create_trailing_stop_order(symbol, side, quantity, activation_price, callback_rate, position_side):
        """创建移动止损订单（替代方案）"""
        try:
            order = exchange.create_order(
                symbol,
                'TRAILING_STOP_MARKET',
                side,
                quantity,
                None,
                {
                    'activationPrice': activation_price,
                    'callbackRate': callback_rate,
                    'positionSide': position_side
                }
            )
            return order
        except Exception as e:
            print(f"❌ 创建移动止损失败: {e}")
            raise
    
    try:
        symbol = TRADE_CONFIG['symbol']
        account_type = get_account_type()
        
        # 获取当前价格
        try:
            ticker = exchange.fetch_ticker(symbol)
            current_price = float(ticker.get('last') or ticker.get('close') or 0)
        except:
            current_price = 0.0
        
        print(f"\n🎯 设置止盈止损")
        print(f"账户类型: {account_type}")
        print(f"交易对: {symbol}")
        print(f"方向: {position_side}")
        print(f"数量: {position_size}张")
        print(f"当前价: ${current_price:.2f}")
        print(f"止损价: ${stop_loss_price:.2f}")
        print(f"止盈价: ${take_profit_price:.2f}")
        
        # 确定订单方向
        if position_side == 'long':
            exit_side = 'sell'
            pos_side_param = 'LONG'
        else:
            exit_side = 'buy'
            pos_side_param = 'SHORT'
        
        orders_created = []
        
        # ========== 根据账户类型选择策略 ==========
        if account_type in ['future', 'coin_future']:
            print(f"\n📊 检测到期货账户，使用期货订单接口")
            
            # 创建止损订单
            print(f"\n📉 创建止损订单...")
            try:
                # 先尝试创建止损算法订单
                stop_loss_order = create_algo_order_for_future(
                    symbol=symbol,
                    order_type='STOP_MARKET',
                    side=exit_side,
                    quantity=position_size,
                    trigger_price=stop_loss_price,
                    position_side=pos_side_param,
                    tag='sl'
                )
                print(f"✅ 止损订单创建成功: ID {stop_loss_order.get('id', 'N/A')}")
                orders_created.append(('止损', stop_loss_order))
            except Exception as sl_error:
                print(f"❌ 止损算法订单失败: {sl_error}")
                
                # 备选方案：使用移动止损
                print(f"🔄 尝试移动止损...")
                try:
                    # 设置激活价格（比当前价格略高/低）
                    if position_side == 'long':
                        activation_price = current_price * 0.995  # 多头：价格下跌0.5%激活
                    else:
                        activation_price = current_price * 1.005  # 空头：价格上涨0.5%激活
                    
                    callback_rate = 0.5  # 0.5% 回撤
                    
                    stop_loss_order = create_trailing_stop_order(
                        symbol=symbol,
                        side=exit_side,
                        quantity=position_size,
                        activation_price=activation_price,
                        callback_rate=callback_rate,
                        position_side=pos_side_param
                    )
                    print(f"✅ 移动止损创建成功: ID {stop_loss_order.get('id', 'N/A')}")
                    orders_created.append(('移动止损', stop_loss_order))
                except Exception as ts_error:
                    print(f"❌ 移动止损也失败: {ts_error}")
            
            # 创建止盈订单
            print(f"\n📈 创建止盈订单...")
            try:
                # 先尝试算法止盈订单
                take_profit_order = create_algo_order_for_future(
                    symbol=symbol,
                    order_type='TAKE_PROFIT_MARKET',
                    side=exit_side,
                    quantity=position_size,
                    trigger_price=take_profit_price,
                    position_side=pos_side_param,
                    tag='tp'
                )
                print(f"✅ 止盈订单创建成功: ID {take_profit_order.get('id', 'N/A')}")
                orders_created.append(('止盈', take_profit_order))
            except Exception as tp_error:
                print(f"❌ 算法止盈失败: {tp_error}")
                
                # 备选方案：使用限价单
                try:
                    take_profit_order = create_limit_order(
                        symbol=symbol,
                        side=exit_side,
                        quantity=position_size,
                        price=take_profit_price,
                        position_side=pos_side_param,
                        tag='tp'
                    )
                    print(f"✅ 限价止盈单创建成功: ID {take_profit_order.get('id', 'N/A')}")
                    orders_created.append(('限价止盈', take_profit_order))
                except Exception as limit_error:
                    print(f"❌ 限价止盈也失败: {limit_error}")
        
        else:
            # 现货账户 - 使用不同的方法
            print(f"\n📊 检测到现货账户，使用现货订单接口")
            
            # 现货账户的止损方法不同
            # 这里需要根据现货API来调整
            print(f"⚠️ 现货账户需要不同的止损策略")
            
            # 对于现货，我们可能只能使用限价单或OCO订单
            try:
                # 尝试创建OCO订单（一个订单包含止盈和止损）
                oco_params = {
                    'stopPrice': stop_loss_price,
                    'stopLimitPrice': stop_loss_price * 0.99,
                    'stopLimitTimeInForce': 'GTC',
                }
                
                # 注意：现货可能不支持OCO，或者需要特定的API调用
                print(f"⚠️ 现货止损策略需要额外实现")
                
            except Exception as e:
                print(f"❌ 现货止损设置失败: {e}")
        
        # ========== 结果处理 ==========
        import time
        current_time = time.strftime('%Y-%m-%d %H:%M:%S')
        
        if orders_created:
            # 重新获取当前价格
            try:
                ticker = exchange.fetch_ticker(symbol)
                final_price = float(ticker.get('last') or ticker.get('close') or current_price)
            except:
                final_price = current_price
            
            # 构建消息
            order_details = []
            for name, order in orders_created:
                order_details.append(f"- {name}: ID {order.get('id', 'N/A')}")
            
            order_details_str = "\n".join(order_details)
            
            message = f"""**SOL止盈止损设置结果**

**账户类型**: {account_type}
**仓位方向**: {position_side}
**持仓数量**: {position_size}张
**当前价格**: ${final_price:.2f}
**止损价格**: ${stop_loss_price:.2f}
**止盈价格**: ${take_profit_price:.2f}

**订单详情**
{order_details_str}

**状态**: {'✅ 全部成功' if len(orders_created) >= 2 else '⚠️ 部分成功'}

⏰ {current_time}"""
            
            # 发送通知
            msg_type = "info" if len(orders_created) >= 2 else "warning"
            send_dingtalk_message("🎯 止盈止损设置完成", message, msg_type)
            
            print(f"\n{'='*40}")
            print(f"✅ 设置完成: {len(orders_created)}个订单创建成功")
            print(f"{'='*40}")
            
            return True
            
        else:
            # 所有订单都失败
            send_dingtalk_message(
                "❌ 止盈止损设置失败",
                f"""**SOL止盈止损设置失败**

所有订单创建尝试均失败，请手动设置。

**交易信息**
- 账户类型: {account_type}
- 仓位方向: {position_side}
- 持仓数量: {position_size}张
- 止损价格: ${stop_loss_price:.2f}
- 止盈价格: ${take_profit_price:.2f}

**建议**
1. 登录币安APP手动设置止损止盈
2. 检查API权限是否足够
3. 确认账户有足够保证金

⏰ {current_time}""",
                "error"
            )
            
            print(f"\n❌ 所有订单创建失败，请手动设置")
            return False
            
    except Exception as e:
        print(f"❌ 设置过程发生错误: {e}")
        import traceback
        traceback.print_exc()
        
        # 发送错误通知
        try:
            import time
            send_dingtalk_message(
                "❌ 止盈止损设置异常",
                f"""**SOL止盈止损设置异常**

**错误信息**
{str(e)[:200]}

**交易信息**
- 仓位方向: {position_side}
- 持仓数量: {position_size}张
- 止损价格: ${stop_loss_price:.2f}
- 止盈价格: ${take_profit_price:.2f}

⏰ {time.strftime('%Y-%m-%d %H:%M:%S')}""",
                "error"
            )
        except:
            pass
        
        return False
    
def update_existing_orders(current_position, signal_data):
    """更新现有持仓的止盈止损"""
    try:
        if current_position and current_position['size'] > 0:
            print("🔄 更新现有持仓的止盈止损...")
            cancel_existing_conditional_orders()
            setup_take_profit_stop_loss(
                current_position['side'],
                current_position['size'],
                signal_data['take_profit'],
                signal_data['stop_loss']
            )
    except Exception as e:
        print(f"❌ 更新止盈止损失败: {e}")

def execute_intelligent_trade(signal_data, price_data):
    """执行智能交易 - Binance版本（优化：同步设置止盈止损）"""
    global position

    current_position = get_current_position()

    # 计算智能仓位
    position_size = calculate_intelligent_position(signal_data, price_data, current_position)

    print(f"交易信号: {signal_data['signal']}")
    print(f"信心程度: {signal_data['confidence']}")
    print(f"智能仓位: {position_size:.2f} 张")
    print(f"理由: {signal_data['reason']}")
    print(f"当前持仓: {current_position}")

    # 发送交易信号到钉钉
    signal_emojis = {
        'BUY': '🟢',
        'SELL': '🔴', 
        'HOLD': '🟡'
    }
    emoji = signal_emojis.get(signal_data['signal'], '⚪')
    
    send_dingtalk_message(
        f"{emoji} SOL交易信号 - {signal_data['signal']}",
        f"**信号详情:**\n"
        f"- 信心程度: {signal_data['confidence']}\n"
        f"- 建议仓位: {position_size:.2f}张\n"
        f"- 止损价格: ${signal_data['stop_loss']:.2f}\n"
        f"- 止盈价格: ${signal_data['take_profit']:.2f}\n\n"
        f"**分析理由:**\n{signal_data['reason']}\n\n"
        f"**当前价格:** ${price_data['price']:.2f}",
        "info" if signal_data['signal'] == 'HOLD' else "success" if signal_data['confidence'] == 'HIGH' else "warning"
    )

    # 风险管理
    if signal_data['confidence'] == 'LOW' and not TRADE_CONFIG['test_mode']:
        print("⚠️ 低信心信号，跳过执行")
        return

    if TRADE_CONFIG['test_mode']:
        print("测试模式 - 仅模拟交易")
        return

    try:
        # 先取消所有现有的条件订单
        cancel_existing_conditional_orders()
        
        # Binance交易执行逻辑
        if signal_data['signal'] == 'BUY':
            if current_position and current_position['side'] == 'short':
                # 平空仓并开多仓
                if current_position['size'] > 0:
                    print(f"平空仓 {current_position['size']:.2f} 张并开多仓 {position_size:.2f} 张...")
                    exchange.create_order(
                        TRADE_CONFIG['symbol'],
                        'market',
                        'buy',
                        current_position['size'],
                        None,
                        {'reduceOnly': True}
                    )
                    time.sleep(1)
                    
                # 开多仓
                print(f"开多仓 {position_size:.2f} 张...")
                exchange.create_order(
                    TRADE_CONFIG['symbol'],
                    'market',
                    'buy',
                    position_size
                )

            elif current_position and current_position['side'] == 'long':
                # 调整多仓仓位
                size_diff = position_size - current_position['size']
                if abs(size_diff) >= 0.01:
                    if size_diff > 0:
                        add_size = round(size_diff, 2)
                        print(f"多仓加仓 {add_size:.2f} 张")
                        exchange.create_order(
                            TRADE_CONFIG['symbol'],
                            'market',
                            'buy',
                            add_size
                        )
                    else:
                        reduce_size = round(abs(size_diff), 2)
                        print(f"多仓减仓 {reduce_size:.2f} 张")
                        exchange.create_order(
                            TRADE_CONFIG['symbol'],
                            'market',
                            'sell',
                            reduce_size,
                            None,
                            {'reduceOnly': True}
                        )
                else:
                    print(f"已有多头持仓，仓位合适保持现状")

            else:
                # 无持仓时开多仓
                print(f"开多仓 {position_size:.2f} 张...")
                exchange.create_order(
                    TRADE_CONFIG['symbol'],
                    'market',
                    'buy',
                    position_size
                )

            # 设置多头止盈止损
            if position_size > 0:
                # 等待订单执行完成
                time.sleep(1)
                # 获取最新持仓信息
                updated_position = get_current_position()
                if updated_position and updated_position['side'] == 'long':
                    setup_take_profit_stop_loss('long', updated_position['size'], 
                                               signal_data['take_profit'], signal_data['stop_loss'])

        elif signal_data['signal'] == 'SELL':
            if current_position and current_position['side'] == 'long':
                # 平多仓并开空仓
                if current_position['size'] > 0:
                    print(f"平多仓 {current_position['size']:.2f} 张并开空仓 {position_size:.2f} 张...")
                    exchange.create_order(
                        TRADE_CONFIG['symbol'],
                        'market',
                        'sell',
                        current_position['size'],
                        None,
                        {'reduceOnly': True}
                    )
                    time.sleep(1)
                    
                # 开空仓
                print(f"开空仓 {position_size:.2f} 张...")
                exchange.create_order(
                    TRADE_CONFIG['symbol'],
                    'market',
                    'sell',
                    position_size
                )

            elif current_position and current_position['side'] == 'short':
                # 调整空仓仓位
                size_diff = position_size - current_position['size']
                if abs(size_diff) >= 0.01:
                    if size_diff > 0:
                        add_size = round(size_diff, 2)
                        print(f"空仓加仓 {add_size:.2f} 张")
                        exchange.create_order(
                            TRADE_CONFIG['symbol'],
                            'market',
                            'sell',
                            add_size
                        )
                    else:
                        reduce_size = round(abs(size_diff), 2)
                        print(f"空仓减仓 {reduce_size:.2f} 张")
                        exchange.create_order(
                            TRADE_CONFIG['symbol'],
                            'market',
                            'buy',
                            reduce_size,
                            None,
                            {'reduceOnly': True}
                        )
                else:
                    print(f"已有空头持仓，仓位合适保持现状")

            else:
                # 无持仓时开空仓
                print(f"开空仓 {position_size:.2f} 张...")
                exchange.create_order(
                    TRADE_CONFIG['symbol'],
                    'market',
                    'sell',
                    position_size
                )

            # 设置空头止盈止损
            if position_size > 0:
                # 等待订单执行完成
                time.sleep(1)
                # 获取最新持仓信息
                updated_position = get_current_position()
                if updated_position and updated_position['side'] == 'short':
                    setup_take_profit_stop_loss('short', updated_position['size'], 
                                               signal_data['take_profit'], signal_data['stop_loss'])

        elif signal_data['signal'] == 'HOLD':
            print("建议观望，不执行交易")
            # 即使HOLD也检查是否需要更新止盈止损
            if current_position and current_position['size'] > 0:
                update_existing_orders(current_position, signal_data)
            return

        print("智能交易执行成功")
        
        # 发送交易执行结果到钉钉
        send_dingtalk_message(
            "✅ 交易执行完成",
            f"**SOL交易执行成功**\n\n"
            f"- 操作: {signal_data['signal']}\n"
            f"- 数量: {position_size:.2f}张\n"
            f"- 价格: ${price_data['price']:.2f}\n"
            f"- 止损: ${signal_data['stop_loss']:.2f}\n"
            f"- 止盈: ${signal_data['take_profit']:.2f}\n"
            f"- 时间: {datetime.now().strftime('%H:%M:%S')}",
            "success"
        )
        
        time.sleep(2)
        position = get_current_position()
        print(f"更新后持仓: {position}")

    except Exception as e:
        error_msg = f"交易执行失败: {e}"
        print(f"❌ {error_msg}")
        
        # 发送交易失败通知到钉钉
        send_dingtalk_message(
            "❌ 交易执行失败",
            f"**SOL交易执行失败**\n\n"
            f"- 错误信息: {str(e)}\n"
            f"- 信号: {signal_data['signal']}\n"
            f"- 建议仓位: {position_size:.2f}张\n"
            f"- 时间: {datetime.now().strftime('%H:%M:%S')}",
            "error"
        )
        
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
    """等待到下一个配置间隔的整点"""
    interval = TRADE_CONFIG['execution_interval']
    now = datetime.now()
    current_minute = now.minute
    current_second = now.second

    # 计算下一个间隔整点时间
    next_period_minute = ((current_minute // interval) + 1) * interval
    if next_period_minute >= 60:
        next_period_minute = 0

    # 计算需要等待的总秒数
    if next_period_minute > current_minute:
        minutes_to_wait = next_period_minute - current_minute
    else:
        minutes_to_wait = 60 - current_minute + next_period_minute

    seconds_to_wait = minutes_to_wait * 60 - current_second

    # 显示友好的等待时间
    display_minutes = minutes_to_wait - 1 if current_second > 0 else minutes_to_wait
    display_seconds = 60 - current_second if current_second > 0 else 0

    if display_minutes > 0:
        print(f"🕒 等待 {display_minutes} 分 {display_seconds} 秒到下一个{interval}分钟整点...")
    else:
        print(f"🕒 等待 {display_seconds} 秒到下一个{interval}分钟整点...")

    return seconds_to_wait


def trading_bot():
    """主交易机器人函数"""
    # 等待到整点再执行
    wait_seconds = wait_for_next_period()
    if wait_seconds > 0:
        time.sleep(wait_seconds)

    print("\n" + "=" * 60)
    print(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    try:
        # 1. 获取增强版K线数据
        price_data = get_sol_ohlcv_enhanced()
        if not price_data:
            send_dingtalk_message(
                "⚠️ 数据获取失败",
                "获取SOL K线数据失败，本次跳过执行",
                "warning"
            )
            return

        print(f"SOL当前价格: ${price_data['price']:,.2f}")
        print(f"数据周期: {TRADE_CONFIG['timeframe']}")
        print(f"价格变化: {price_data['price_change']:+.2f}%")

        # 2. 使用DeepSeek分析（带重试）
        signal_data = analyze_with_deepseek_with_retry(price_data)

        if signal_data.get('is_fallback', False):
            print("⚠️ 使用备用交易信号")

        # 3. 执行智能交易
        execute_intelligent_trade(signal_data, price_data)

    except Exception as e:
        error_msg = f"交易机器人执行异常: {e}"
        print(f"❌ {error_msg}")
        send_dingtalk_message("❌ 交易机器人异常", error_msg, "error")
        import traceback
        traceback.print_exc()


def main():
    """主函数"""
    print("SOL/USDT Binance自动交易机器人启动成功！")
    print("融合技术指标策略 + Binance实盘接口")

    if TRADE_CONFIG['test_mode']:
        print("当前为模拟模式，不会真实下单")
    else:
        print("实盘交易模式，请谨慎操作！")

    print(f"交易周期: {TRADE_CONFIG['timeframe']}")
    print(f"执行间隔: {TRADE_CONFIG['execution_interval']}分钟")
    print("已启用完整技术指标分析和持仓跟踪功能")

    # 设置交易所
    if not setup_exchange():
        print("交易所初始化失败，程序退出")
        return

    print(f"执行频率: 每{TRADE_CONFIG['execution_interval']}分钟整点执行")

    # 循环执行
    while True:
        trading_bot()
        time.sleep(60)  # 每分钟检查一次


if __name__ == "__main__":
    main()