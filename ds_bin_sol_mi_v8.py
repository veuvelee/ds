"""
SOL/USDT Binance永续合约自动交易机器人
完整版本：包含所有必需方法，高度容错，多策略融合
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
import asyncio
import threading
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
import logging
from enum import Enum

# ============================================================================
# 配置日志系统
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(f'logs/out8.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 加载环境变量
load_dotenv()

# ============================================================================
# 枚举定义
# ============================================================================
class SignalType(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

class ConfidenceLevel(Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

class RiskLevel(Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

# ============================================================================
# 数据类定义
# ============================================================================
@dataclass
class TradeConfig:
    """交易配置数据类"""
    user: str
    symbol: str = 'SOL/USDT:USDT'
    leverage: int = 10
    timeframe: str = '15m'
    execution_interval: int = 15
    test_mode: bool = False
    data_points: int = 96
    contract_size: float = 1.0
    min_amount: float = 0.01
    
    # 多时间框架配置
    timeframes: Dict[str, str] = None
    
    # 仓位管理配置
    position_config: Dict = None
    
    # API重试配置
    retry_config: Dict = None
    
    def __post_init__(self):
        """初始化后处理"""
        if self.timeframes is None:
            self.timeframes = {
                'fast': '5m',
                'medium': '15m',
                'slow': '1h'
            }
        
        if self.position_config is None:
            self.position_config = {
                'enable_intelligent_position': True,
                'base_usdt_amount': 100,
                'high_confidence_multiplier': 1.5,
                'medium_confidence_multiplier': 1.0,
                'low_confidence_multiplier': 0.5,
                'max_position_ratio': 50,
                'trend_strength_multiplier': 1.2,
                'volatility_multiplier': True,
                'max_daily_loss_percent': 5,
                'max_consecutive_losses': 3,
                'pyramid_enabled': True,
                'pyramid_max_layers': 3,
                'stop_loss_pct': 2.0,
                'take_profit_pct': 4.0
            }
        
        if self.retry_config is None:
            self.retry_config = {
                'max_retries': 3,
                'retry_delay': 1,
                'exponential_backoff': True
            }

@dataclass
class SignalData:
    """交易信号数据类"""
    signal: SignalType
    reason: str
    stop_loss: float
    take_profit: float
    confidence: ConfidenceLevel
    risk_level: RiskLevel
    timestamp: str
    price: float = 0.0
    is_fallback: bool = False

# ============================================================================
# 核心组件类
# ============================================================================

class DingTalkManager:
    """钉钉通知管理器"""
    
    def __init__(self, webhook: str, secret: str, enable: bool = True, user: str = 'default'):
        self.webhook = webhook
        self.secret = secret
        self.enable = enable
        self.message_queue = []
        self.lock = threading.Lock()
        self.message_count = 0
        self.last_message_time = time.time()
        self.user = user
        
    def send_message(self, title: str, message: str, message_type: str = "info", 
                    retry_count: int = 3) -> bool:
        """发送钉钉消息（线程安全，带重试）"""
        if not self.enable or not self.webhook:
            logger.warning("钉钉通知已禁用或未配置webhook")
            return False
        
        # 频率限制：每分钟最多10条消息
        current_time = time.time()
        if current_time - self.last_message_time < 6 and self.message_count >= 10:
            logger.warning("钉钉消息频率限制，跳过发送")
            return False
        
        for attempt in range(retry_count):
            try:
                # 消息类型表情映射
                emojis = {
                    "info": "ℹ️",
                    "success": "✅", 
                    "warning": "⚠️",
                    "error": "❌",
                    "trade": "💰",
                    "alert": "🚨",
                    "signal": "📈"
                }
                emoji = emojis.get(message_type, "ℹ️")
                
                # 生成时间戳和签名
                timestamp = str(round(time.time() * 1000))
                current_time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
                # 构建完整消息
                full_message = f"### {emoji} [{self.user}]  {title}\n\n{message}\n\n---\n⏰ {current_time_str}"
                
                # 生成签名（如果有secret）
                webhook_url = self.webhook
                if self.secret:
                    string_to_sign = f"{timestamp}\n{self.secret}"
                    hmac_code = hmac.new(
                        self.secret.encode('utf-8'),
                        string_to_sign.encode('utf-8'),
                        hashlib.sha256
                    ).digest()
                    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
                    webhook_url = f"{self.webhook}&timestamp={timestamp}&sign={sign}"
                
                # 请求数据
                data = {
                    "msgtype": "markdown",
                    "markdown": {
                        "title": f"{emoji} [{self.user}] {title}",
                        "text": full_message
                    },
                    "at": {"isAtAll": False}
                }
                
                headers = {"Content-Type": "application/json", "Charset": "UTF-8"}
                
                with self.lock:
                    response = requests.post(
                        webhook_url, 
                        json=data, 
                        headers=headers, 
                        timeout=10
                    )
                
                if response.status_code == 200:
                    result = response.json()
                    if result.get('errcode') == 0:
                        logger.info(f"钉钉消息发送成功: {title}")
                        self.message_count += 1
                        self.last_message_time = current_time
                        
                        # 重置消息计数（每分钟）
                        if current_time - self.last_message_time >= 60:
                            self.message_count = 0
                        
                        return True
                    else:
                        logger.error(f"钉钉API错误: {result.get('errmsg')}")
                else:
                    logger.error(f"钉钉HTTP错误: {response.status_code}")
                
                # 重试前等待（指数退避）
                if attempt < retry_count - 1:
                    wait_time = 2 ** attempt  # 指数退避
                    logger.info(f"钉钉消息发送失败，等待{wait_time}秒后重试...")
                    time.sleep(wait_time)
                    
            except requests.exceptions.Timeout:
                logger.error(f"钉钉请求超时 (尝试 {attempt+1}/{retry_count})")
                if attempt < retry_count - 1:
                    time.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"钉钉消息发送异常 (尝试 {attempt+1}/{retry_count}): {e}")
                if attempt < retry_count - 1:
                    time.sleep(2 ** attempt)
        
        logger.error(f"钉钉消息发送失败，已重试{retry_count}次")
        return False
    
    def send_alert(self, alert_type: str, message: str, level: str = "warning"):
        """发送警报消息"""
        alert_titles = {
            "risk": "⚠️ 风险警报",
            "error": "❌ 系统错误",
            "trade": "💰 交易提醒",
            "performance": "📊 绩效报告"
        }
        title = alert_titles.get(alert_type, "⚠️ 系统通知")
        return self.send_message(title, message, level)

class RetryManager:
    """重试管理器"""
    
    @staticmethod
    def retry_operation(operation, max_retries: int = 3, delay: float = 1.0, 
                       exponential_backoff: bool = True, *args, **kwargs):
        """
        重试操作装饰器/管理器
        
        Args:
            operation: 要执行的操作函数
            max_retries: 最大重试次数
            delay: 重试延迟（秒）
            exponential_backoff: 是否使用指数退避
            *args, **kwargs: 操作函数的参数
            
        Returns:
            操作结果或None
        """
        last_exception = None
        
        for attempt in range(max_retries):
            try:
                result = operation(*args, **kwargs)
                if attempt > 0:
                    logger.info(f"操作在第{attempt+1}次尝试时成功")
                return result
                
            except Exception as e:
                last_exception = e
                logger.warning(f"操作失败 (尝试 {attempt+1}/{max_retries}): {e}")
                
                # 最后一次尝试不等待
                if attempt == max_retries - 1:
                    break
                
                # 计算等待时间
                if exponential_backoff:
                    wait_time = delay * (2 ** attempt)
                else:
                    wait_time = delay
                
                logger.info(f"等待 {wait_time:.1f} 秒后重试...")
                time.sleep(wait_time)
        
        logger.error(f"操作失败，已达最大重试次数 {max_retries}")
        if last_exception:
            logger.error(f"最后错误: {last_exception}")
        return None

class TechnicalAnalyzer:
    """技术分析器"""
    
    @staticmethod
    def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
        """计算技术指标"""
        try:
            if df.empty or len(df) < 20:
                logger.warning("数据不足，无法计算完整指标")
                return df
            
            close = df['close']
            high = df['high']
            low = df['low']
            volume = df['volume']
            
            # ========== 移动平均线 ==========
            df['sma_5'] = close.rolling(window=5, min_periods=1).mean()
            df['sma_10'] = close.rolling(window=10, min_periods=1).mean()
            df['sma_20'] = close.rolling(window=20, min_periods=1).mean()
            df['sma_50'] = close.rolling(window=50, min_periods=1).mean()
            
            # ========== 指数移动平均 ==========
            df['ema_9'] = close.ewm(span=9, adjust=False).mean()
            df['ema_12'] = close.ewm(span=12, adjust=False).mean()
            df['ema_26'] = close.ewm(span=26, adjust=False).mean()
            
            # ========== MACD ==========
            df['macd'] = df['ema_12'] - df['ema_26']
            df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
            df['macd_hist'] = df['macd'] - df['macd_signal']
            
            # ========== RSI ==========
            delta = close.diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            df['rsi'] = 100 - (100 / (1 + rs))
            
            # ========== 布林带 ==========
            df['bb_middle'] = close.rolling(window=20).mean()
            bb_std = close.rolling(window=20).std()
            df['bb_upper'] = df['bb_middle'] + (bb_std * 2)
            df['bb_lower'] = df['bb_middle'] - (bb_std * 2)
            df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / df['bb_middle']
            
            # ========== ATR（平均真实波幅） ==========
            tr1 = high - low
            tr2 = abs(high - close.shift())
            tr3 = abs(low - close.shift())
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            df['atr'] = tr.rolling(window=14).mean()
            
            # ========== 成交量指标 ==========
            df['volume_ma'] = volume.rolling(window=20).mean()
            df['volume_ratio'] = volume / df['volume_ma']
            
            # ========== 支撑阻力 ==========
            df['resistance'] = high.rolling(window=20).max()
            df['support'] = low.rolling(window=20).min()
            
            # 填充NaN值
            df = df.ffill().bfill()
            
            logger.debug("技术指标计算完成")
            return df
            
        except Exception as e:
            logger.error(f"技术指标计算失败: {e}")
            return df

class RiskManager:
    """风险管理器"""
    
    def __init__(self, config: TradeConfig, exchange):
        self.config = config
        self.exchange = exchange
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.last_trade_time = None
        self.trade_history = []
        self.max_daily_loss = 0.0
        self.starting_balance = self._get_starting_balance()
        
    def _get_starting_balance(self) -> float:
        """获取起始余额"""
        try:
            balance = self.exchange.fetch_balance()
            if 'USDT' in balance:
                return float(balance['USDT']['free'])
            return 1000.0
        except:
            return 1000.0
    
    def check_risk_limits(self) -> Tuple[bool, str]:
        """检查所有风险限制"""
        checks = [
            self._check_daily_loss(),
            self._check_consecutive_losses(),
            self._check_max_position_size()
        ]
        
        for is_ok, message in checks:
            if not is_ok:
                return False, message
        
        return True, ""
    
    def _check_daily_loss(self) -> Tuple[bool, str]:
        """检查日亏损"""
        try:
            current_balance = self._get_current_balance()
            daily_loss_pct = abs(self.daily_pnl / self.starting_balance * 100)
            max_loss_pct = self.config.position_config['max_daily_loss_percent']
            
            if daily_loss_pct >= max_loss_pct:
                return False, f"日亏损已达{daily_loss_pct:.1f}%，超过{max_loss_pct}%限制"
            
            return True, ""
        except Exception as e:
            logger.error(f"检查日亏损失败: {e}")
            return True, ""
    
    def _check_consecutive_losses(self) -> Tuple[bool, str]:
        """检查连续亏损"""
        max_losses = self.config.position_config['max_consecutive_losses']
        if self.consecutive_losses >= max_losses:
            return False, f"连续亏损{self.consecutive_losses}次，暂停交易"
        return True, ""
    
    def _check_max_position_size(self) -> Tuple[bool, str]:
        """检查最大仓位"""
        try:
            position = self._get_current_position()
            if position:
                current_balance = self._get_current_balance()
                position_value = position['size'] * position['entry_price']
                position_ratio = position_value / (current_balance + position_value) / 10 * 100

                if position_ratio > self.config.position_config['max_position_ratio']:
                    return False, f"仓位比例{position_ratio:.1f}%超过限制"
            
            return True, ""
        except Exception as e:
            logger.error(f"检查仓位大小失败: {e}")
            return True, ""
    
    def _get_current_balance(self) -> float:
        """获取当前余额"""
        return RetryManager.retry_operation(
            lambda: self._fetch_balance_safe(),
            max_retries=2,
            delay=1
        ) or 1000.0
    
    def _fetch_balance_safe(self) -> float:
        """安全获取余额"""
        try:
            balance = self.exchange.fetch_balance()
            if 'USDT' in balance:
                return float(balance['USDT']['free'])
            return 1000.0
        except Exception as e:
            logger.error(f"获取余额失败: {e}")
            return 1000.0
    
    def _get_current_position(self) -> Optional[Dict]:
        """获取当前持仓"""
        return RetryManager.retry_operation(
            lambda: self._fetch_position_safe(),
            max_retries=2,
            delay=1
        )
    
    def _fetch_position_safe(self) -> Optional[Dict]:
        """安全获取持仓"""
        try:
            positions = self.exchange.fetch_positions([self.config.symbol])
            for pos in positions:
                if pos['symbol'] == self.config.symbol:
                    contracts = float(pos['contracts'] or 0)
                    if contracts > 0:
                        return {
                            'side': pos['side'],
                            'size': contracts,
                            'entry_price': float(pos['entryPrice'] or 0),
                            'unrealized_pnl': float(pos['unrealizedPnl'] or 0),
                            'leverage': float(pos['leverage'] or self.config.leverage)
                        }
            return None
        except Exception as e:
            logger.error(f"获取持仓失败: {e}")
            return None
    
    def record_trade(self, signal: str, pnl: float, reason: str):
        """记录交易"""
        trade_record = {
            'timestamp': datetime.now(),
            'signal': signal,
            'pnl': pnl,
            'reason': reason,
            'balance': self._get_current_balance()
        }
        
        self.trade_history.append(trade_record)
        self.daily_pnl += pnl
        
        # 更新连续亏损计数
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        
        # 限制历史记录长度
        if len(self.trade_history) > 100:
            self.trade_history.pop(0)
        
        self.last_trade_time = datetime.now()
        
        # 记录日志
        logger.info(f"交易记录: {signal}, 盈亏: {pnl:.2f}, 原因: {reason}")
    
    def get_performance_report(self) -> Dict:
        """获取绩效报告"""
        if not self.trade_history:
            return {}
        
        total_trades = len(self.trade_history)
        winning_trades = sum(1 for t in self.trade_history if t['pnl'] > 0)
        losing_trades = sum(1 for t in self.trade_history if t['pnl'] < 0)
        
        total_profit = sum(t['pnl'] for t in self.trade_history if t['pnl'] > 0)
        total_loss = abs(sum(t['pnl'] for t in self.trade_history if t['pnl'] < 0))
        
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
        profit_factor = (total_profit / total_loss) if total_loss > 0 else float('inf')
        
        return {
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': f"{win_rate:.1f}%",
            'profit_factor': f"{profit_factor:.2f}" if profit_factor != float('inf') else "∞",
            'total_pnl': f"{sum(t['pnl'] for t in self.trade_history):.2f}",
            'daily_pnl': f"{self.daily_pnl:.2f}",
            'consecutive_losses': self.consecutive_losses,
            'current_balance': f"{self._get_current_balance():.2f}"
        }
    
    def reset_daily_stats(self):
        """重置日统计"""
        now = datetime.now()
        if self.last_trade_time and self.last_trade_time.date() < now.date():
            self.daily_pnl = 0.0
            self.starting_balance = self._get_current_balance()
            logger.info("已重置日统计")

class OrderManager:
    """订单管理器"""
    
    def __init__(self, exchange, symbol: str, config: TradeConfig):
        self.exchange = exchange
        self.symbol = symbol
        self.config = config
        self.active_orders = []
    
    def cancel_existing_orders(self, side: str = None) -> int:
        """
        取消现有条件订单
        
        Args:
            side: 指定取消的方向（'buy'或'sell'），None表示取消所有
            
        Returns:
            取消的订单数量
        """
        try:
            # 获取所有活动订单（包括条件订单）
            params = {'stop': True}  # 获取条件订单
            orders = self.exchange.fetch_open_orders(self.symbol, params=params)
            
            cancelled_count = 0
            
            for order in orders:
                try:
                    # 检查订单信息
                    info = order.get('info', {})
                    order_id = order.get('id')
                    order_side = order.get('side', '').lower()
                    
                    # 如果指定了方向，检查是否匹配
                    if side is not None and order_side != side.lower():
                        continue
                    
                    # 检查是否条件订单（通过算法类型或reduceOnly判断）
                    is_conditional = (
                        info.get('algoType') == 'CONDITIONAL' or
                        order.get('reduceOnly', False) or
                        info.get('closePosition') == 'true'
                    )
                    
                    if is_conditional:
                        logger.info(f"取消条件订单: {order_id} - {order.get('type', 'N/A')}")
                        
                        # 尝试取消订单
                        try:
                            self.exchange.cancel_order(order_id, self.symbol, params={'stop': True})
                            cancelled_count += 1
                            time.sleep(0.1)  # 避免API限频
                        except Exception as cancel_error:
                            logger.error(f"取消订单 {order_id} 失败: {cancel_error}")
                            
                except Exception as order_error:
                    logger.error(f"处理订单时出错: {order_error}")
                    continue
            
            if cancelled_count > 0:
                logger.info(f"已取消 {cancelled_count} 个条件订单")
            else:
                logger.info("没有需要取消的条件订单")
            
            return cancelled_count
            
        except Exception as e:
            logger.error(f"取消现有订单失败: {e}")
            return 0
    
    def create_market_order(self, side: str, amount: float, reduce_only: bool = False) -> Optional[Dict]:
        """创建市价订单"""
        try:
            params = {}
            if reduce_only:
                params['reduceOnly'] = True
            
            order = self.exchange.create_order(
                symbol=self.symbol,
                type='market',
                side=side,
                amount=amount,
                params=params
            )
            
            logger.info(f"市价订单创建成功: {side} {amount} {self.symbol}")
            return order
            
        except Exception as e:
            logger.error(f"创建市价订单失败: {e}")
            return None
    
    def create_stop_loss_order(self, side: str, amount: float, stop_price: float) -> Optional[Dict]:
        """创建止损订单"""
        try:
            # 确定订单方向（与持仓方向相反）
            stop_side = 'sell' if side == 'long' else 'buy'
            
            order = self.exchange.create_order(
                symbol=self.symbol,
                type='STOP_MARKET',
                side=stop_side,
                amount=amount,
                params={
                    'stopPrice': stop_price,
                    'closePosition': True,
                    'workingType': 'MARK_PRICE',
                    'priceProtect': True
                }
            )
            
            logger.info(f"止损订单创建成功: {stop_price}")
            return order
            
        except Exception as e:
            logger.error(f"创建止损订单失败: {e}")
            return None
    
    def create_take_profit_order(self, side: str, amount: float, take_profit_price: float) -> Optional[Dict]:
        """创建止盈订单"""
        try:
            # 确定订单方向（与持仓方向相反）
            tp_side = 'sell' if side == 'long' else 'buy'
            
            order = self.exchange.create_order(
                symbol=self.symbol,
                type='TAKE_PROFIT_MARKET',
                side=tp_side,
                amount=amount,
                params={
                    'stopPrice': take_profit_price,
                    'closePosition': True,
                    'workingType': 'MARK_PRICE',
                    'priceProtect': True
                }
            )
            
            logger.info(f"止盈订单创建成功: {take_profit_price}")
            return order
            
        except Exception as e:
            logger.error(f"创建止盈订单失败: {e}")
            return None
    
    def setup_stop_loss_take_profit(self, position_side: str, position_size: float, 
                                   stop_loss_price: float, take_profit_price: float) -> bool:
        """设置止盈止损"""
        try:
            logger.info(f"设置止盈止损: {position_side} {position_size}张")
            
            # 先取消现有条件订单
            self.cancel_existing_orders()
            
            # 创建止损订单
            stop_order = self.create_stop_loss_order(position_side, position_size, stop_loss_price)
            if not stop_order:
                logger.error("止损订单创建失败")
                return False
            
            # 创建止盈订单
            tp_order = self.create_take_profit_order(position_side, position_size, take_profit_price)
            if not tp_order:
                logger.error("止盈订单创建失败")
                # 尝试取消已创建的止损订单
                try:
                    self.exchange.cancel_order(stop_order['id'], self.symbol, params={'stop': True})
                except:
                    pass
                return False
            
            # 获取当前价格
            ticker = self.exchange.fetch_ticker(self.symbol)
            current_price = ticker['last']
            
            # 计算价格距离
            stop_distance_pct = abs((stop_loss_price - current_price) / current_price * 100)
            tp_distance_pct = abs((take_profit_price - current_price) / current_price * 100)
            
            logger.info(f"""
            止盈止损设置成功:
            方向: {position_side}
            数量: {position_size:.2f}张
            当前价: ${current_price:.2f}
            止损: ${stop_loss_price:.2f} ({stop_distance_pct:.1f}%)
            止盈: ${take_profit_price:.2f} ({tp_distance_pct:.1f}%)
            """)
            
            return True
            
        except Exception as e:
            logger.error(f"设置止盈止损失败: {e}")
            return False

class MarketDataFetcher:
    """市场数据获取器"""
    
    def __init__(self, exchange, symbol: str, config: TradeConfig):
        self.exchange = exchange
        self.symbol = symbol
        self.config = config
        self.cache = {}
        self.cache_time = {}
        self.cache_duration = 60  # 缓存60秒
    
    def fetch_ohlcv_data(self, timeframe: str = None, limit: int = None) -> Optional[pd.DataFrame]:
        """获取K线数据"""
        try:
            tf = timeframe or self.config.timeframe
            lim = limit or self.config.data_points
            
            # 检查缓存
            cache_key = f"ohlcv_{tf}_{lim}"
            current_time = time.time()
            
            if (cache_key in self.cache and 
                cache_key in self.cache_time and
                current_time - self.cache_time[cache_key] < self.cache_duration):
                logger.debug(f"使用缓存数据: {cache_key}")
                return self.cache[cache_key].copy()
            
            # 从交易所获取数据
            ohlcv = RetryManager.retry_operation(
                lambda: self.exchange.fetch_ohlcv(self.symbol, tf, limit=lim),
                max_retries=3,
                delay=1,
                exponential_backoff=True
            )
            
            if not ohlcv:
                logger.error("获取K线数据失败")
                return None
            
            # 转换为DataFrame
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            
            # 计算技术指标
            analyzer = TechnicalAnalyzer()
            df = analyzer.calculate_indicators(df)
            
            # 更新缓存
            self.cache[cache_key] = df.copy()
            self.cache_time[cache_key] = current_time
            
            logger.info(f"获取K线数据成功: {tf}, {len(df)} 条记录")
            return df
            
        except Exception as e:
            logger.error(f"获取K线数据异常: {e}")
            return None
    
    def fetch_multi_timeframe_data(self) -> Dict[str, pd.DataFrame]:
        """获取多时间框架数据"""
        try:
            data = {}
            
            for name, tf in self.config.timeframes.items():
                df = self.fetch_ohlcv_data(tf, limit=50)
                if df is not None:
                    data[name] = df
            
            return data
            
        except Exception as e:
            logger.error(f"获取多时间框架数据失败: {e}")
            return {}
    
    def get_price_data(self) -> Optional[Dict]:
        """获取价格数据"""
        try:
            df = self.fetch_ohlcv_data()
            if df is None or df.empty:
                return None
            
            current_data = df.iloc[-1]
            previous_data = df.iloc[-2] if len(df) > 1 else current_data
            
            # 趋势分析
            trend_analysis = self._analyze_trend(df)
            
            # 支撑阻力
            levels_analysis = self._analyze_support_resistance(df)
            
            # 技术指标
            technical_data = {
                'sma_5': current_data.get('sma_5', 0),
                'sma_20': current_data.get('sma_20', 0),
                'sma_50': current_data.get('sma_50', 0),
                'rsi': current_data.get('rsi', 50),
                'macd': current_data.get('macd', 0),
                'macd_signal': current_data.get('macd_signal', 0),
                'macd_hist': current_data.get('macd_hist', 0),
                'bb_upper': current_data.get('bb_upper', 0),
                'bb_lower': current_data.get('bb_lower', 0),
                'atr': current_data.get('atr', 0),
                'volume_ratio': current_data.get('volume_ratio', 1)
            }
            
            price_data = {
                'price': float(current_data['close']),
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'high': float(current_data['high']),
                'low': float(current_data['low']),
                'volume': float(current_data['volume']),
                'timeframe': self.config.timeframe,
                'price_change': ((current_data['close'] - previous_data['close']) / previous_data['close'] * 100),
                'technical_data': technical_data,
                'trend_analysis': trend_analysis,
                'levels_analysis': levels_analysis,
                'full_data': df
            }
            
            return price_data
            
        except Exception as e:
            logger.error(f"获取价格数据失败: {e}")
            return None
    
    def _analyze_trend(self, df: pd.DataFrame) -> Dict:
        """分析趋势"""
        try:
            if df.empty:
                return {}
            
            current_price = df['close'].iloc[-1]
            
            # 多时间框架趋势
            short_trend = "上涨" if current_price > df['sma_20'].iloc[-1] else "下跌"
            medium_trend = "上涨" if current_price > df['sma_50'].iloc[-1] else "下跌"
            
            # MACD趋势
            macd_trend = "bullish" if df['macd'].iloc[-1] > df['macd_signal'].iloc[-1] else "bearish"
            
            # 综合趋势
            if short_trend == "上涨" and medium_trend == "上涨":
                overall_trend = "强势上涨"
            elif short_trend == "下跌" and medium_trend == "下跌":
                overall_trend = "强势下跌"
            else:
                overall_trend = "震荡整理"
            
            return {
                'short_term': short_trend,
                'medium_term': medium_trend,
                'macd': macd_trend,
                'overall': overall_trend,
                'rsi_level': df['rsi'].iloc[-1]
            }
            
        except Exception as e:
            logger.error(f"趋势分析失败: {e}")
            return {}
    
    def _analyze_support_resistance(self, df: pd.DataFrame, lookback: int = 20) -> Dict:
        """分析支撑阻力"""
        try:
            if df.empty:
                return {}
            
            recent_high = df['high'].tail(lookback).max()
            recent_low = df['low'].tail(lookback).min()
            current_price = df['close'].iloc[-1]
            
            return {
                'static_resistance': float(recent_high),
                'static_support': float(recent_low),
                'dynamic_resistance': float(df['bb_upper'].iloc[-1]),
                'dynamic_support': float(df['bb_lower'].iloc[-1]),
                'price_vs_resistance': ((recent_high - current_price) / current_price * 100),
                'price_vs_support': ((current_price - recent_low) / recent_low * 100)
            }
            
        except Exception as e:
            logger.error(f"支撑阻力分析失败: {e}")
            return {}

class AIAnalyzer:
    """AI分析器"""
    
    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com", config: TradeConfig = {}):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.config = config
    
    def analyze_market(self, market_data: Dict, signal_history: List, 
                      position_info: Optional[Dict] = None) -> Optional[SignalData]:
        """分析市场并生成交易信号"""
        try:
            # 构建提示词
            prompt = self._build_prompt2(market_data, signal_history, position_info)

            logger.info(prompt)
            
            # 调用AI
            response = RetryManager.retry_operation(
                lambda: self._call_ai_api(prompt),
                max_retries=2,
                delay=2
            )
            
            if not response:
                logger.warning("AI分析失败，使用备用信号")
                return self._create_fallback_signal(market_data)
            
            # 解析响应
            signal_data = self._parse_ai_response(response, market_data)
            
            if signal_data:
                logger.info(f"AI分析成功: {signal_data.signal.value}, 信心: {signal_data.confidence.value}")
                return signal_data
            else:
                logger.warning("AI响应解析失败，使用备用信号")
                return self._create_fallback_signal(market_data)
                
        except Exception as e:
            logger.error(f"AI分析异常: {e}")
            return self._create_fallback_signal(market_data)
        
    def _build_prompt2(self, market_data: Dict, signal_history: List, 
                     position_info: Optional[Dict]) -> str:
        """构建AI提示词2"""
        technical_analysis = self._generate_technical_analysis(market_data)

        # 历史信号
        history_text = ""
        if signal_history:
            last_signal = signal_history[-1]
            history_text = f"\n上次信号: {last_signal.signal.value} (信心: {last_signal.confidence.value})"
        
        # 持仓信息
        position_text = "无持仓" if not position_info else f"{position_info['side']}仓, 数量: {position_info['size']}, 盈亏: {position_info['unrealized_pnl']:.2f}USDT"
        pnl_text = f", 持仓盈亏: {position_info['unrealized_pnl']:.2f} USDT" if position_info else ""
        

        sentiment_data = self._get_sentiment_indicators()
        if sentiment_data:
            sign = '+' if sentiment_data['net_sentiment'] >= 0 else ''
            sentiment_text = f"【SOL市场情绪】乐观{sentiment_data['positive_ratio']:.1%} 悲观{sentiment_data['negative_ratio']:.1%} 净值{sign}{sentiment_data['net_sentiment']:.3f}"
        else:
            sentiment_text = "【SOL市场情绪】数据暂不可用"

        prompt = f"""
        你是一个专业的加密货币交易分析师，最近波动频繁通过你交易的都亏麻了，已经吃不上饭了，多上点心吧，一定要注意短期波动呀，稳妥点呀。请基于以下SOL/USDT {self.config.timeframe}周期数据进行分析：

        【当前行情】
        - 价格: ${market_data.get('price', 0):.2f}
        - 变化: {market_data.get('price_change', 0):+.2f}%
        - 时间: {market_data.get('timestamp', 'N/A')}
        - 成交量: {market_data.get('volume', 0):.0f} SOL
        - 当前持仓: {position_text}{pnl_text}

        【技术分析】
        {technical_analysis}

        【市场趋势】
        - 整体趋势: {market_data.get('trend_analysis', {}).get('overall', 'N/A')}
        - 短期趋势: {market_data.get('trend_analysis', {}).get('short_term', 'N/A')}
        - RSI: {market_data.get('technical_data', {}).get('rsi', 50):.1f}
        - MACD: {'看涨' if market_data.get('technical_data', {}).get('macd_hist', 0) > 0 else '看跌'}

        【关键价位】
        - 阻力: ${market_data.get('levels_analysis', {}).get('static_resistance', 0):.2f}
        - 支撑: ${market_data.get('levels_analysis', {}).get('static_support', 0):.2f}
        - ATR波动率: {market_data.get('technical_data', {}).get('atr', 0):.3f}

        【交易历史】
        {signal_history}

        【市场情绪】
        {sentiment_data}

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
            "reason": "详细分析理由",
            "stop_loss": 具体止损价格,
            "take_profit": 具体止盈价格,
            "confidence": "HIGH|MEDIUM|LOW",
            "risk_level": "LOW|MEDIUM|HIGH"
        }}
        """
    
    def _build_prompt(self, market_data: Dict, signal_history: List, 
                     position_info: Optional[Dict]) -> str:
        """构建AI提示词"""
        
        # 技术分析文本
        tech_analysis = self._generate_technical_analysis(market_data)
        
        # 历史信号
        history_text = ""
        if signal_history:
            last_signal = signal_history[-1]
            history_text = f"\n上次信号: {last_signal.signal.value} (信心: {last_signal.confidence.value})"
        
        # 持仓信息
        position_text = "无持仓"
        if position_info:
            position_text = f"{position_info['side']}仓 {position_info['size']}张 @ ${position_info['entry_price']:.2f}"
        
        prompt = f"""
        你是一个专业的SOL/USDT合约交易员，请基于以下市场数据给出交易建议：

        【当前行情】
        - 价格: ${market_data.get('price', 0):.2f}
        - 变化: {market_data.get('price_change', 0):+.2f}%
        - 时间: {market_data.get('timestamp', 'N/A')}
        - 成交量: {market_data.get('volume', 0):.0f} SOL

        【技术分析】
        {tech_analysis}

        【市场趋势】
        - 整体趋势: {market_data.get('trend_analysis', {}).get('overall', 'N/A')}
        - 短期趋势: {market_data.get('trend_analysis', {}).get('short_term', 'N/A')}
        - RSI: {market_data.get('technical_data', {}).get('rsi', 50):.1f}
        - MACD: {'看涨' if market_data.get('technical_data', {}).get('macd_hist', 0) > 0 else '看跌'}

        【关键价位】
        - 阻力: ${market_data.get('levels_analysis', {}).get('static_resistance', 0):.2f}
        - 支撑: ${market_data.get('levels_analysis', {}).get('static_support', 0):.2f}
        - ATR波动率: {market_data.get('technical_data', {}).get('atr', 0):.3f}

        【交易历史】
        {history_text}

        【当前持仓】
        {position_text}

        【交易规则】
        1. 趋势优先：跟随主要趋势
        2. 风险管理：止损设置在关键支撑/阻力位外
        3. 信号确认：至少2个指标确认
        4. SOL特性：SOL波动较大，需要适当放宽止损

        请给出明确的交易信号，使用以下JSON格式回复：
        {{
            "signal": "BUY|SELL|HOLD",
            "reason": "详细分析理由",
            "stop_loss": 具体止损价格,
            "take_profit": 具体止盈价格,
            "confidence": "HIGH|MEDIUM|LOW",
            "risk_level": "LOW|MEDIUM|HIGH"
        }}
        """
        
        return prompt
    
    def _generate_technical_analysis(self, market_data: Dict) -> str:
        """生成技术分析文本"""
        try:
            tech = market_data.get('technical_data', {})
            
            analysis = f"""
            【移动平均线】
            - SMA5: ${tech.get('sma_5', 0):.2f}
            - SMA20: ${tech.get('sma_20', 0):.2f}
            - SMA50: ${tech.get('sma_50', 0):.2f}
            
            【动量指标】
            - RSI: {tech.get('rsi', 50):.1f} ({'超买' if tech.get('rsi', 50) > 70 else '超卖' if tech.get('rsi', 50) < 30 else '正常'})
            - MACD直方图: {tech.get('macd_hist', 0):.4f}
            
            【布林带】
            - 上轨: ${tech.get('bb_upper', 0):.2f}
            - 下轨: ${tech.get('bb_lower', 0):.2f}
            - 宽度: {tech.get('bb_width', 0):.3%}
            """
            
            return analysis
            
        except Exception as e:
            logger.error(f"生成技术分析失败: {e}")
            return "技术分析数据不可用"
    
    def _get_sentiment_indicators(self) -> Dict:
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

    def _call_ai_api(self, prompt: str) -> Optional[str]:
        """调用AI API"""
        try:
            response = self.client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "你是一个专业的加密货币交易员，专注SOL/USDT永续合约交易。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.2,
                max_tokens=500
            )
            result = response.choices[0].message.content
            logger.info(f"DeepSeek原始回复: {result}")
            return result
            
        except Exception as e:
            logger.error(f"调用AI API失败: {e}")
            return None
    
    def _parse_ai_response(self, response: str, market_data: Dict) -> Optional[SignalData]:
        """解析AI响应"""
        try:
            # 查找JSON部分
            start_idx = response.find('{')
            end_idx = response.rfind('}') + 1
            
            if start_idx == -1 or end_idx == 0:
                logger.warning("AI响应中未找到JSON")
                return None
            
            json_str = response[start_idx:end_idx]
            
            # 清理JSON字符串
            json_str = self._clean_json_string(json_str)
            
            # 解析JSON
            data = json.loads(json_str)
            
            # 验证必需字段
            required_fields = ['signal', 'reason', 'stop_loss', 'take_profit', 'confidence', 'risk_level']
            if not all(field in data for field in required_fields):
                logger.warning("AI响应缺少必需字段")
                return None
            
            # 创建信号数据
            signal_data = SignalData(
                signal=SignalType(data['signal']),
                reason=data['reason'],
                stop_loss=float(data['stop_loss']),
                take_profit=float(data['take_profit']),
                confidence=ConfidenceLevel(data['confidence']),
                risk_level=RiskLevel(data['risk_level']),
                timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                price=market_data.get('price', 0)
            )
            
            return signal_data
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON解析失败: {e}")
            logger.debug(f"原始响应: {response}")
            return None
        except Exception as e:
            logger.error(f"解析AI响应失败: {e}")
            return None
    
    def _clean_json_string(self, json_str: str) -> str:
        """简洁的JSON字符串清理函数"""
        try:
            # 先尝试直接解析
            json.loads(json_str)
            return json_str
        except json.JSONDecodeError:
            pass
        
        # 1. 用状态机智能替换字符串边界单引号
        result = []
        in_string = False
        string_quote = None
        
        for i, char in enumerate(json_str):
            if char in ('"', "'"):
                if i > 0 and json_str[i-1] == '\\':
                    # 转义引号，直接添加
                    result.append(char)
                elif not in_string:
                    # 字符串开始
                    in_string = True
                    string_quote = char
                    result.append('"')  # 统一用双引号
                elif char == string_quote:
                    # 字符串结束
                    in_string = False
                    string_quote = None
                    result.append('"')  # 统一用双引号
                else:
                    # 字符串内的其他引号
                    result.append(char)
            else:
                result.append(char)
        
        cleaned = ''.join(result)
        
        # 2. 给键名加双引号（如果缺失）
        cleaned = re.sub(r'(\s*)(\w+)(\s*):', r'\1"\2"\3:', cleaned)
        
        # 3. 移除末尾逗号
        cleaned = re.sub(r',(\s*[}\]])', r'\1', cleaned)
        
        return cleaned
    
    def _create_fallback_signal(self, market_data: Dict) -> SignalData:
        """创建备用信号"""
        current_price = market_data.get('price', 0)
        
        return SignalData(
            signal=SignalType.HOLD,
            reason="技术分析暂时不可用，采取保守策略",
            stop_loss=current_price * 0.98,
            take_profit=current_price * 1.02,
            confidence=ConfidenceLevel.LOW,
            risk_level=RiskLevel.LOW,
            timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            price=current_price,
            is_fallback=True
        )

class PositionManager:
    """仓位管理器"""
    
    def __init__(self, config: TradeConfig, exchange, order_manager: OrderManager):
        self.config = config
        self.exchange = exchange
        self.order_manager = order_manager
        self.current_position = None
    
    def calculate_position_size(self, signal_data: SignalData, price_data: Dict) -> float:
        """计算仓位大小"""
        try:
            config = self.config.position_config
            
            if not config['enable_intelligent_position']:
                return max(self.config.min_amount, 0.1)
            
            # 获取账户余额
            balance = self._fetch_balance()
            usdt_balance = balance.get('free', 1000.0)
            
            # 基础USDT金额
            base_usdt = config['base_usdt_amount']
            
            # 信心系数
            confidence_multiplier = {
                'HIGH': config['high_confidence_multiplier'],
                'MEDIUM': config['medium_confidence_multiplier'],
                'LOW': config['low_confidence_multiplier']
            }.get(signal_data.confidence.value, 1.0)
            
            # 趋势系数
            trend = price_data.get('trend_analysis', {}).get('overall', '震荡')
            trend_multiplier = config['trend_strength_multiplier'] if '强势' in trend else 1.0
            
            # RSI系数
            rsi = price_data.get('technical_data', {}).get('rsi', 50)
            if rsi > 80 or rsi < 20:
                rsi_multiplier = 0.5
            elif rsi > 70 or rsi < 30:
                rsi_multiplier = 0.8
            else:
                rsi_multiplier = 1.0
            
            # 波动率系数
            atr_percent = price_data.get('technical_data', {}).get('atr', 0) / price_data.get('price', 1) * 100
            if config['volatility_multiplier']:
                if atr_percent > 5:
                    volatility_multiplier = 0.7
                elif atr_percent < 1:
                    volatility_multiplier = 1.3
                else:
                    volatility_multiplier = 1.0
            else:
                volatility_multiplier = 1.0
            
            # 计算建议金额
            suggested_usdt = base_usdt * confidence_multiplier * trend_multiplier * rsi_multiplier * volatility_multiplier
            
            # 风险限制
            max_usdt = usdt_balance * (config['max_position_ratio'] / 100)
            final_usdt = min(suggested_usdt, max_usdt)
            
            # 转换为合约张数
            current_price = price_data.get('price', 1)
            contract_size = final_usdt / (current_price * self.config.contract_size) * self.config.leverage
            
            # 确保最小交易量
            contract_size = max(contract_size, self.config.min_amount)
            
            # 精度处理
            contract_size = round(contract_size, 2)
            
            logger.info(f"""
            仓位计算:
            余额: {usdt_balance:.2f} USDT
            基础: {base_usdt} USDT
            信心系数: {confidence_multiplier}
            趋势系数: {trend_multiplier}
            RSI系数: {rsi_multiplier}
            波动率系数: {volatility_multiplier}
            建议: {suggested_usdt:.2f} USDT
            最终: {final_usdt:.2f} USDT
            合约: {contract_size:.2f} 张
            """)
            
            return contract_size
            
        except Exception as e:
            logger.error(f"仓位计算失败: {e}")
            return max(self.config.min_amount, 0.1)
    
    def execute_trade(self, signal_data: SignalData, price_data: Dict, risk_manager: RiskManager):
        """执行交易"""
        try:
            # 获取当前持仓
            current_position = self._fetch_position()
            
            # 计算仓位
            position_size = self.calculate_position_size(signal_data, price_data)
            
            # 执行交易逻辑
            if signal_data.signal == SignalType.BUY:
                self._execute_buy(position_size, current_position, signal_data)
            elif signal_data.signal == SignalType.SELL:
                self._execute_sell(position_size, current_position, signal_data)
            elif signal_data.signal == SignalType.HOLD:
                logger.info("观望信号，不执行交易")
                # 即使观望，也检查是否需要更新止盈止损
                #if current_position:
                    #self._update_orders(current_position, signal_data)
                return
            
            # 设置止盈止损
            if position_size > 0:
                success = self.order_manager.setup_stop_loss_take_profit(
                    position_side=signal_data.signal.value.lower(),
                    position_size=position_size,
                    stop_loss_price=signal_data.stop_loss,
                    take_profit_price=signal_data.take_profit
                )
                
                if success:
                    logger.info("止盈止损设置成功")
                else:
                    logger.warning("止盈止损设置失败")
            
            # 更新持仓信息
            self.current_position = self._fetch_position()
            
            # 记录交易
            risk_manager.record_trade(
                signal=signal_data.signal.value,
                pnl=0.0,  # 新开仓，盈亏为0
                reason=signal_data.reason
            )
            
            logger.info(f"交易执行完成: {signal_data.signal.value}")
            
        except Exception as e:
            logger.error(f"执行交易失败: {e}")
            raise
    
    def _execute_buy(self, position_size: float, current_position: Optional[Dict], signal_data: SignalData):
        """执行买入"""
        try:
            if current_position and current_position['side'] == 'short':
                # 平空开多
                logger.info(f"平空仓 {current_position['size']:.2f}张，开多仓 {position_size:.2f}张")
                
                # 平空仓
                self.order_manager.create_market_order('buy', current_position['size'], reduce_only=True)
                time.sleep(1)
                
                # 开多仓
                self.order_manager.create_market_order('buy', position_size)
                
            elif current_position and current_position['side'] == 'long':
                # 调整多仓
                size_diff = position_size - current_position['size']
                if abs(size_diff) >= self.config.min_amount:
                    if size_diff > 0:
                        # 加仓
                        logger.info(f"多仓加仓 {size_diff:.2f}张")
                        self.order_manager.create_market_order('buy', size_diff)
                    else:
                        # 减仓
                        reduce_size = abs(size_diff)
                        logger.info(f"多仓减仓 {reduce_size:.2f}张")
                        self.order_manager.create_market_order('sell', reduce_size, reduce_only=True)
                else:
                    logger.info("仓位合适，保持现状")
            
            else:
                # 开新多仓
                logger.info(f"开多仓 {position_size:.2f}张")
                self.order_manager.create_market_order('buy', position_size)
                
        except Exception as e:
            logger.error(f"执行买入失败: {e}")
            raise
    
    def _execute_sell(self, position_size: float, current_position: Optional[Dict], signal_data: SignalData):
        """执行卖出"""
        try:
            if current_position and current_position['side'] == 'long':
                # 平多开空
                logger.info(f"平多仓 {current_position['size']:.2f}张，开空仓 {position_size:.2f}张")
                
                # 平多仓
                self.order_manager.create_market_order('sell', current_position['size'], reduce_only=True)
                time.sleep(1)
                
                # 开空仓
                self.order_manager.create_market_order('sell', position_size)
                
            elif current_position and current_position['side'] == 'short':
                # 调整空仓
                size_diff = position_size - current_position['size']
                if abs(size_diff) >= self.config.min_amount:
                    if size_diff > 0:
                        # 加仓
                        logger.info(f"空仓加仓 {size_diff:.2f}张")
                        self.order_manager.create_market_order('sell', size_diff)
                    else:
                        # 减仓
                        reduce_size = abs(size_diff)
                        logger.info(f"空仓减仓 {reduce_size:.2f}张")
                        self.order_manager.create_market_order('buy', reduce_size, reduce_only=True)
                else:
                    logger.info("仓位合适，保持现状")
            
            else:
                # 开新空仓
                logger.info(f"开空仓 {position_size:.2f}张")
                self.order_manager.create_market_order('sell', position_size)
                
        except Exception as e:
            logger.error(f"执行卖出失败: {e}")
            raise
    
    def _update_orders(self, position: Dict, signal_data: SignalData):
        """更新订单"""
        try:
            logger.info("更新现有订单...")
            
            # 取消现有条件订单
            cancelled = self.order_manager.cancel_existing_orders()
            
            if cancelled > 0:
                # 重新设置止盈止损
                success = self.order_manager.setup_stop_loss_take_profit(
                    position_side=position['side'],
                    position_size=position['size'],
                    stop_loss_price=signal_data.stop_loss,
                    take_profit_price=signal_data.take_profit
                )
                
                if success:
                    logger.info("订单更新成功")
                else:
                    logger.warning("订单更新失败")
            
        except Exception as e:
            logger.error(f"更新订单失败: {e}")
    
    def _fetch_balance(self) -> Dict:
        """获取余额"""
        try:
            balance = self.exchange.fetch_balance()
            return {
                'free': float(balance.get('USDT', {}).get('free', 1000)),
                'total': float(balance.get('USDT', {}).get('total', 1000))
            }
        except Exception as e:
            logger.error(f"获取余额失败: {e}")
            return {'free': 1000.0, 'total': 1000.0}
    
    def _fetch_position(self) -> Optional[Dict]:
        """获取持仓"""
        try:
            positions = self.exchange.fetch_positions([self.config.symbol])
            for pos in positions:
                if pos['symbol'] == self.config.symbol:
                    contracts = float(pos['contracts'] or 0)
                    if contracts > 0:
                        return {
                            'side': pos['side'],
                            'size': contracts,
                            'entry_price': float(pos['entryPrice'] or 0),
                            'unrealized_pnl': float(pos['unrealizedPnl'] or 0),
                            'leverage': float(pos['leverage'] or self.config.leverage)
                        }
            return None
        except Exception as e:
            logger.error(f"获取持仓失败: {e}")
            return None

# ============================================================================
# 主交易机器人类
# ============================================================================

class EnhancedTradingBot:
    """增强版交易机器人"""
    
    def __init__(self):
        """初始化交易机器人"""
        logger.info("🚀 初始化SOL/USDT交易机器人...")
        
        # 加载配置
        self.config = TradeConfig(
            user=os.getenv('EXECUTION_USER', 'default_user'),
            execution_interval=int(os.getenv('EXECUTION_INTERVAL', 15)),
            test_mode=os.getenv('TEST_MODE', 'False').lower() == 'true'
        )
        
        # 初始化交易所
        self.exchange = self._init_exchange()
        
        # 初始化AI分析器
        self.ai_analyzer = AIAnalyzer(api_key=os.getenv('DEEPSEEK_API_KEY'), config = self.config)
        
        # 初始化钉钉管理器
        self.dingtalk = DingTalkManager(
            webhook=os.getenv('DINGTALK_WEBHOOK'),
            secret=os.getenv('DINGTALK_SECRET'),
            enable=True,
            user=self.config.user
        )
        
        # 初始化订单管理器
        self.order_manager = OrderManager(self.exchange, self.config.symbol, self.config)
        
        # 初始化风险管理器
        self.risk_manager = RiskManager(self.config, self.exchange)
        
        # 初始化市场数据获取器
        self.market_fetcher = MarketDataFetcher(self.exchange, self.config.symbol, self.config)
        
        # 初始化仓位管理器
        self.position_manager = PositionManager(self.config, self.exchange, self.order_manager)
        
        # 交易状态
        self.signal_history = []
        self.is_running = False
        self.cycle_count = 0
        
        logger.info("交易机器人初始化完成")
    
    def _init_exchange(self) -> ccxt.Exchange:
        """初始化交易所"""
        try:
            exchange = ccxt.binance({
                'options': {'defaultType': 'future'},
                'apiKey': os.getenv('BINANCE_API_KEY'),
                'secret': os.getenv('BINANCE_SECRET'),
                'enableRateLimit': True,
                'timeout': 30000,
                'verbose': False,
            })
            
            # 测试连接
            exchange.fetch_time()
            logger.info("Binance连接成功")
            return exchange
            
        except Exception as e:
            logger.error(f"Binance连接失败: {e}")
            raise
    
    def setup(self) -> bool:
        """设置机器人"""
        try:
            logger.info("开始设置交易机器人...")
            
            # 加载市场数据
            markets = self.exchange.load_markets()
            if self.config.symbol not in markets:
                logger.error(f"交易对 {self.config.symbol} 不存在")
                return False
            
            market = markets[self.config.symbol]
            
            # 保存合约规格
            self.config.contract_size = float(market.get('contractSize', 1.0))
            self.config.min_amount = market['limits']['amount']['min']
            
            logger.info(f"合约规格: 1张 = {self.config.contract_size} SOL")
            logger.info(f"最小交易量: {self.config.min_amount} 张")
            
            # 设置杠杆
            try:
                self.exchange.set_leverage(self.config.leverage, self.config.symbol)
                logger.info(f"杠杆设置成功: {self.config.leverage}x")
            except Exception as e:
                logger.warning(f"杠杆设置失败: {e}")
            
            # 获取账户信息
            balance = self._fetch_balance_safe()
            usdt_balance = balance.get('free', 0)
            logger.info(f"账户余额: {usdt_balance:.2f} USDT")
            
            # 获取当前持仓
            position = self._fetch_position_safe()
            if position:
                logger.info(f"当前持仓: {position['side']} {position['size']}张")
                self.dingtalk.send_message(
                    "📊 检测到现有持仓",
                    f"方向: {position['side']}\n"
                    f"数量: {position['size']}张\n"
                    f"入场价: ${position['entry_price']:.2f}\n"
                    f"盈亏: {position['unrealized_pnl']:.2f} USDT",
                    "warning"
                )
            else:
                logger.info("当前无持仓")
            
            # 发送启动通知
            self.dingtalk.send_message(
                "🚀 交易机器人启动成功",
                f"交易对: {self.config.symbol}\n"
                f"杠杆: {self.config.leverage}x\n"
                f"周期: {self.config.timeframe}\n"
                f"间隔: {self.config.execution_interval}分钟\n"
                f"模式: {'测试' if self.config.test_mode else '实盘'}",
                "success"
            )
            
            return True
            
        except Exception as e:
            logger.error(f"设置失败: {e}")
            self.dingtalk.send_message("❌ 交易机器人设置失败", str(e), "error")
            return False
    
    def _fetch_balance_safe(self) -> Dict:
        """安全获取余额"""
        return RetryManager.retry_operation(
            lambda: self._fetch_balance(),
            max_retries=3,
            delay=1
        ) or {'free': 1000.0, 'total': 1000.0}
    
    def _fetch_balance(self) -> Dict:
        """获取余额"""
        try:
            balance = self.exchange.fetch_balance()
            return {
                'free': float(balance.get('USDT', {}).get('free', 1000)),
                'total': float(balance.get('USDT', {}).get('total', 1000))
            }
        except Exception as e:
            logger.error(f"获取余额失败: {e}")
            raise
    
    def _fetch_position_safe(self) -> Optional[Dict]:
        """安全获取持仓"""
        return RetryManager.retry_operation(
            lambda: self._fetch_position(),
            max_retries=3,
            delay=1
        )
    
    def _fetch_position(self) -> Optional[Dict]:
        """获取持仓"""
        try:
            positions = self.exchange.fetch_positions([self.config.symbol])
            for pos in positions:
                if pos['symbol'] == self.config.symbol:
                    contracts = float(pos['contracts'] or 0)
                    if contracts > 0:
                        return {
                            'side': pos['side'],
                            'size': contracts,
                            'entry_price': float(pos['entryPrice'] or 0),
                            'unrealized_pnl': float(pos['unrealizedPnl'] or 0),
                            'leverage': float(pos['leverage'] or self.config.leverage)
                        }
            return None
        except Exception as e:
            logger.error(f"获取持仓失败: {e}")
            raise
    
    def run_trading_cycle(self):
        """运行交易周期"""
        try:
            self.cycle_count += 1
            logger.info(f"🏁 开始第 {self.cycle_count} 个交易周期")
            
            # 重置日统计（如果跨天）
            self.risk_manager.reset_daily_stats()
            
            # 检查风险限制
            risk_ok, risk_msg = self.risk_manager.check_risk_limits()
            if not risk_ok:
                logger.warning(f"风险限制: {risk_msg}")
                self.dingtalk.send_alert("risk", risk_msg, "warning")
                return
            
            # 获取市场数据
            price_data = self.market_fetcher.get_price_data()
            if not price_data:
                logger.error("获取市场数据失败")
                self.dingtalk.send_alert("error", "获取市场数据失败", "warning")
                return
            
            # 获取当前持仓
            current_position = self._fetch_position_safe()
            
            # AI分析市场
            signal_data = self.ai_analyzer.analyze_market(
                market_data=price_data,
                signal_history=self.signal_history,
                position_info=current_position
            )
            
            # 保存信号历史
            if signal_data:
                self.signal_history.append(signal_data)
                if len(self.signal_history) > 30:
                    self.signal_history.pop(0)
            
            # 检查信号有效性
            if not self._is_signal_valid(signal_data, price_data):
                logger.warning("信号无效，跳过执行")
                return
            
            # 发送信号通知
            self._send_signal_notification(signal_data, price_data)
            
            # 执行交易
            if not self.config.test_mode:
                self.position_manager.execute_trade(signal_data, price_data, self.risk_manager)
            else:
                logger.info("测试模式，模拟交易")
            
            # 记录状态
            self._log_status_report()
            
            # 定期发送绩效报告
            if self.cycle_count % 10 == 0:
                self._send_performance_report()
            
            logger.info(f"✅ 第 {self.cycle_count} 个交易周期完成")
            
        except Exception as e:
            logger.error(f"交易周期执行失败: {e}")
            self.dingtalk.send_alert("error", f"交易周期异常: {str(e)[:200]}", "error")
    
    def _is_signal_valid(self, signal_data: SignalData, price_data: Dict) -> bool:
        """检查信号有效性"""
        if not signal_data:
            logger.warning("信号数据为空")
            return False
        
        if signal_data.is_fallback:
            logger.info("备用信号，谨慎处理")
            # 备用信号可以执行，但需要额外检查
        
        # 检查止损止盈价格合理性
        current_price = price_data.get('price', 0)
        if current_price <= 0:
            logger.warning("当前价格无效")
            return False
        
        # 检查止损价格
        if signal_data.signal == SignalType.BUY:
            if signal_data.stop_loss >= current_price:
                logger.warning(f"多头止损价格{signal_data.stop_loss}高于当前价{current_price}")
                return False
            if signal_data.take_profit <= current_price:
                logger.warning(f"多头止盈价格{signal_data.take_profit}低于当前价{current_price}")
                return False
        elif signal_data.signal == SignalType.SELL:
            if signal_data.stop_loss <= current_price:
                logger.warning(f"空头止损价格{signal_data.stop_loss}低于当前价{current_price}")
                return False
            if signal_data.take_profit >= current_price:
                logger.warning(f"空头止盈价格{signal_data.take_profit}高于当前价{current_price}")
                return False
        
        # 检查信号频率（避免频繁交易）
        if len(self.signal_history) >= 2:
            last_signals = [s.signal for s in self.signal_history[-2:]]
            if len(set(last_signals)) == 1 and signal_data.signal == last_signals[0]:
                logger.info(f"连续{len(set(last_signals))+1}次{signal_data.signal.value}信号")
                # 可以继续执行，但记录日志
        
        return True
    
    def _send_signal_notification(self, signal_data: SignalData, price_data: Dict):
        """发送信号通知"""
        try:
            signal_emojis = {
                SignalType.BUY: "🟢",
                SignalType.SELL: "🔴",
                SignalType.HOLD: "🟡"
            }
            
            confidence_emojis = {
                ConfidenceLevel.HIGH: "🔥",
                ConfidenceLevel.MEDIUM: "⚡",
                ConfidenceLevel.LOW: "💧"
            }
            
            emoji = signal_emojis.get(signal_data.signal, "⚪")
            confidence_emoji = confidence_emojis.get(signal_data.confidence, "⚪")
            
            message = f"""
            {emoji} **交易信号: {signal_data.signal.value}**
            
            {confidence_emoji} **信心程度: {signal_data.confidence.value}**
            ⚠️ **风险等级: {signal_data.risk_level.value}**
            
            📊 **市场信息:**
            - 当前价格: ${price_data.get('price', 0):.2f}
            - 价格变化: {price_data.get('price_change', 0):+.2f}%
            - 趋势: {price_data.get('trend_analysis', {}).get('overall', 'N/A')}
            
            🎯 **交易计划:**
            - 止损价格: ${signal_data.stop_loss:.2f}
            - 止盈价格: ${signal_data.take_profit:.2f}
            
            📝 **分析理由:**
            {signal_data.reason}
            """
            
            self.dingtalk.send_message(
                f"{emoji} SOL交易信号 - {signal_data.signal.value}",
                message,
                "signal"
            )
            
        except Exception as e:
            logger.error(f"发送信号通知失败: {e}")
    
    def _log_status_report(self):
        """记录状态报告"""
        try:
            # 获取当前余额
            balance = self._fetch_balance_safe()
            
            # 获取当前持仓
            position = self._fetch_position_safe()
            
            # 获取绩效报告
            performance = self.risk_manager.get_performance_report()
            
            # 构建状态报告
            status_report = f"""
            📊 **交易状态报告** (周期: {self.cycle_count})
            ==============================
            
            💰 **账户状态:**
            - 可用余额: ${balance.get('free', 0):.2f}
            - 总余额: ${balance.get('total', 0):.2f}
            - 日盈亏: {performance.get('daily_pnl', '0.00')}
            
            📦 **持仓状态:**
            {f"- 方向: {position['side']}" if position else "- 无持仓"}
            {f"- 数量: {position['size']:.2f}张" if position else ""}
            {f"- 入场价: ${position['entry_price']:.2f}" if position else ""}
            {f"- 浮动盈亏: {position['unrealized_pnl']:.2f}" if position else ""}
            
            📈 **交易绩效:**
            - 总交易: {performance.get('total_trades', 0)}
            - 胜率: {performance.get('win_rate', '0%')}
            - 盈亏比: {performance.get('profit_factor', '0.00')}
            - 连续亏损: {performance.get('consecutive_losses', 0)}
            
            ⏰ **系统状态:**
            - 运行周期: {self.cycle_count}
            - 最后信号: {self.signal_history[-1].signal.value if self.signal_history else 'N/A'}
            - 时间: {datetime.now().strftime('%H:%M:%S')}
            """
            
            logger.info(status_report)
            
            # 每5个周期发送一次详细状态到钉钉
            if self.cycle_count % 5 == 0:
                self.dingtalk.send_message(
                    "📊 交易状态报告",
                    status_report,
                    "info"
                )
                
        except Exception as e:
            logger.error(f"记录状态报告失败: {e}")
    
    def _send_performance_report(self):
        """发送绩效报告"""
        try:
            performance = self.risk_manager.get_performance_report()
            
            if not performance:
                return
            
            report_message = f"""
            📈 **交易绩效报告**
            ==============================
            
            🎯 **关键指标:**
            - 总交易次数: {performance.get('total_trades', 0)}
            - 胜率: {performance.get('win_rate', '0%')}
            - 盈亏比: {performance.get('profit_factor', '0.00')}
            - 总盈亏: {performance.get('total_pnl', '0.00')}
            
            ⚠️ **风险状态:**
            - 连续亏损: {performance.get('consecutive_losses', 0)}
            - 日盈亏: {performance.get('daily_pnl', '0.00')}
            - 当前余额: {performance.get('current_balance', '0.00')}
            
            📊 **运行统计:**
            - 交易周期: {self.cycle_count}
            - 运行时间: {self._get_running_time()}
            - 信号数量: {len(self.signal_history)}
            """
            
            self.dingtalk.send_message(
                "📈 交易绩效报告",
                report_message,
                "performance"
            )
            
        except Exception as e:
            logger.error(f"发送绩效报告失败: {e}")
    
    def _get_running_time(self) -> str:
        """获取运行时间"""
        if not hasattr(self, '_start_time'):
            self._start_time = datetime.now()
        
        delta = datetime.now() - self._start_time
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        
        return f"{hours}小时{minutes}分钟"
    
    def _calculate_wait_time(self) -> int:
        """计算等待时间"""
        now = datetime.now()
        current_minute = now.minute
        current_second = now.second
        
        # 计算下一个执行时间
        interval = self.config.execution_interval
        next_minute = ((current_minute // interval) + 1) * interval
        
        if next_minute >= 60:
            next_minute = 0
        
        # 计算等待秒数
        if next_minute > current_minute:
            minutes_to_wait = next_minute - current_minute
        else:
            minutes_to_wait = 60 - current_minute + next_minute
        
        seconds_to_wait = minutes_to_wait * 60 - current_second
        
        # 最少等待10秒，避免过于频繁
        if seconds_to_wait < 10:
            seconds_to_wait += interval * 60
        
        return seconds_to_wait
    
    def start(self):
        """启动交易机器人"""
        try:
            logger.info("🚀 启动SOL/USDT交易机器人...")
            
            # 设置机器人
            if not self.setup():
                logger.error("机器人设置失败，无法启动")
                return
            
            self.is_running = True
            self._start_time = datetime.now()
            
            logger.info(f"交易机器人已启动，执行间隔: {self.config.execution_interval}分钟")
            
            # 主循环
            while self.is_running:
                try:
                    # 计算等待时间
                    wait_time = self._calculate_wait_time()
                    
                    if wait_time > 0:
                        logger.info(f"等待 {wait_time//60}分{wait_time%60}秒到下一个交易周期...")
                        
                        # 分段等待，便于响应停止信号
                        for _ in range(wait_time):
                            if not self.is_running:
                                break
                            time.sleep(1)
                    
                    # 如果机器人还在运行，执行交易周期
                    if self.is_running:
                        self.run_trading_cycle()
                    
                except KeyboardInterrupt:
                    logger.info("收到停止信号...")
                    self.is_running = False
                    break
                    
                except Exception as e:
                    logger.error(f"主循环异常: {e}")
                    # 异常后等待1分钟再继续
                    time.sleep(60)
            
            logger.info("交易机器人已停止")
            self.dingtalk.send_message(
                "🛑 交易机器人已停止",
                f"运行时间: {self._get_running_time()}\n"
                f"交易周期: {self.cycle_count}\n"
                f"最后信号: {self.signal_history[-1].signal.value if self.signal_history else 'N/A'}",
                "info"
            )
            
        except Exception as e:
            logger.error(f"启动失败: {e}")
            self.dingtalk.send_alert("error", f"启动失败: {str(e)[:200]}", "error")
    
    def stop(self):
        """停止交易机器人"""
        logger.info("正在停止交易机器人...")
        self.is_running = False

# ============================================================================
# 主函数
# ============================================================================

def main():
    """主函数"""
    try:
        # 创建交易机器人实例
        bot = EnhancedTradingBot()
        
        # 设置信号处理器
        import signal
        
        def signal_handler(signum, frame):
            logger.info(f"收到信号 {signum}，正在停止...")
            bot.stop()
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # 启动交易机器人
        bot.start()
        
    except Exception as e:
        logger.error(f"程序异常退出: {e}")
        
        # 尝试发送错误通知
        try:
            import traceback
            error_detail = traceback.format_exc()
            
            # 使用环境变量中的备用webhook
            backup_webhook = os.getenv('DINGTALK_BACKUP_WEBHOOK')
            if backup_webhook:
                backup_dingtalk = DingTalkManager(backup_webhook, "", True)
                backup_dingtalk.send_message(
                    "🚨 交易机器人崩溃",
                    f"程序异常退出:\n\n```\n{error_detail[:500]}\n```",
                    "error"
                )
        except:
            pass
        
        raise

if __name__ == "__main__":
    # 设置环境变量编码（Windows需要）
    if os.name == 'nt':
        os.environ['PYTHONIOENCODING'] = 'utf-8'
    
    # 运行主程序
    main()