"""
币安(Binance) SOL量化交易机器人 - 增强版
功能特性：
1. 针对SOL/USDT永续合约交易
2. 智能仓位控制（解决contractSize为None的问题）
3. 智能止盈止损设置
4. 可配置的执行时间间隔
5. 优化的量化交易分析提示词
6. 钉钉关键信息通知
7. 清晰的代码结构和充分注释
"""

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

# 加载环境变量
load_dotenv()

class BinanceSOLTradingBot:
    """币安SOL量化交易机器人主类"""
    
    def __init__(self):
        """初始化交易机器人"""
        self.setup_config()
        self.setup_clients()
        self.setup_globals()
        
    def setup_config(self):
        """配置交易参数"""
        self.TRADE_CONFIG = {
            # 交易对配置 - 改为SOL/USDT
            'symbol': 'SOL/USDT',
            'leverage': 5,  # 杠杆倍数(SOL波动较大，降低杠杆)
            'timeframe': '15m',  # K线周期
            'execution_interval': 5,  # 执行间隔(分钟)
            
            # 数据配置
            'data_points': 192,  # 数据点数量(48小时)
            'test_mode': False,  # 测试模式
            
            # SOL特定参数
            'sol_config': {
                'base_quantity': 1.0,  # 基础交易数量(SOL个数)
                'min_quantity': 0.1,   # 最小交易数量
                'price_precision': 3,  # 价格精度
                'quantity_precision': 2,  # 数量精度
            },
            
            # 技术指标周期
            'analysis_periods': {
                'short_term': 20,   # 短期均线
                'medium_term': 50,  # 中期均线  
                'long_term': 96     # 长期趋势
            },
            
            # 智能仓位管理
            'position_management': {
                'enable_intelligent_position': True,
                'base_usdt_amount': 100,  # 基础USDT投入(SOL价格较低)
                'high_confidence_multiplier': 1.5,
                'medium_confidence_multiplier': 1.0,
                'low_confidence_multiplier': 0.5,
                'max_position_ratio': 0.1,  # 最大仓位比例(10%)
                'trend_strength_multiplier': 1.2
            },
            
            # 风险管理参数
            'risk_management': {
                'default_stop_loss_ratio': 0.03,   # 默认止损比例3%(SOL波动较大)
                'default_take_profit_ratio': 0.06, # 默认止盈比例6%
                'trailing_stop_enabled': False,    # 是否启用移动止损
                'max_daily_loss_ratio': 0.05       # 最大日亏损比例5%
            },
            
            # 钉钉通知配置
            'dingtalk': {
                'enabled': True,
                'webhook': os.getenv('DINGTALK_WEBHOOK'),
                'secret': os.getenv('DINGTALK_SECRET'),
                'only_important': True  # 仅重要通知
            }
        }
        
    def setup_clients(self):
        """初始化API客户端"""
        try:
            # 初始化DeepSeek客户端
            self.deepseek_client = OpenAI(
                api_key=os.getenv('DEEPSEEK_API_KEY'),
                base_url="https://api.deepseek.com"
            )
            
            # 初始化币安交易所
            self.exchange = ccxt.binance({
                'options': {
                    'defaultType': 'future',  # 币安永续合约
                },
                'apiKey': os.getenv('BINANCE_API_KEY'),
                'secret': os.getenv('BINANCE_SECRET'),
                'sandbox': self.TRADE_CONFIG['test_mode'],  # 测试模式
            })
            
            print("✅ API客户端初始化成功")
            
        except Exception as e:
            print(f"❌ API客户端初始化失败: {e}")
            raise
    
    def setup_globals(self):
        """初始化全局变量"""
        self.price_history = []      # 价格历史
        self.signal_history = []     # 信号历史  
        self.position = None         # 当前持仓
        self.daily_pnl = 0           # 当日盈亏
        self.last_trade_time = None  # 上次交易时间
        
    def setup_exchange(self):
        """设置交易所参数"""
        try:
            print("🔍 设置币安交易所参数...")
            
            # 加载市场数据
            markets = self.exchange.load_markets()
            symbol = self.TRADE_CONFIG['symbol']
            
            if symbol not in markets:
                raise Exception(f"交易对 {symbol} 不存在")
                
            # 获取SOL合约信息
            market = markets[symbol]
            print(f"✅ 交易对信息: {symbol}")
            
            # 🆕 优化：币安contractSize为None，使用自定义逻辑
            self.TRADE_CONFIG['min_amount'] = market['limits']['amount']['min']
            
            # 🆕 更安全的精度获取
            price_precision = market.get('precision', {}).get('price', 3)
            amount_precision = market.get('precision', {}).get('amount', 2)
            
            # 如果精度信息不完整，使用默认值
            if price_precision is None:
                price_precision = 3
            if amount_precision is None:
                amount_precision = 2
                
            self.TRADE_CONFIG['price_precision'] = price_precision
            self.TRADE_CONFIG['amount_precision'] = amount_precision
            
            print(f"📏 最小交易量: {self.TRADE_CONFIG['min_amount']} SOL")
            print(f"🎯 价格精度: {self.TRADE_CONFIG['price_precision']}")
            print(f"🎯 数量精度: {self.TRADE_CONFIG['amount_precision']}")
            
            # 设置杠杆
            print(f"⚙️ 设置杠杆: {self.TRADE_CONFIG['leverage']}x")
            self.exchange.set_leverage(
                self.TRADE_CONFIG['leverage'],
                symbol
            )
            
            # 设置保证金模式 (币安默认全仓)
            print("💰 设置全仓保证金模式")
            try:
                self.exchange.set_margin_mode('cross', symbol)
            except Exception as e:
                print(f"⚠️ 设置保证金模式警告: {e}")
            
            # 验证账户余额
            balance = self.exchange.fetch_balance()
            usdt_balance = balance['USDT']['free']
            print(f"💰 当前USDT余额: {usdt_balance:.2f}")
            
            # 检查当前持仓
            current_pos = self.get_current_position()
            if current_pos:
                print(f"📦 当前持仓: {current_pos['side']}仓 {current_pos['size']} SOL")
            else:
                print("📦 当前无持仓")
                
            print("🎯 交易所设置完成")
            return True
            
        except Exception as e:
            print(f"❌ 交易所设置失败: {e}")
            import traceback
            traceback.print_exc()
            return False

    def send_dingtalk_message(self, title, message, is_important=False):
        """
        发送钉钉通知
        
        Args:
            title: 通知标题
            message: 通知内容
            is_important: 是否重要通知
        """
        try:
            config = self.TRADE_CONFIG['dingtalk']
            
            # 检查是否启用通知
            if not config['enabled']:
                return
                
            # 如果设置为仅重要通知且当前不是重要通知，则跳过
            if config['only_important'] and not is_important:
                return
                
            webhook = config['webhook']
            secret = config['secret']
            
            if not webhook:
                print("⚠️ 钉钉webhook未配置")
                return
            
            timestamp = str(round(time.time() * 1000))
            secret_enc = secret.encode('utf-8')
            string_to_sign = f'{timestamp}\n{secret}'
            string_to_sign_enc = string_to_sign.encode('utf-8')
            hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            
            # 构建消息内容
            data = {
                "msgtype": "markdown",
                "markdown": {
                    "title": title,
                    "text": f"## {title}\n\n{message}\n\n> 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                }
            }
            
            # 发送请求
            url = f"{webhook}&timestamp={timestamp}&sign={sign}"
            response = requests.post(url, json=data, timeout=10)
            
            if response.status_code == 200:
                print("✅ 钉钉通知发送成功")
            else:
                print(f"❌ 钉钉通知发送失败: {response.status_code}")
                
        except Exception as e:
            print(f"❌ 钉钉通知异常: {e}")

    def calculate_intelligent_position(self, signal_data, price_data, current_position):
        """
        🆕 优化版：计算智能仓位大小 - 针对SOL优化
        
        Args:
            signal_data: 信号数据
            price_data: 价格数据
            current_position: 当前持仓
            
        Returns:
            仓位数量(SOL个数)
        """
        config = self.TRADE_CONFIG['position_management']
        sol_config = self.TRADE_CONFIG['sol_config']

        # 如果禁用智能仓位，使用固定仓位
        if not config.get('enable_intelligent_position', True):
            fixed_quantity = sol_config['base_quantity']
            print(f"🔧 智能仓位已禁用，使用固定仓位: {fixed_quantity} SOL")
            return fixed_quantity

        try:
            # 获取账户余额
            balance = self.exchange.fetch_balance()
            usdt_balance = balance['USDT']['free']
            
            # 检查余额有效性
            if usdt_balance <= 0:
                print("⚠️ 账户USDT余额不足，使用基础仓位")
                return sol_config['base_quantity']

            # 基础USDT投入
            base_usdt = config['base_usdt_amount']
            print(f"💰 可用USDT余额: {usdt_balance:.2f}, 下单基数: {base_usdt} USDT")

            # 根据信心程度调整
            confidence_multiplier = {
                'HIGH': config['high_confidence_multiplier'],
                'MEDIUM': config['medium_confidence_multiplier'],
                'LOW': config['low_confidence_multiplier']
            }.get(signal_data.get('confidence', 'MEDIUM'), 1.0)

            # 根据趋势强度调整
            trend = price_data.get('trend_analysis', {}).get('overall', '震荡整理')
            if trend in ['强势上涨', '强势下跌']:
                trend_multiplier = config['trend_strength_multiplier']
            else:
                trend_multiplier = 1.0

            # 根据RSI状态调整（超买超卖区域减仓）
            rsi = price_data.get('technical_data', {}).get('rsi', 50)
            if isinstance(rsi, (int, float)):
                if rsi > 75 or rsi < 25:
                    rsi_multiplier = 0.7
                else:
                    rsi_multiplier = 1.0
            else:
                rsi_multiplier = 1.0

            # 计算建议投入USDT金额
            suggested_usdt = base_usdt * confidence_multiplier * trend_multiplier * rsi_multiplier

            # 风险管理：不超过总资金的指定比例
            max_usdt = usdt_balance * config['max_position_ratio']
            final_usdt = min(suggested_usdt, max_usdt)
            
            # 确保最小投入金额
            min_usdt = 5  # 最小投入5 USDT
            if final_usdt < min_usdt:
                final_usdt = min_usdt
                print(f"⚠️ 投入金额小于最小值，调整为: {final_usdt} USDT")

            # 🆕 优化：计算SOL数量（基于USDT价值和当前价格）
            current_price = price_data.get('price', 0)
            if current_price <= 0:
                print("❌ 当前价格无效，使用基础仓位")
                return sol_config['base_quantity']
                
            # 公式：SOL数量 = 投入USDT / 当前SOL价格
            sol_quantity = final_usdt / current_price
            
            # 精度处理
            amount_precision = self.TRADE_CONFIG.get('amount_precision', 2)
            sol_quantity = round(sol_quantity, amount_precision)

            # 确保最小交易量
            min_quantity = sol_config['min_quantity']
            if sol_quantity < min_quantity:
                sol_quantity = min_quantity
                print(f"⚠️ 仓位小于最小值，调整为: {sol_quantity} SOL")
                
            # 确保不超过最大仓位限制（基于账户余额）
            max_quantity_from_balance = (usdt_balance * config['max_position_ratio']) / current_price
            max_quantity_from_balance = round(max_quantity_from_balance, amount_precision)
            if sol_quantity > max_quantity_from_balance:
                sol_quantity = max_quantity_from_balance
                print(f"⚠️ 仓位超过最大限制，调整为: {sol_quantity} SOL")

            print(f"📊 仓位计算详情:")
            print(f"   - 基础USDT: {base_usdt}")
            print(f"   - 信心倍数: {confidence_multiplier}")
            print(f"   - 趋势倍数: {trend_multiplier}")
            print(f"   - RSI倍数: {rsi_multiplier}")
            print(f"   - 建议USDT: {suggested_usdt:.2f}")
            print(f"   - 最终USDT: {final_usdt:.2f}")
            print(f"   - 当前SOL价格: {current_price:.3f}")
            print(f"   - 计算数量: {sol_quantity:.3f} SOL")
            print(f"   - 最大允许数量: {max_quantity_from_balance:.3f} SOL")

            print(f"🎯 最终仓位: {final_usdt:.2f} USDT → {sol_quantity:.3f} SOL")
            return sol_quantity

        except Exception as e:
            print(f"❌ 仓位计算失败，使用基础仓位: {e}")
            import traceback
            traceback.print_exc()  # 打印详细错误信息
            # 紧急备用计算
            return sol_config['base_quantity']

    def calculate_technical_indicators(self, df):
        """
        计算技术指标
        
        Args:
            df: K线数据DataFrame
            
        Returns:
            添加技术指标后的DataFrame
        """
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
            print(f"❌ 技术指标计算失败: {e}")
            return df

    def get_market_trend(self, df):
        """
        判断市场趋势
        
        Args:
            df: 包含技术指标的DataFrame
            
        Returns:
            趋势分析字典
        """
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
            print(f"❌ 趋势分析失败: {e}")
            return {}

    def get_support_resistance_levels(self, df, lookback=20):
        """
        计算支撑阻力位
        
        Args:
            df: K线数据DataFrame
            lookback: 回溯周期
            
        Returns:
            支撑阻力位字典
        """
        try:
            recent_high = df['high'].tail(lookback).max()
            recent_low = df['low'].tail(lookback).min()
            current_price = df['close'].iloc[-1]
            
            # 动态支撑阻力（基于布林带）
            bb_upper = df['bb_upper'].iloc[-1]
            bb_lower = df['bb_lower'].iloc[-1]
            
            return {
                'static_resistance': recent_high,
                'static_support': recent_low,
                'dynamic_resistance': bb_upper,
                'dynamic_support': bb_lower,
                'price_vs_resistance': ((recent_high - current_price) / current_price) * 100,
                'price_vs_support': ((current_price - recent_low) / recent_low) * 100
            }
            
        except Exception as e:
            print(f"❌ 支撑阻力计算失败: {e}")
            return {}

    def get_btc_ohlcv_enhanced(self):
        """
        获取SOL K线数据并计算技术指标
        
        Returns:
            增强的市场数据字典
        """
        try:
            # 获取K线数据
            ohlcv = self.exchange.fetch_ohlcv(
                self.TRADE_CONFIG['symbol'], 
                self.TRADE_CONFIG['timeframe'],
                limit=self.TRADE_CONFIG['data_points']
            )
            
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

            # 计算技术指标
            df = self.calculate_technical_indicators(df)

            current_data = df.iloc[-1]
            previous_data = df.iloc[-2]

            # 获取技术分析数据
            trend_analysis = self.get_market_trend(df)
            levels_analysis = self.get_support_resistance_levels(df)

            return {
                'price': current_data['close'],
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'high': current_data['high'],
                'low': current_data['low'],
                'volume': current_data['volume'],
                'timeframe': self.TRADE_CONFIG['timeframe'],
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
            print(f"❌ 获取SOL K线数据失败: {e}")
            return None

    def generate_technical_analysis_text(self, price_data):
        """生成技术分析文本"""
        if 'technical_data' not in price_data:
            return "技术指标数据不可用"

        tech = price_data['technical_data']
        trend = price_data.get('trend_analysis', {})
        levels = price_data.get('levels_analysis', {})

        # 检查数据有效性
        def safe_float(value, default=0):
            return float(value) if value and pd.notna(value) else default

        analysis_text = f"""
        【SOL技术指标分析】
        📈 移动平均线:
        - 5周期: {safe_float(tech['sma_5']):.3f} | 价格相对: {(price_data['price'] - safe_float(tech['sma_5'])) / safe_float(tech['sma_5']) * 100:+.2f}%
        - 20周期: {safe_float(tech['sma_20']):.3f} | 价格相对: {(price_data['price'] - safe_float(tech['sma_20'])) / safe_float(tech['sma_20']) * 100:+.2f}%
        - 50周期: {safe_float(tech['sma_50']):.3f} | 价格相对: {(price_data['price'] - safe_float(tech['sma_50'])) / safe_float(tech['sma_50']) * 100:+.2f}%

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
        - 静态阻力: {safe_float(levels.get('static_resistance', 0)):.3f}
        - 静态支撑: {safe_float(levels.get('static_support', 0)):.3f}
        """
        return analysis_text

    def get_current_position(self):
        """
        获取当前持仓情况 - 币安版本
        
        Returns:
            持仓信息字典或None
        """
        try:
            positions = self.exchange.fetch_positions([self.TRADE_CONFIG['symbol']])
            
            for pos in positions:
                if pos['symbol'] == self.TRADE_CONFIG['symbol']:
                    contracts = float(pos['contracts']) if pos['contracts'] else 0
                    
                    if contracts > 0:
                        return {
                            'side': pos['side'],  # 'long' or 'short'
                            'size': contracts,
                            'entry_price': float(pos['entryPrice']) if pos['entryPrice'] else 0,
                            'unrealized_pnl': float(pos['unrealizedPnl']) if pos['unrealizedPnl'] else 0,
                            'leverage': float(pos['leverage']) if pos['leverage'] else self.TRADE_CONFIG['leverage'],
                            'symbol': pos['symbol']
                        }
                        
            return None
            
        except Exception as e:
            print(f"❌ 获取持仓失败: {e}")
            return None

    def set_stop_loss_take_profit(self, side, quantity, stop_loss_price, take_profit_price):
        """
        🆕 设置止盈止损订单
        
        Args:
            side: 方向 ('long' or 'short')
            quantity: 数量
            stop_loss_price: 止损价格
            take_profit_price: 止盈价格
            
        Returns:
            bool: 是否设置成功
        """
        try:
            symbol = self.TRADE_CONFIG['symbol']
            
            # 取消现有的止盈止损订单
            open_orders = self.exchange.fetch_open_orders(symbol)
            for order in open_orders:
                if order['type'] in ['stop_market', 'take_profit_market']:
                    self.exchange.cancel_order(order['id'], symbol)
            
            # 根据持仓方向设置止损止盈
            if side == 'long':
                # 多头持仓：止损卖单，止盈卖单
                if stop_loss_price > 0:
                    self.exchange.create_order(
                        symbol, 'stop_market', 'sell', quantity, None,
                        {'stopPrice': stop_loss_price, 'reduceOnly': True}
                    )
                    print(f"✅ 设置多头止损: {stop_loss_price:.3f}")
                
                if take_profit_price > 0:
                    self.exchange.create_order(
                        symbol, 'take_profit_market', 'sell', quantity, None,
                        {'stopPrice': take_profit_price, 'reduceOnly': True}
                    )
                    print(f"✅ 设置多头止盈: {take_profit_price:.3f}")
                    
            elif side == 'short':
                # 空头持仓：止损买单，止盈买单
                if stop_loss_price > 0:
                    self.exchange.create_order(
                        symbol, 'stop_market', 'buy', quantity, None,
                        {'stopPrice': stop_loss_price, 'reduceOnly': True}
                    )
                    print(f"✅ 设置空头止损: {stop_loss_price:.3f}")
                
                if take_profit_price > 0:
                    self.exchange.create_order(
                        symbol, 'take_profit_market', 'buy', quantity, None,
                        {'stopPrice': take_profit_price, 'reduceOnly': True}
                    )
                    print(f"✅ 设置空头止盈: {take_profit_price:.3f}")
            
            return True
            
        except Exception as e:
            print(f"❌ 设置止盈止损失败: {e}")
            return False

    def safe_json_parse(self, json_str):
        """安全解析JSON，处理格式不规范的情况"""
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            try:
                # 修复常见的JSON格式问题
                json_str = json_str.replace("'", '"')
                json_str = re.sub(r'(\w+):', r'"\1":', json_str)
                json_str = re.sub(r',\s*}', '}', json_str)
                json_str = re.sub(r',\s*]', ']', json_str)
                return json.loads(json_str)
            except json.JSONDecodeError as e:
                print(f"❌ JSON解析失败，原始内容: {json_str}")
                print(f"错误详情: {e}")
                return None

    def create_fallback_signal(self, price_data):
        """创建备用交易信号"""
        return {
            "signal": "HOLD",
            "reason": "因技术分析暂时不可用，采取保守策略",
            "stop_loss": price_data['price'] * (1 - self.TRADE_CONFIG['risk_management']['default_stop_loss_ratio']),
            "take_profit": price_data['price'] * (1 + self.TRADE_CONFIG['risk_management']['default_take_profit_ratio']),
            "confidence": "LOW",
            "is_fallback": True
        }

    def analyze_with_deepseek(self, price_data):
        """
        使用DeepSeek分析SOL市场并生成交易信号
        
        Args:
            price_data: 价格数据
            
        Returns:
            交易信号字典
        """
        # 生成技术分析文本
        technical_analysis = self.generate_technical_analysis_text(price_data)

        # 构建K线数据文本
        kline_text = f"【最近5根{self.TRADE_CONFIG['timeframe']}K线数据】\n"
        for i, kline in enumerate(price_data['kline_data'][-5:]):
            trend = "阳线" if kline['close'] > kline['open'] else "阴线"
            change = ((kline['close'] - kline['open']) / kline['open']) * 100
            kline_text += f"K线{i + 1}: {trend} 开盘:{kline['open']:.3f} 收盘:{kline['close']:.3f} 涨跌:{change:+.2f}%\n"

        # 添加上次交易信号
        signal_text = ""
        if self.signal_history:
            last_signal = self.signal_history[-1]
            signal_text = f"\n【上次交易信号】\n信号: {last_signal.get('signal', 'N/A')}\n信心: {last_signal.get('confidence', 'N/A')}"

        # 添加当前持仓信息
        current_pos = self.get_current_position()
        position_text = "无持仓" if not current_pos else f"{current_pos['side']}仓, 数量: {current_pos['size']:.3f} SOL, 盈亏: {current_pos['unrealized_pnl']:.2f} USDT"

        # 🆕 优化的SOL交易分析提示词
        prompt = f"""
        你是一个专业的加密货币交易分析师，专注于SOL/USDT永续合约交易。请基于以下数据进行分析：

        {kline_text}

        {technical_analysis}

        {signal_text}

        【当前SOL行情】
        - 当前价格: ${price_data['price']:.3f}
        - 时间: {price_data['timestamp']}
        - 本K线最高: ${price_data['high']:.3f}
        - 本K线最低: ${price_data['low']:.3f}
        - 本K线成交量: {price_data['volume']:.2f} SOL
        - 价格变化: {price_data['price_change']:+.2f}%
        - 当前持仓: {position_text}

        【SOL交易特性分析】
        1. **高波动性**: SOL波动性高于BTC，需要更严格的风险控制
        2. **技术指标敏感性**: SOL对技术指标反应更敏感，突破信号更可靠
        3. **趋势持续性**: SOL趋势一旦形成，持续性较好

        【量化交易分析框架 - 必须遵守】
        1. **多因子权重分配**:
           - 趋势分析 (40%): 均线排列、MACD趋势
           - 动量指标 (25%): RSI、成交量确认
           - 价格位置 (20%): 布林带位置、支撑阻力
           - 市场结构 (15%): K线形态、突破确认

        2. **技术指标优先级**:
           - 一级指标: 均线排列 > 趋势线突破
           - 二级指标: RSI背离 > MACD金叉死叉
           - 三级指标: 布林带突破 > 成交量确认

        3. **SOL特定交易逻辑**:
           - 强势突破布林带上轨 + 成交量放大 → 高信心BUY
           - 跌破关键支撑 + RSI超卖反弹 → 高信心SELL
           - 均线多头排列 + MACD金叉 → 中等信心BUY
           - 均线空头排列 + MACD死叉 → 中等信心SELL

        4. **风险管理规则**:
           - RSI > 75: 避免开多，考虑减仓
           - RSI < 25: 避免开空，考虑减仓
           - 布林带位置 > 80%: 警惕回调
           - 布林带位置 < 20%: 关注反弹

        【交易信号生成规则】
        - BUY信号: 至少3个技术指标支持做多，趋势明确向上
        - SELL信号: 至少3个技术指标支持做空，趋势明确向下  
        - HOLD信号: 指标矛盾、趋势不明、或等待更好入场点

        【重要】请基于严谨的技术分析做出明确判断，避免过度交易！

        请用以下JSON格式回复：
        {{
            "signal": "BUY|SELL|HOLD",
            "reason": "详细的技术分析理由，包含具体的指标依据",
            "stop_loss": 具体价格,
            "take_profit": 具体价格, 
            "confidence": "HIGH|MEDIUM|LOW"
        }}
        """

        try:
            response = self.deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "你是专业的SOL交易员，专注于技术分析和风险管理。请结合多时间框架分析做出判断，并严格遵循JSON格式要求。"},
                    {"role": "user", "content": prompt}
                ],
                stream=False,
                temperature=0.1
            )

            # 安全解析JSON
            result = response.choices[0].message.content
            print(f"🤖 DeepSeek原始回复: {result}")

            # 提取JSON部分
            start_idx = result.find('{')
            end_idx = result.rfind('}') + 1

            if start_idx != -1 and end_idx != 0:
                json_str = result[start_idx:end_idx]
                signal_data = self.safe_json_parse(json_str)

                if signal_data is None:
                    signal_data = self.create_fallback_signal(price_data)
            else:
                signal_data = self.create_fallback_signal(price_data)

            # 验证必需字段
            required_fields = ['signal', 'reason', 'stop_loss', 'take_profit', 'confidence']
            if not all(field in signal_data for field in required_fields):
                signal_data = self.create_fallback_signal(price_data)

            # 保存信号到历史记录
            signal_data['timestamp'] = price_data['timestamp']
            self.signal_history.append(signal_data)
            if len(self.signal_history) > 30:
                self.signal_history.pop(0)

            return signal_data

        except Exception as e:
            print(f"❌ DeepSeek分析失败: {e}")
            return self.create_fallback_signal(price_data)

    def execute_intelligent_trade(self, signal_data, price_data):
        """
        执行智能交易 - 币安SOL版本
        
        Args:
            signal_data: 交易信号
            price_data: 价格数据
        """
        current_position = self.get_current_position()

        # 计算智能仓位
        position_size = self.calculate_intelligent_position(signal_data, price_data, current_position)

        print(f"🎯 交易信号: {signal_data['signal']}")
        print(f"📊 信心程度: {signal_data['confidence']}")
        print(f"💼 智能仓位: {position_size:.3f} SOL")
        print(f"📝 理由: {signal_data['reason']}")
        print(f"📦 当前持仓: {current_position}")

        # 风险管理
        if signal_data['confidence'] == 'LOW' and not self.TRADE_CONFIG['test_mode']:
            print("⚠️ 低信心信号，跳过执行")
            return

        if self.TRADE_CONFIG['test_mode']:
            print("🔬 测试模式 - 仅模拟交易")
            return

        try:
            # 🆕 执行交易并设置止盈止损
            if signal_data['signal'] == 'BUY':
                if current_position and current_position['side'] == 'short':
                    # 先平空仓再开多仓
                    print(f"🔄 平空仓 {current_position['size']:.3f} SOL并开多仓 {position_size:.3f} SOL...")
                    
                    # 平空仓
                    self.exchange.create_market_order(
                        self.TRADE_CONFIG['symbol'],
                        'buy',
                        current_position['size'],
                        params={'reduceOnly': True}
                    )
                    time.sleep(1)
                    
                    # 开多仓
                    order = self.exchange.create_market_order(
                        self.TRADE_CONFIG['symbol'],
                        'buy',
                        position_size
                    )
                    
                    # 设置止盈止损
                    self.set_stop_loss_take_profit(
                        'long', position_size, 
                        signal_data['stop_loss'], 
                        signal_data['take_profit']
                    )
                    
                    # 🆕 发送钉钉通知
                    self.send_dingtalk_message(
                        "SOL交易通知 - 开多仓",
                        f"✅ 已开多仓\n"
                        f"数量: {position_size:.3f} SOL\n"
                        f"价格: ${price_data['price']:.3f}\n"
                        f"止损: ${signal_data['stop_loss']:.3f}\n"
                        f"止盈: ${signal_data['take_profit']:.3f}\n"
                        f"理由: {signal_data['reason']}",
                        is_important=True
                    )

                elif current_position and current_position['side'] == 'long':
                    # 同方向调整仓位
                    size_diff = position_size - current_position['size']
                    
                    if abs(size_diff) >= 0.01:  # 有可调整的差异
                        if size_diff > 0:
                            # 加仓
                            add_size = round(size_diff, self.TRADE_CONFIG['amount_precision'])
                            print(f"📈 多仓加仓 {add_size:.3f} SOL")
                            
                            self.exchange.create_market_order(
                                self.TRADE_CONFIG['symbol'],
                                'buy',
                                add_size
                            )
                            
                            # 更新止盈止损
                            self.set_stop_loss_take_profit(
                                'long', position_size, 
                                signal_data['stop_loss'], 
                                signal_data['take_profit']
                            )
                            
                            self.send_dingtalk_message(
                                "SOL交易通知 - 多仓加仓",
                                f"📈 多仓加仓\n"
                                f"加仓数量: {add_size:.3f} SOL\n"
                                f"总仓位: {position_size:.3f} SOL\n"
                                f"当前价格: ${price_data['price']:.3f}",
                                is_important=False
                            )
                        else:
                            # 减仓
                            reduce_size = round(abs(size_diff), self.TRADE_CONFIG['amount_precision'])
                            print(f"📉 多仓减仓 {reduce_size:.3f} SOL")
                            
                            self.exchange.create_market_order(
                                self.TRADE_CONFIG['symbol'],
                                'sell',
                                reduce_size,
                                params={'reduceOnly': True}
                            )
                    else:
                        print(f"✅ 已有多头持仓，仓位合适保持现状")

                else:
                    # 无持仓时开多仓
                    print(f"🟢 开多仓 {position_size:.3f} SOL...")
                    
                    self.exchange.create_market_order(
                        self.TRADE_CONFIG['symbol'],
                        'buy',
                        position_size
                    )
                    
                    # 设置止盈止损
                    self.set_stop_loss_take_profit(
                        'long', position_size, 
                        signal_data['stop_loss'], 
                        signal_data['take_profit']
                    )
                    
                    self.send_dingtalk_message(
                        "SOL交易通知 - 开多仓",
                        f"🟢 新建多仓\n"
                        f"数量: {position_size:.3f} SOL\n"
                        f"价格: ${price_data['price']:.3f}\n"
                        f"止损: ${signal_data['stop_loss']:.3f}\n"
                        f"止盈: ${signal_data['take_profit']:.3f}\n"
                        f"理由: {signal_data['reason']}",
                        is_important=True
                    )

            elif signal_data['signal'] == 'SELL':
                # 类似的空头交易逻辑...
                if current_position and current_position['side'] == 'long':
                    print(f"🔄 平多仓 {current_position['size']:.3f} SOL并开空仓 {position_size:.3f} SOL...")
                    
                    self.exchange.create_market_order(
                        self.TRADE_CONFIG['symbol'],
                        'sell',
                        current_position['size'],
                        params={'reduceOnly': True}
                    )
                    time.sleep(1)
                    
                    self.exchange.create_market_order(
                        self.TRADE_CONFIG['symbol'],
                        'sell',
                        position_size
                    )
                    
                    self.set_stop_loss_take_profit(
                        'short', position_size, 
                        signal_data['stop_loss'], 
                        signal_data['take_profit']
                    )
                    
                    self.send_dingtalk_message(
                        "SOL交易通知 - 开空仓",
                        f"🔴 开空仓\n"
                        f"数量: {position_size:.3f} SOL\n"
                        f"价格: ${price_data['price']:.3f}\n"
                        f"止损: ${signal_data['stop_loss']:.3f}\n"
                        f"止盈: ${signal_data['take_profit']:.3f}\n"
                        f"理由: {signal_data['reason']}",
                        is_important=True
                    )

                else:
                    # 简化处理...
                    print(f"🔴 开空仓 {position_size:.3f} SOL...")
                    self.exchange.create_market_order(
                        self.TRADE_CONFIG['symbol'],
                        'sell',
                        position_size
                    )
                    
                    self.set_stop_loss_take_profit(
                        'short', position_size, 
                        signal_data['stop_loss'], 
                        signal_data['take_profit']
                    )
                    
                    self.send_dingtalk_message(
                        "SOL交易通知 - 开空仓",
                        f"🔴 新建空仓\n"
                        f"数量: {position_size:.3f} SOL\n"
                        f"价格: ${price_data['price']:.3f}\n"
                        f"理由: {signal_data['reason']}",
                        is_important=True
                    )

            elif signal_data['signal'] == 'HOLD':
                print("⏸️ 建议观望，不执行交易")
                return

            print("✅ 智能交易执行成功")
            self.last_trade_time = datetime.now()
            
            time.sleep(2)
            self.position = self.get_current_position()
            print(f"📊 更新后持仓: {self.position}")

        except Exception as e:
            print(f"❌ 交易执行失败: {e}")
            self.send_dingtalk_message(
                "SOL交易异常",
                f"❌ 交易执行失败\n错误: {str(e)}",
                is_important=True
            )

    def analyze_with_deepseek_with_retry(self, price_data, max_retries=2):
        """带重试的DeepSeek分析"""
        for attempt in range(max_retries):
            try:
                signal_data = self.analyze_with_deepseek(price_data)
                if signal_data and not signal_data.get('is_fallback', False):
                    return signal_data

                print(f"第{attempt + 1}次尝试失败，进行重试...")
                time.sleep(1)

            except Exception as e:
                print(f"第{attempt + 1}次尝试异常: {e}")
                if attempt == max_retries - 1:
                    return self.create_fallback_signal(price_data)
                time.sleep(1)

        return self.create_fallback_signal(price_data)

    def wait_for_next_period(self):
        """等待到下一个执行周期"""
        interval = self.TRADE_CONFIG['execution_interval']
        now = datetime.now()
        current_minute = now.minute
        current_second = now.second

        # 计算下一个执行时间
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
            print(f"🕒 等待 {display_minutes} 分 {display_seconds} 秒到下一个执行点...")
        else:
            print(f"🕒 等待 {display_seconds} 秒到下一个执行点...")

        return seconds_to_wait

    def trading_bot(self):
        """主交易机器人函数"""
        # 等待到执行时间
        wait_seconds = self.wait_for_next_period()
        if wait_seconds > 0:
            time.sleep(wait_seconds)

        print("\n" + "=" * 60)
        print(f"🕒 执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)

        # 1. 获取SOL K线数据
        price_data = self.get_btc_ohlcv_enhanced()
        if not price_data:
            self.send_dingtalk_message(
                "SOL数据获取异常",
                "❌ 无法获取SOL市场数据，请检查网络连接",
                is_important=True
            )
            return

        print(f"💰 SOL当前价格: ${price_data['price']:.3f}")
        print(f"📊 数据周期: {self.TRADE_CONFIG['timeframe']}")
        print(f"📈 价格变化: {price_data['price_change']:+.2f}%")

        # 2. 使用DeepSeek分析（带重试）
        signal_data = self.analyze_with_deepseek_with_retry(price_data)

        if signal_data.get('is_fallback', False):
            print("⚠️ 使用备用交易信号")

        # 3. 执行智能交易
        self.execute_intelligent_trade(signal_data, price_data)

    def main(self):
        """主函数"""
        print("🚀 SOL/USDT 币安自动交易机器人启动成功！")
        print("🎯 融合技术指标策略 + 币安实盘接口")
        
        if self.TRADE_CONFIG['test_mode']:
            print("🔬 当前为模拟模式，不会真实下单")
        else:
            print("💰 实盘交易模式，请谨慎操作！")

        print(f"⏰ 交易周期: {self.TRADE_CONFIG['timeframe']}")
        print(f"🔄 执行间隔: {self.TRADE_CONFIG['execution_interval']}分钟")
        print("📊 已启用完整技术指标分析和持仓跟踪功能")

        # 设置交易所
        if not self.setup_exchange():
            print("❌ 交易所初始化失败，程序退出")
            return

        # 发送启动通知
        self.send_dingtalk_message(
            "SOL交易机器人启动",
            "✅ SOL/USDT交易机器人已启动\n"
            f"模式: {'模拟交易' if self.TRADE_CONFIG['test_mode'] else '实盘交易'}\n"
            f"交易周期: {self.TRADE_CONFIG['timeframe']}\n"
            f"执行间隔: {self.TRADE_CONFIG['execution_interval']}分钟",
            is_important=True
        )

        print("🔄 开始执行交易循环...")

        # 循环执行
        while True:
            try:
                self.trading_bot()
                # 执行完后等待一段时间再检查
                time.sleep(60)  # 每分钟检查一次
                
            except KeyboardInterrupt:
                print("\n🛑 用户中断程序")
                self.send_dingtalk_message(
                    "SOL交易机器人停止",
                    "🛑 交易机器人已被手动停止",
                    is_important=True
                )
                break
            except Exception as e:
                print(f"❌ 主循环异常: {e}")
                time.sleep(60)


if __name__ == "__main__":
    bot = BinanceSOLTradingBot()
    bot.main()