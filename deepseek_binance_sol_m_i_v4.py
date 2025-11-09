"""
币安(Binance) SOL量化交易机器人 - 务实优化完整版
核心原则：
1. 简单有效的趋势跟踪策略
2. 严格的仓位管理和风险控制  
3. 减少过度交易和情绪化决策
4. 基于实证的交易逻辑
"""

import os
import time
import schedule
from openai import OpenAI
import ccxt
import pandas as pd
import numpy as np
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
    """币安SOL量化交易机器人 - 务实优化版"""
    
    def __init__(self):
        """初始化交易机器人"""
        self.setup_config()
        self.setup_clients()
        self.setup_globals()
        
    def setup_config(self):
        """配置交易参数 - 务实优化"""
        self.TRADE_CONFIG = {
            'symbol': 'SOL/USDT',
            'leverage': 10,
            'timeframe': '15m',
            'execution_interval': 5,  # 延长执行间隔
            
            # 数据配置
            'data_points': 50,
            'test_mode': False,
            
            # SOL合约交易参数
            'sol_config': {
                'base_quantity': 2.0,
                'min_quantity': 1.0,
                'price_precision': 3,
                'quantity_precision': 1,
            },
            
            # 🆕 核心交易策略配置
            'trading_strategy': {
                'strategy_type': 'trend_following',  # 趋势跟踪
                'entry_conditions': {
                    'trend_confirmation': True,     # 趋势确认
                    'volume_confirmation': True,    # 成交量确认  
                    'rsi_filter': True,             # RSI过滤
                },
                'exit_conditions': {
                    'stop_loss': True,
                    'take_profit': True,
                    'trend_reversal': True,         # 趋势反转退出
                }
            },
            
            # 🆕 务实风险管理
            'risk_management': {
                'max_position_ratio': 0.6,          # 降低最大仓位
                'stop_loss_ratio': 0.02,            # 固定止损2%
                'take_profit_ratio': 0.04,          # 固定止盈4%
                'risk_reward_ratio': 2.0,           # 风险回报比
                'max_daily_loss': 0.1,              # 最大日亏损10%
                'trailing_stop': True,              # 移动止损
            },
            
            # 🆕 仓位持有策略
            'position_holding': {
                'min_hold_periods': 3,              # 最小持有3个周期
                'max_hold_periods': 20,             # 最大持有20个周期
                'reduce_on_weakness': True,         # 弱势减仓
                'add_on_strength': False,           # 强势不加仓（避免追高）
            },
            
            # 钉钉通知配置
            'dingtalk': {
                'enabled': True,
                'webhook': os.getenv('DINGTALK_WEBHOOK'),
                'secret': os.getenv('DINGTALK_SECRET'),
                'only_important': True
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
        self.position_open_time = None  # 🆕 持仓开始时间
        
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
            
            # 优化：币安contractSize为None，使用自定义逻辑
            self.TRADE_CONFIG['min_amount'] = market['limits']['amount']['min']
            
            # 修复：币安返回的是步长（step size），不是小数位数
            precision_info = market.get('precision', {})
            
            # 价格步长处理
            price_step = precision_info.get('price')
            if price_step is None:
                price_step = 0.01  # 默认价格步长
            self.TRADE_CONFIG['price_step'] = float(price_step)
                
            # 数量步长处理
            amount_step = precision_info.get('amount')
            if amount_step is None:
                amount_step = 0.001  # 默认数量步长
            self.TRADE_CONFIG['amount_step'] = float(amount_step)
            
            # 计算对应的小数位数（用于显示）
            price_precision = len(str(price_step).split('.')[-1]) if '.' in str(price_step) else 0
            amount_precision = len(str(amount_step).split('.')[-1]) if '.' in str(amount_step) else 0
            
            self.TRADE_CONFIG['price_precision'] = price_precision
            self.TRADE_CONFIG['amount_precision'] = amount_precision
            
            print(f"📏 最小交易量: {self.TRADE_CONFIG['min_amount']} SOL")
            print(f"🎯 价格步长: {self.TRADE_CONFIG['price_step']} (对应{price_precision}位小数)")
            print(f"🎯 数量步长: {self.TRADE_CONFIG['amount_step']} (对应{amount_precision}位小数)")
            
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
                self.position_open_time = datetime.now()  # 🆕 记录持仓时间
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

    def calculate_core_indicators(self, df):
        """
        🎯 计算核心指标 - 只保留最有效的
        """
        try:
            # 1. 移动平均线 - 趋势判断核心
            df['sma_fast'] = df['close'].rolling(window=12).mean()
            df['sma_slow'] = df['close'].rolling(window=26).mean()
            df['sma_trend'] = df['close'].rolling(window=50).mean()
            
            # 2. RSI - 动量过滤
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss
            df['rsi'] = 100 - (100 / (1 + rs))
            
            # 3. 成交量确认
            df['volume_ma'] = df['volume'].rolling(20).mean()
            df['volume_ratio'] = df['volume'] / df['volume_ma']
            
            # 4. 布林带 - 波动率和位置
            df['bb_middle'] = df['close'].rolling(20).mean()
            bb_std = df['close'].rolling(20).std()
            df['bb_upper'] = df['bb_middle'] + (bb_std * 2)
            df['bb_lower'] = df['bb_middle'] - (bb_std * 2)
            
            return df.bfill().ffill()
            
        except Exception as e:
            print(f"❌ 核心指标计算失败: {e}")
            return df

    def get_simple_trend(self, df):
        """
        🎯 简单的趋势判断
        """
        current = df.iloc[-1]
        price = current['close']
        
        trend_short = "上涨" if price > current.get('sma_fast', price) else "下跌"
        trend_medium = "上涨" if price > current.get('sma_slow', price) else "下跌"
        
        if trend_short == trend_medium == "上涨":
            overall = "上涨趋势"
        elif trend_short == trend_medium == "下跌":
            overall = "下跌趋势" 
        else:
            overall = "震荡整理"
            
        return {
            'short_term': trend_short,
            'medium_term': trend_medium, 
            'overall': overall
        }

    def get_market_data_simple(self):
        """
        🎯 简化的市场数据获取
        """
        try:
            ohlcv = self.exchange.fetch_ohlcv(
                self.TRADE_CONFIG['symbol'], 
                self.TRADE_CONFIG['timeframe'],
                limit=50
            )
            
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df = self.calculate_core_indicators(df)
            
            current = df.iloc[-1]
            return {
                'price': current['close'],
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'high': current['high'],
                'low': current['low'],
                'volume': current['volume'],
                'technical_data': {
                    'sma_fast': current.get('sma_fast', 0),
                    'sma_slow': current.get('sma_slow', 0), 
                    'sma_trend': current.get('sma_trend', 0),
                    'rsi': current.get('rsi', 50),
                    'volume_ratio': current.get('volume_ratio', 1),
                    'bb_upper': current.get('bb_upper', 0),
                    'bb_lower': current.get('bb_lower', 0),
                },
                'trend_analysis': self.get_simple_trend(df)
            }
            
        except Exception as e:
            print(f"❌ 数据获取失败: {e}")
            return None

    def generate_simple_trading_prompt(self, price_data):
        """
        🎯 生成简洁有效的交易提示词
        基于明确的交易哲学：趋势跟踪 + 风险控制
        """
        current_pos = self.get_current_position()
        
        prompt = f"""
你是一个严谨的趋势跟踪交易系统。基于明确的规则进行决策，避免主观判断。

【当前市场状态】
- 价格: ${price_data['price']:.3f}
- 持仓: {current_pos if current_pos else "无持仓"}
- 短期趋势: {price_data['trend_analysis'].get('short_term', 'N/A')}
- 中期趋势: {price_data['trend_analysis'].get('medium_term', 'N/A')}

【核心交易规则 - 严格执行】

入场条件（需同时满足）：
✅ 趋势确认：价格在慢速均线之上(多头)或之下(空头)
✅ 动量配合：RSI在合理区间(30-70)
✅ 成交量：成交量高于平均水平

出场条件（满足任一）：
🛑 止损触发：价格触及2%止损位
🎯 止盈达成：价格达到4%止盈位
🔁 趋势反转：均线系统发出反向信号

【当前技术状况】
- 快慢均线关系: {'金叉' if price_data['technical_data'].get('sma_fast', 0) > price_data['technical_data'].get('sma_slow', 0) else '死叉'}
- RSI状态: {price_data['technical_data'].get('rsi', 0):.1f} ({'超买' if price_data['technical_data'].get('rsi', 0) > 70 else '超卖' if price_data['technical_data'].get('rsi', 0) < 30 else '正常'})
- 成交量: {price_data['technical_data'].get('volume_ratio', 0):.2f}x 均量
- 布林带位置: {'上部' if price_data['price'] > price_data['technical_data'].get('bb_upper', 0) else '下部' if price_data['price'] < price_data['technical_data'].get('bb_lower', 0) else '中部'}

【重要原则】
1. 趋势为王 - 只在明确趋势中交易
2. 风险第一 - 单笔亏损不超过总资金2%  
3. 持仓耐心 - 给趋势足够时间发展
4. 止损坚决 - 触及止损无条件出场

请基于以上规则给出明确决策：

{{
    "signal": "BUY|SELL|HOLD",
    "reason": "基于具体规则的分析",
    "stop_loss": 具体价格,
    "take_profit": 具体价格,
    "confidence": "HIGH|MEDIUM|LOW"
}}
"""
        return prompt

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
        current_price = price_data['price']
        trend = price_data.get('trend_analysis', {}).get('overall', '震荡整理')
        
        # 基于趋势的备用信号
        if trend == '上涨趋势':
            signal = 'BUY'
            reason = "趋势跟踪: 上涨趋势"
        elif trend == '下跌趋势':
            signal = 'SELL' 
            reason = "趋势跟踪: 下跌趋势"
        else:
            signal = 'HOLD'
            reason = "震荡观望: 趋势不明确"
        
        return {
            "signal": signal,
            "reason": f"备用信号 - {reason}",
            "stop_loss": current_price * (1 - self.TRADE_CONFIG['risk_management']['stop_loss_ratio']),
            "take_profit": current_price * (1 + self.TRADE_CONFIG['risk_management']['take_profit_ratio']),
            "confidence": "LOW",
            "is_fallback": True
        }

    def analyze_with_deepseek_simple(self, price_data):
        """
        🎯 使用DeepSeek分析 - 简化版
        """
        prompt = self.generate_simple_trading_prompt(price_data)
        
        try:
            response = self.deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "你是严谨的趋势跟踪交易系统，基于明确规则执行交易。"},
                    {"role": "user", "content": prompt}
                ],
                stream=False,
                temperature=0.2  # 降低温度，保持一致性
            )

            result = response.choices[0].message.content
            print(f"🤖 DeepSeek分析回复: {result}")

            # 解析JSON响应
            start_idx = result.find('{')
            end_idx = result.rfind('}') + 1

            if start_idx != -1 and end_idx != 0:
                json_str = result[start_idx:end_idx]
                signal_data = self.safe_json_parse(json_str)
            else:
                signal_data = self.create_fallback_signal(price_data)

            # 验证必需字段
            required_fields = ['signal', 'reason', 'stop_loss', 'take_profit', 'confidence']
            if not all(field in signal_data for field in required_fields):
                signal_data = self.create_fallback_signal(price_data)

            # 保存信号到历史记录
            signal_data['timestamp'] = price_data['timestamp']
            self.signal_history.append(signal_data)
            if len(self.signal_history) > 20:
                self.signal_history.pop(0)

            return signal_data

        except Exception as e:
            print(f"❌ DeepSeek分析失败: {e}")
            return self.create_fallback_signal(price_data)

    def get_current_position(self):
        """
        获取当前持仓情况 - 币安版本
        """
        try:
            positions = self.exchange.fetch_positions([self.TRADE_CONFIG['symbol']])
            
            for pos in positions:
                if pos['symbol'] == self.TRADE_CONFIG['symbol'] + ':USDT':
                    contracts = float(pos['contracts']) if pos['contracts'] else 0
                    
                    if contracts > 0:
                        return {
                            'side': pos['side'],  # 'long' or 'short'
                            'size': contracts,
                            'entry_price': float(pos['entryPrice']) if pos['entryPrice'] else 0,
                            'unrealized_pnl': float(pos['unrealizedPnl']) if pos['unrealizedPnl'] else 0,
                            'leverage': float(pos['leverage']) if pos['leverage'] else self.TRADE_CONFIG['leverage'],
                            'symbol': self.TRADE_CONFIG['symbol']
                        }
                        
            return None
            
        except Exception as e:
            print(f"❌ 获取持仓失败: {e}")
            return None

    def should_enter_trade(self, signal_data, price_data, current_position):
        """
        🎯 严格的入场条件检查
        """
        if current_position:
            return False  # 有持仓时不新开仓
            
        tech_data = price_data['technical_data']
        price = price_data['price']
        
        # 检查趋势条件
        sma_fast = tech_data.get('sma_fast', price)
        sma_slow = tech_data.get('sma_slow', price)
        sma_trend = tech_data.get('sma_trend', price)
        
        # 多头入场条件
        if signal_data['signal'] == 'BUY':
            trend_ok = price > sma_slow > sma_trend
            rsi_ok = 30 < tech_data.get('rsi', 50) < 70
            volume_ok = tech_data.get('volume_ratio', 0) > 0.8
            
            return trend_ok and rsi_ok and volume_ok
            
        # 空头入场条件  
        elif signal_data['signal'] == 'SELL':
            trend_ok = price < sma_slow < sma_trend
            rsi_ok = 30 < tech_data.get('rsi', 50) < 70
            volume_ok = tech_data.get('volume_ratio', 0) > 0.8
            
            return trend_ok and rsi_ok and volume_ok
            
        return False

    def should_exit_trade(self, current_position, price_data):
        """
        🎯 严格的出场条件检查
        """
        if not current_position:
            return False, ''
            
        current_price = price_data['price']
        entry_price = current_position['entry_price']
        tech_data = price_data['technical_data']
        
        # 检查止损止盈
        if current_position['side'] == 'long':
            # 多头止损止盈
            stop_loss = entry_price * (1 - self.TRADE_CONFIG['risk_management']['stop_loss_ratio'])
            take_profit = entry_price * (1 + self.TRADE_CONFIG['risk_management']['take_profit_ratio'])
            
            if current_price <= stop_loss:
                return True, '止损触发'
            if current_price >= take_profit:
                return True, '止盈达成'
                
            # 趋势反转检查
            if current_price < tech_data.get('sma_slow', current_price):
                return True, '趋势反转'
                
        else:  # 空头
            stop_loss = entry_price * (1 + self.TRADE_CONFIG['risk_management']['stop_loss_ratio'])
            take_profit = entry_price * (1 - self.TRADE_CONFIG['risk_management']['take_profit_ratio'])
            
            if current_price >= stop_loss:
                return True, '止损触发'
            if current_price <= take_profit:
                return True, '止盈达成'
                
            if current_price > tech_data.get('sma_slow', current_price):
                return True, '趋势反转'
                
        return False, '继续持有'

    def close_position(self, position, reason):
        """
        🎯 平仓逻辑
        """
        try:
            if position['side'] == 'long':
                self.exchange.create_market_order(
                    self.TRADE_CONFIG['symbol'], 'sell', position['size'],
                    params={'reduceOnly': True}
                )
            else:
                self.exchange.create_market_order(
                    self.TRADE_CONFIG['symbol'], 'buy', position['size'],
                    params={'reduceOnly': True}
                )
                
            print(f"✅ 平仓完成: {reason}")
            self.position_open_time = None  # 🆕 清空持仓时间
            
            # 发送通知
            self.send_dingtalk_message(
                "SOL交易通知 - 平仓",
                f"平仓完成\n"
                f"方向: {position['side']}\n"
                f"数量: {position['size']:.3f} SOL\n"
                f"原因: {reason}",
                is_important=True
            )
            
        except Exception as e:
            print(f"❌ 平仓失败: {e}")

    def set_simple_stop_loss_take_profit(self, side, quantity, stop_loss, take_profit):
        """
        🎯 简单的止盈止损设置
        """
        try:
            symbol = self.TRADE_CONFIG['symbol']
            
            # 取消现有订单
            open_orders = self.exchange.fetch_open_orders(symbol)
            for order in open_orders:
                if order['type'] in ['stop_market', 'take_profit_market']:
                    self.exchange.cancel_order(order['id'], symbol)
            
            # 设置新订单
            if side == 'long':
                if stop_loss > 0:
                    self.exchange.create_order(
                        symbol, 'stop_market', 'sell', quantity, None,
                        {'stopPrice': stop_loss, 'reduceOnly': True}
                    )
                if take_profit > 0:
                    self.exchange.create_order(
                        symbol, 'take_profit_market', 'sell', quantity, None,
                        {'stopPrice': take_profit, 'reduceOnly': True}
                    )
            else:  # short
                if stop_loss > 0:
                    self.exchange.create_order(
                        symbol, 'stop_market', 'buy', quantity, None,
                        {'stopPrice': stop_loss, 'reduceOnly': True}
                    )
                if take_profit > 0:
                    self.exchange.create_order(
                        symbol, 'take_profit_market', 'buy', quantity, None, 
                        {'stopPrice': take_profit, 'reduceOnly': True}
                    )
                    
            print(f"✅ 止盈止损设置: 止损={stop_loss:.3f}, 止盈={take_profit:.3f}")
            
        except Exception as e:
            print(f"❌ 止盈止损设置失败: {e}")

    def calculate_simple_position(self, price_data):
        """
        🎯 简单的仓位计算
        """
        try:
            # 获取账户余额
            balance = self.exchange.fetch_balance()
            usdt_balance = balance['USDT']['free']
            
            # 简单仓位管理：总资金的10%
            risk_amount = usdt_balance * 0.1
            current_price = price_data['price']
            position_size = risk_amount / current_price
            
            # 确保最小交易量
            min_quantity = self.TRADE_CONFIG['sol_config']['min_quantity']
            position_size = max(min_quantity, position_size)
            
            # 根据步长调整数量
            amount_step = self.TRADE_CONFIG.get('amount_step', 0.001)
            if amount_step > 0:
                position_size = (position_size // amount_step) * amount_step
            
            print(f"💰 仓位计算: {usdt_balance:.2f} USDT → {position_size:.3f} SOL")
            return position_size
            
        except Exception as e:
            print(f"❌ 仓位计算失败: {e}")
            return self.TRADE_CONFIG['sol_config']['base_quantity']

    def execute_simple_trade(self, signal_data, price_data):
        """
        🎯 执行简单的交易
        """
        try:
            position_size = self.calculate_simple_position(price_data)
            
            if signal_data['signal'] == 'BUY':
                self.exchange.create_market_order(
                    self.TRADE_CONFIG['symbol'], 'buy', position_size
                )
                print(f"🟢 开多仓: {position_size:.2f} SOL")
                
                # 设置止盈止损
                self.set_simple_stop_loss_take_profit(
                    'long', position_size, 
                    signal_data['stop_loss'], signal_data['take_profit']
                )
                
                # 记录开仓时间
                self.position_open_time = datetime.now()
                
                self.send_dingtalk_message(
                    "SOL交易通知 - 开多仓",
                    f"新建多仓\n"
                    f"数量: {position_size:.3f} SOL\n"
                    f"价格: ${price_data['price']:.3f}\n"
                    f"止损: ${signal_data['stop_loss']:.3f}\n"
                    f"止盈: ${signal_data['take_profit']:.3f}\n"
                    f"理由: {signal_data['reason']}",
                    is_important=True
                )
                
            elif signal_data['signal'] == 'SELL':
                self.exchange.create_market_order(
                    self.TRADE_CONFIG['symbol'], 'sell', position_size
                )
                print(f"🔴 开空仓: {position_size:.2f} SOL")
                
                self.set_simple_stop_loss_take_profit(
                    'short', position_size, 
                    signal_data['stop_loss'], signal_data['take_profit']
                )
                
                # 记录开仓时间
                self.position_open_time = datetime.now()
                
                self.send_dingtalk_message(
                    "SOL交易通知 - 开空仓",
                    f"新建空仓\n"
                    f"数量: {position_size:.3f} SOL\n"
                    f"价格: ${price_data['price']:.3f}\n"
                    f"理由: {signal_data['reason']}",
                    is_important=True
                )
                
        except Exception as e:
            print(f"❌ 交易执行失败: {e}")
            self.send_dingtalk_message(
                "SOL交易异常",
                f"❌ 交易执行失败\n错误: {str(e)}",
                is_important=True
            )

    def execute_prudent_trading(self, signal_data, price_data):
        """
        🎯 执行谨慎的交易逻辑
        """
        current_position = self.get_current_position()
        
        # 🎯 优先处理出场逻辑
        if current_position:
            should_exit, exit_reason = self.should_exit_trade(current_position, price_data)
            if should_exit:
                print(f"🎯 执行出场: {exit_reason}")
                self.close_position(current_position, exit_reason)
                return
                
        # 🎯 入场逻辑检查
        if signal_data['signal'] in ['BUY', 'SELL']:
            if self.should_enter_trade(signal_data, price_data, current_position):
                print(f"🎯 符合入场条件，执行{signal_data['signal']}信号")
                self.execute_simple_trade(signal_data, price_data)
            else:
                print("⏸️ 不符合入场条件，跳过交易")
        else:
            print("⏸️ 观望信号，不执行交易")

    def analyze_with_deepseek_with_retry(self, price_data, max_retries=2):
        """带重试的DeepSeek分析"""
        for attempt in range(max_retries):
            try:
                signal_data = self.analyze_with_deepseek_simple(price_data)
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

    def trading_bot_simple(self):
        """
        🎯 简化的交易机器人主逻辑
        """
        # 等待到执行时间
        wait_seconds = self.wait_for_next_period()
        if wait_seconds > 0:
            time.sleep(wait_seconds)

        print("\n" + "=" * 60)
        print(f"🎯 执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)
        
        # 获取数据
        price_data = self.get_market_data_simple()
        if not price_data:
            self.send_dingtalk_message(
                "SOL数据获取异常",
                "❌ 无法获取SOL市场数据，请检查网络连接",
                is_important=True
            )
            return
            
        print(f"💰 SOL价格: ${price_data['price']:.3f}")
        print(f"📊 趋势状态: {price_data['trend_analysis'].get('overall', 'N/A')}")
        print(f"📈 RSI: {price_data['technical_data'].get('rsi', 0):.1f}")
        
        # AI分析
        signal_data = self.analyze_with_deepseek_with_retry(price_data)
        
        if signal_data.get('is_fallback', False):
            print("⚠️ 使用备用交易信号")
        
        # 执行交易
        self.execute_prudent_trading(signal_data, price_data)

    def main(self):
        """主函数"""
        print("🚀 SOL/USDT 币安自动交易机器人启动成功！")
        print("🎯 务实优化版 - 趋势跟踪策略")
        
        if self.TRADE_CONFIG['test_mode']:
            print("🔬 当前为模拟模式，不会真实下单")
        else:
            print("💰 实盘交易模式，请谨慎操作！")

        print(f"⏰ 交易周期: {self.TRADE_CONFIG['timeframe']}")
        print(f"🔄 执行间隔: {self.TRADE_CONFIG['execution_interval']}分钟")
        print("📊 已启用核心趋势跟踪和风险管理")

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
            f"执行间隔: {self.TRADE_CONFIG['execution_interval']}分钟\n"
            f"策略: 趋势跟踪 + 严格风控",
            is_important=True
        )

        print("🔄 开始执行交易循环...")

        # 循环执行
        while True:
            try:
                self.trading_bot_simple()
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
                import traceback
                traceback.print_exc()
                time.sleep(60)


if __name__ == "__main__":
    bot = BinanceSOLTradingBot()
    bot.main()