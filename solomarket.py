#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Predict.fun Solo Market 自动挂单脚本
用户提供市场 URL/ID，脚本按 BestAsk 驱动自动挂单
"""

import os
import re
import math
import socket
import sys
import time
import yaml
import requests
import traceback
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from loguru import logger
from dotenv import load_dotenv
from predict_sdk import Side

from modules.models import OrderBook, OrderBookLevel, PredictOrder
from modules.predict_client import PredictClient

# Telegram 通知
TG_BOT_TOKEN = ""
TG_CHAT_ID = ""

def send_tg_notification(message: str, proxy: Dict = None):
    if not TG_CHAT_ID or not TG_BOT_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, data=data, timeout=10, proxies=proxy)
    except Exception as e:
        logger.warning(f"TG通知失败: {e}")


class PredictSoloMonitor:
    """Solo Market 自动挂单器"""

    DEFAULT_SIDE = "YES"

    @staticmethod
    def _normalize_markets_input(markets) -> List[str]:
        """兼容单个字符串和字符串列表两种配置写法。"""
        if markets is None:
            return []
        if isinstance(markets, str):
            value = markets.strip()
            return [value] if value else []
        if isinstance(markets, list):
            result = []
            for item in markets:
                if item is None:
                    continue
                value = str(item).strip()
                if value:
                    result.append(value)
            return result
        raise ValueError("solo_market.markets 必须是字符串或字符串列表")

    def __init__(self, config: Dict):
        socket.setdefaulttimeout(20)
        self.config = config
        solo = config.get('solo_market', {})

        # 市场配置 (支持 URL / slug / market_id)
        self.markets_input = self._normalize_markets_input(solo.get('markets', []))
        self.option_name = (solo.get('option') or '').strip()
        self.order_side = str(solo.get('YON', self.DEFAULT_SIDE)).strip().upper()
        self.order_shares = solo.get('order_shares', 101)
        self.check_interval = int(solo.get('check_interval_seconds', 30))
        self.target_offset = float(solo.get('target_offset_cents', 2.0)) / 100.0
        self.lower_bound_offset = float(solo.get('lower_bound_offset_cents', 3.5)) / 100.0
        self.upper_bound_offset = float(solo.get('upper_bound_offset_cents', 1.3)) / 100.0
        if self.order_side not in {"YES", "NO"}:
            raise ValueError("solo_market.YON 只能是 YES 或 NO")
        if not (self.lower_bound_offset > self.target_offset > self.upper_bound_offset > 0):
            raise ValueError(
                "偏移参数必须满足: lower_bound_offset_cents > target_offset_cents > upper_bound_offset_cents > 0"
            )

        load_dotenv()

        global TG_BOT_TOKEN, TG_CHAT_ID
        tg = config.get('telegram', {})
        TG_BOT_TOKEN = tg.get('bot_token') or os.getenv('TELEGRAM_BOT_TOKEN')
        TG_CHAT_ID = tg.get('chat_id') or os.getenv('TELEGRAM_CHAT_ID')

        private_key = os.getenv('PREDICT_PRIVATE_KEY')
        api_key = os.getenv('PREDICT_API_KEY')
        wallet_address = os.getenv('PREDICT_WALLET_ADDRESS')
        predict_account = os.getenv('PREDICT_ACCOUNT')

        if not private_key or not api_key or not wallet_address:
            raise ValueError("缺少环境变量: PREDICT_PRIVATE_KEY, PREDICT_API_KEY, PREDICT_WALLET_ADDRESS")

        proxy_config = config.get('proxy', {})
        self.proxy = None
        if proxy_config.get('enabled'):
            self.proxy = {'http': proxy_config.get('http'), 'https': proxy_config.get('https')}

        self.client = PredictClient(
            private_key=private_key,
            api_key=api_key,
            wallet_address=wallet_address,
            predict_account=predict_account,
            proxy=self.proxy
        )

        self.wallet_address = wallet_address
        self.wallet_alias = os.getenv('PREDICT_WALLET_ALIAS', '')

        # 订单跟踪: market_key -> PredictOrder
        self.orders: Dict[str, PredictOrder] = {}
        # 市场信息缓存: market_key -> {market_id, title, token_id, fee_rate, ...}
        self.market_cache: Dict[str, Dict] = {}

        self.running = False
        self.last_report_time = 0
        self.report_interval = 2 * 3600  # 2 小时

    # ── 工具方法 ──────────────────────────────────────────────

    def _send_tg(self, message: str):
        if self.wallet_alias:
            label = f"🏷️ 别名: <b>{self.wallet_alias}</b>"
        else:
            short = f"{self.wallet_address[:6]}...{self.wallet_address[-4:]}"
            label = f"👤 钱包: <code>{short}</code>"
        footer = f"\n━━━━━━━━━━━━━━━\n{label}"
        if footer not in message:
            message += footer
        send_tg_notification(message, self.proxy)

    @staticmethod
    def _parse_market_input(raw: str) -> Tuple[str, str]:
        """
        解析市场输入，返回 (market_key, outcome)
        支持:
          - "https://predict.fun/market/cs2-prv-nip-2026-03-19"        -> slug, "YES"
          - "https://predict.fun/market/cs2-prv-nip-2026-03-19:PARI"   -> slug, "PARI"
          - "12474"                                                      -> id, "YES"
          - "12474:NIP"                                                  -> id, "NIP"
        """
        raw = raw.strip()
        # 提取 URL 中的 slug
        m = re.match(r'https?://predict\.fun/market/([^:\s]+)', raw)
        if m:
            rest = raw[m.end():]
            slug = m.group(1)
            outcome = rest.lstrip(':').strip() if rest.startswith(':') else "YES"
            return slug, outcome

        # market_id 或 market_id:outcome
        parts = raw.split(":", 1)
        key = parts[0].strip()
        outcome = parts[1].strip() if len(parts) > 1 else "YES"
        return key, outcome

    def _get_market_option_and_side(self, raw: str) -> Tuple[str, str, str]:
        """
        返回 (market_key, option_name, side)

        兼容两种写法:
          1. 新配置:
             markets: [url]
             option: "500m"
             YON: "NO"
          2. 老配置:
             markets: ["url:PARI"]  -> 默认买 PARI 的 YES
             markets: ["url:NO"]    -> 二元市场买 NO
        """
        market_key, inline_outcome = self._parse_market_input(raw)
        inline_outcome = inline_outcome.strip()

        if self.option_name:
            return market_key, self.option_name, self.order_side

        if inline_outcome.upper() in {"YES", "NO"}:
            return market_key, inline_outcome.upper(), inline_outcome.upper()

        return market_key, inline_outcome, self.order_side

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r'[^a-z0-9]+', '', (value or '').lower())

    def _pick_market_from_candidates(self, candidates: List[Dict], slug: str, option: str = "") -> Optional[str]:
        """从分类或搜索结果里挑出最匹配的 market id。"""
        if not candidates:
            return None

        normalized_option = self._normalize_text(option)
        normalized_slug = self._normalize_text(slug)

        if normalized_option:
            for market in candidates:
                texts = [
                    market.get('title', ''),
                    market.get('question', ''),
                    market.get('categorySlug', ''),
                ]
                for outcome in market.get('outcomes', []) or []:
                    texts.append(outcome.get('name', ''))

                if any(normalized_option in self._normalize_text(text) for text in texts if text):
                    return str(market.get('id'))

        for market in candidates:
            market_slug = market.get('categorySlug', '')
            if market_slug and self._normalize_text(market_slug) == normalized_slug:
                return str(market.get('id'))

        if len(candidates) == 1:
            return str(candidates[0].get('id'))

        return None

    def _resolve_slug_to_id(self, slug: str, outcome: str = "") -> Optional[str]:
        """优先通过官方 API，将 slug 转换为 market_id。"""
        if outcome and outcome.upper() != "YES":
            logger.debug(f"正在通过 API 解析 {slug} (选项: {outcome}) 的 market ID ...")
        else:
            logger.debug(f"正在通过 API 解析 market ID: {slug} ...")

        category = self.client.fetch_category_by_slug(slug)
        if category:
            markets = category.get('markets', []) or []
            picked = self._pick_market_from_candidates(markets, slug, outcome)
            if picked:
                logger.debug(f"分类解析成功: slug '{slug}' -> market ID {picked}")
                return picked
            logger.warning(f"分类已找到，但未在 slug={slug} 下匹配到选项 '{outcome}'")

        search_candidates = self.client.search_markets(slug, limit=20)
        picked = self._pick_market_from_candidates(search_candidates, slug, outcome)
        if picked:
            logger.debug(f"搜索解析成功: slug '{slug}' -> market ID {picked}")
            return picked

        if outcome:
            search_candidates = self.client.search_markets(outcome, limit=20)
            picked = self._pick_market_from_candidates(search_candidates, slug, outcome)
            if picked:
                logger.debug(f"选项搜索解析成功: slug '{slug}' / '{outcome}' -> market ID {picked}")
                return picked

        logger.debug(f"API 未解析出 slug={slug}，回退到网页解析")
        import requests as _req
        import re
        try:
            resp = _req.get(
                f"https://predict.fun/market/{slug}",
                headers={'User-Agent': 'Mozilla/5.0'},
                proxies=self.proxy, timeout=5
            )
            if resp.ok:
                # 网页的 og:image 链接通常包含 ?marketId=XXXX
                m = re.search(r'marketId=(\d+)', resp.text)
                if m:
                    mid = m.group(1)
                    logger.debug(f"网页解析成功: slug '{slug}' -> market ID {mid}")
                    return mid
                else:
                    logger.debug(f"og:image 中未找到 marketId (slug={slug})，尝试从网页数据块提取...")
                    
                    if outcome and outcome.upper() != "YES":
                        # 方案1.5: 针对多选项市场 (如2B)，在页面源码中寻找包含该选项的提问并回溯最近的 ID
                        escaped = resp.text.replace('\\"', '"').replace('\\\\', '\\')
                        for m_q in re.finditer(r'\"question\":\"([^\"]+)\"', escaped):
                            q = m_q.group(1)
                            if outcome.lower() in q.lower().replace(' ', ''):
                                text_before = escaped[:m_q.start()]
                                all_ids = re.findall(r'\"id\":\"(\d+)\"', text_before)
                                if all_ids:
                                    mid = all_ids[-1]
                                    logger.debug(f"多选项专项解析成功: 选项 '{outcome}' -> market ID {mid}")
                                    return mid
                    
                    # 方案2: 对于多选市场 (如 polymarket-fdv...)，从 Next.js 脱水数据中提取 category ID
                    # 匹配 React state 中的 "category":{"id":"9327" 或类似结构
                    m_cat = re.search(r'\"category\"\[^\}]+?\"id\":\"(\d+)\"', resp.text) or \
                            re.search(r'\\\"category\\\":\{\\\"id\\\":\\\"(\d+)\\\"', resp.text) or \
                            re.search(r'\"category\":\{\"id\":\"(\d+)\"', resp.text)
                    if m_cat:
                        mid = m_cat.group(1)
                        logger.debug(f"网页数据解析成功: slug '{slug}' -> market ID {mid}")
                        return mid
                    
                    # 方案3: 模糊搜索 id 邻近 categorySlug
                    m_near = re.search(r'\"id\":\"(\d+)\"[^\}]*?\"categorySlug\":\"'+slug+r'\"', resp.text.replace('\\\"', '\"'))
                    if m_near:
                        mid = m_near.group(1)
                        logger.debug(f"网页结构解析成功: slug '{slug}' -> market ID {mid}")
                        return mid
                        
                    logger.warning(f"所有网页解析方案均未找到对应 slug 的市场 ID (slug={slug})")
            else:
                logger.warning(f"访问网页失败 (slug={slug}): HTTP {resp.status_code}")
        except Exception as e:
            logger.error(f"解析市场 ID 异常: {e}")
            
        return None

    def _resolve_market(self, raw: str) -> Optional[Dict]:
        """解析市场输入 -> 缓存信息 {market_id, title, token_id, outcome, fee_rate, ...}"""
        market_key, option_name, side = self._get_market_option_and_side(raw)

        cache_key = f"{market_key}:{option_name}:{side}"
        # 如果缓存中已有且是同一个 URL/ID，避免重复解析日志
        if cache_key in self.market_cache:
            return self.market_cache[cache_key]

        # 1. 尝试从网页直接提取
        market_id = None
        info = None
        
        # 降级不必要的解析日志
        logger.debug(f"正在从网页解析 {market_key} (选项: {option_name}, 方向: {side}) ...")
        # 如果是纯数字，直接用作 market_id
        # 否则当作 slug，搜索转换
        if not market_key.isdigit():
            resolved_id = self._resolve_slug_to_id(market_key, option_name)
            if resolved_id:
                market_key = resolved_id
                cache_key = f"{market_key}:{option_name}:{side}"
            else:
                logger.error(f"无法通过 slug 找到市场: {market_key}")
                logger.debug(f"请改用数字 ID，方法: 浏览器打开市场页面 → F12 开发者工具 → Network → 搜索 marketId")
                return None

        info = self.client.fetch_market_info(market_key)

        if not info:
            logger.error(f"无法获取市场信息: {market_key}")
            return None

        title = info.get('question') or info.get('title', '未知')
        market_id = info.get('id', market_key)

        # 从 outcomes 查找目标选项
        outcomes = info.get('outcomes', [])
        token_id = None
        outcome_name = None

        for o in outcomes:
            name = o.get('name', '').strip()
            if (name.upper() == option_name.upper() or
                (option_name.upper() == 'YES' and o.get('indexSet') == 1) or
                (option_name.upper() == 'NO' and o.get('indexSet') == 2)):
                token_id = o.get('onChainId') or o.get('tokenId')
                outcome_name = name
                break

        # 如果 option 只是用于选择某个子市场（例如 500m / 1b），
        # 那么真正的二元下单方向由 side 决定。
        if not token_id and side in {"YES", "NO"}:
            target_index = 1 if side == "YES" else 2
            for o in outcomes:
                if o.get('indexSet') == target_index:
                    token_id = o.get('onChainId') or o.get('tokenId')
                    outcome_name = o.get('name', side)
                    logger.debug(f"按交易方向 '{side}' 选择二元市场 token: {outcome_name}")
                    break

        if not token_id and outcomes:
            first = outcomes[0]
            token_id = first.get('onChainId') or first.get('tokenId')
            outcome_name = first.get('name', option_name)
            logger.debug(f"未精确匹配 '{option_name}'，使用第一个选项: {outcome_name}")

        if not token_id:
            logger.error(f"市场 {market_id} 无可用选项")
            return None

        result = {
            'market_id': str(market_id),
            'title': title,
            'token_id': token_id,
            'outcome': outcome_name,
            'side': side,
            'fee_rate': int(info.get('feeRateBps') or info.get('fee_rate_bps') or 100),
            'is_neg_risk': info.get('isNegRisk') or info.get('is_neg_risk') or False,
            'is_yield_bearing': info.get('isYieldBearing') or info.get('is_yield_bearing') or False,
            'cache_key': cache_key,
        }
        self.market_cache[cache_key] = result
        logger.debug(f"解析成功: {title[:30]} -> {market_id}")
        return result

    # ── 核心价格逻辑 ──────────────────────────────────────────

    def _floor_price(self, price: float) -> float:
        return max(0.001, math.floor(price * 1000) / 1000.0)

    def _get_best_ask_for_side(self, ob: OrderBook, side: str) -> Optional[float]:
        """
        Predict API 的 orderbook 是基于 YES 价格返回的。
        NO 侧最优卖价 = 1 - YES 最优买价。
        """
        if not ob:
            return None

        side = side.upper()
        if side == "YES":
            if not ob.asks:
                return None
            return round(ob.asks[0].price, 3)

        if not ob.bids:
            return None
        return round(1.0 - ob.bids[0].price, 3)

    def calculate_target_price(self, ob: OrderBook, side: str) -> Optional[Tuple[float, float, str]]:
        """
        首次下单 / 重新下单价格:
          target_price = BestAsk - target_offset
        """
        best_ask = self._get_best_ask_for_side(ob, side)
        if best_ask is None:
            return None

        target_price = self._floor_price(best_ask - self.target_offset)
        if target_price >= best_ask:
            return None

        return (
            round(target_price, 3),
            round(best_ask, 3),
            f"BestAsk {best_ask:.3f} - {self.target_offset:.3f}"
        )

    def is_order_qualified(self, order_price: float, best_ask: float) -> bool:
        """
        合格条件:
          price >= BestAsk - lower_bound_offset
          price <= BestAsk - upper_bound_offset
        """
        return (best_ask - self.lower_bound_offset) <= order_price <= (best_ask - self.upper_bound_offset)

    def get_qualified_floor_price(self, best_ask: float) -> float:
        """返回存活区间下限，即 BestAsk - lower_bound_offset。"""
        return self._floor_price(best_ask - self.lower_bound_offset)

    def get_qualified_ceiling_price(self, best_ask: float) -> float:
        """返回存活区间上限，即 BestAsk - upper_bound_offset。"""
        return self._floor_price(best_ask - self.upper_bound_offset)

    # ── 下单 ──────────────────────────────────────────────────

    def place_order(self, market_info: Dict) -> bool:
        """在指定市场下单"""
        try:
            market_id = market_info['market_id']
            title = market_info['title']
            token_id = market_info['token_id']
            cache_key = market_info['cache_key']

            ob = self.client.fetch_orderbook(market_id)
            if not ob:
                return False

            calc = self.calculate_target_price(ob, market_info['side'])
            if not calc:
                return False

            price, best_ask, reason = calc
            amount = self.order_shares * price

            logger.info(
                f"下单: {market_id} BUY {market_info['outcome']}({market_info['side']}) "
                f"${amount:.2f} @ {price:.3f} | {reason}"
            )

            order_id = self.client.place_limit_order(
                token_id, Side.BUY, amount, price,
                fee_rate_bps=market_info['fee_rate'],
                is_neg_risk=market_info['is_neg_risk'],
                is_yield_bearing=market_info['is_yield_bearing']
            )

            if order_id:
                self.orders[cache_key] = PredictOrder(
                    order_id=order_id,
                    token_id=token_id,
                    title=title,
                    price=price,
                    amount=amount,
                    create_time=time.time(),
                    last_check_time=time.time()
                )
                logger.success(
                    f"[挂单成功] {title[:30]} {market_info['outcome']}({market_info['side']}) "
                    f"@ {price:.4f} | BestAsk={best_ask:.3f} | 单号: {order_id}"
                )
                return True
            return False
        except Exception as e:
            logger.error(f"下单异常: {e}")
            return False

    # ── 核心循环 ──────────────────────────────────────────────

    def _maintain_orders(self):
        """步骤 A: 维护现有订单 — 检查是否仍合格，价格是否需要调整"""
        for cache_key, order in list(self.orders.items()):
            try:
                minfo = self.market_cache.get(cache_key)
                if not minfo:
                    continue

                ob = self.client.fetch_orderbook(minfo['market_id'])
                if not ob:
                    continue

                best_ask = self._get_best_ask_for_side(ob, minfo['side'])
                if best_ask is None:
                    logger.info(f"执行调整(无法获取BestAsk): {order.price:.4f}(原挂单)")
                    if self.client.cancel_order(order.order_id):
                        del self.orders[cache_key]
                        logger.success(f"撤单成功: {order.order_id}")
                    continue

                if self.is_order_qualified(order.price, best_ask):
                    continue

                calc = self.calculate_target_price(ob, minfo['side'])
                if not calc:
                    continue

                new_price, _, reason = calc
                logger.info(
                    f"执行调整: {order.price:.4f}(原挂单) 已脱离存活区间 "
                    f"[{self.get_qualified_floor_price(best_ask):.4f}, {self.get_qualified_ceiling_price(best_ask):.4f}] "
                    f"| BestAsk={best_ask:.3f} | 新价格={new_price:.4f} ({reason})"
                )

                if self.client.cancel_order(order.order_id):
                    logger.success(f"订单取消成功: {order.order_id}")
                    del self.orders[cache_key]
                    self.place_order(minfo)

            except Exception as e:
                logger.error(f"维护订单异常 ({order.title[:20]}): {e}")

    def _scan_new_orders(self):
        """步骤 B: 遍历所有配置的市场，未挂单的自动补位"""
        for raw in self.markets_input:
            # 先用 _parse_market_input 获取 cache_key，检查是否已有挂单
            market_key, option_name, side = self._get_market_option_and_side(raw)
            cache_key = f"{market_key}:{option_name}:{side}"
            # 如果缓存中已有解析结果，用缓存的 cache_key
            if cache_key in self.market_cache:
                real_key = self.market_cache[cache_key].get('cache_key', cache_key)
                if real_key in self.orders:
                    continue
            # 也直接检查所有已缓存的 key 是否在 orders 里
            already_ordered = False
            for ck in self.market_cache:
                if self.market_cache[ck].get('cache_key', ck) in self.orders:
                    # 检查是否是同一个 raw 输入
                    if market_key in ck:
                        already_ordered = True
                        break
            if already_ordered:
                continue

            minfo = self._resolve_market(raw)
            if not minfo:
                continue

            # 跳过已有挂单的市场
            if minfo['cache_key'] in self.orders:
                continue

            self.place_order(minfo)

    def send_status_report(self):
        """每 2 小时发送状态报告"""
        try:
            balances = self.client.get_balances()
            available = frozen = total = 0.0
            if balances:
                d = balances.get('data', balances)
                available = float(d.get('availableBalance', 0)) / 1e6 if d.get('availableBalance') else 0
                frozen = float(d.get('frozenBalance', 0)) / 1e6 if d.get('frozenBalance') else 0
                total = available + frozen

            order_total = sum(o.amount for o in self.orders.values())

            msg = f"📊 <b>predict.fun脚本监控</b>\n"
            msg += f"━━━━━━━━━━━━━━━\n"
            msg += f"💰 可用余额: ${available:.2f}\n"
            msg += f"🔒 冻结余额: ${frozen:.2f}\n"
            msg += f"💵 总余额: ${total:.2f}\n"
            msg += f"📦 挂单数量: {len(self.orders)}\n"
            msg += f"💼 挂单总额: ${order_total:.2f}\n"
            msg += f"━━━━━━━━━━━━━━━\n"

            for ck, order in self.orders.items():
                hours = (time.time() - order.create_time) / 3600
                minfo = self.market_cache.get(ck)
                mid = minfo['market_id'] if minfo else '?'

                best_ask = None
                if minfo:
                    ob = self.client.fetch_orderbook(mid)
                    if ob:
                        best_ask = self._get_best_ask_for_side(ob, minfo['side'])

                msg += f"\n📌 {order.title[:30]}\n"
                side_label = minfo['side'] if minfo else self.order_side
                outcome_label = minfo['outcome'] if minfo else '?'
                best_ask_text = f"{best_ask:.3f}" if best_ask is not None else "?"
                qualified_floor_text = f"{self.get_qualified_floor_price(best_ask):.3f}" if best_ask is not None else "?"
                qualified_ceiling_text = f"{self.get_qualified_ceiling_price(best_ask):.3f}" if best_ask is not None else "?"
                status = "合格" if best_ask is not None and self.is_order_qualified(order.price, best_ask) else "待调整"
                msg += (
                    f"   选项: {outcome_label}({side_label}) | 价格: {order.price:.3f} | "
                    f"BestAsk: {best_ask_text} | 存活区间: [{qualified_floor_text}, {qualified_ceiling_text}] | {status}\n"
                )
                msg += f"   金额: ${order.amount:.0f} | 已挂: {hours:.1f}小时\n"

            if not self.orders:
                msg += f"\n⚠️ 当前无挂单\n"

            msg += f"\n━━━━━━━━━━━━━━━\n"
            msg += f"⏰ 报告时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            msg += f"━━━━━━━━━━━━━━━"
            self._send_tg(msg)
            logger.info(f"状态报告已发送 ({len(self.orders)}/{len(self.markets_input)})")
        except Exception as e:
            logger.error(f"报告发送失败: {e}")

    def run(self):
        """主循环"""
        self.running = True
        logger.info("━━━ Solo Market 启动 ━━━")
        logger.info(f"市场: {self.markets_input}")
        logger.info(
            f"市场数量: {len(self.markets_input)} | 选项: {self.option_name or '按市场单独解析'} "
            f"| 方向: {self.order_side} | 固定份额: {self.order_shares} | 检查间隔: {self.check_interval}s "
            f"| 目标偏移: {self.target_offset * 100:.1f}c | 存活区间: [{self.lower_bound_offset * 100:.1f}c, {self.upper_bound_offset * 100:.1f}c]"
        )

        self._scan_new_orders()
        self.send_status_report()
        self.last_report_time = time.time()
        
        # 心跳计数器，控制日志输出频率
        loop_counter = 0

        try:
            while self.running:
                loop_counter += 1
                
                # 周期性信息 (INFO)
                if self.orders:
                    active_info = []
                    for cache_key, o in self.orders.items():
                        # 获取实时位置
                        rank = "?"
                        best_ask_text = "?"
                        qualified_floor_text = "?"
                        qualified_ceiling_text = "?"
                        minfo = self.market_cache.get(cache_key)
                        if minfo:
                            ob = self.client.fetch_orderbook(minfo['market_id'])
                            if ob:
                                best_ask = self._get_best_ask_for_side(ob, minfo['side'])
                                if best_ask is not None:
                                    best_ask_text = f"{best_ask:.3f}"
                                    qualified_floor_text = f"{self.get_qualified_floor_price(best_ask):.3f}"
                                    qualified_ceiling_text = f"{self.get_qualified_ceiling_price(best_ask):.3f}"
                                    rank = "合格" if self.is_order_qualified(o.price, best_ask) else "待调"
                        active_info.append(
                            f"{o.title[:12]}@{o.price:.3f}({rank}, BestAsk={best_ask_text}, 存活区间=[{qualified_floor_text},{qualified_ceiling_text}])"
                        )
                    
                    logger.info(f"--- 滴答 [周期 {loop_counter}] 正在监控 {len(self.orders)} 个订单: {active_info} ---")
                else:
                    logger.info(f"--- 滴答 [周期 {loop_counter}] 寻找挂单机会 ---")

                self._maintain_orders()
                self._scan_new_orders()

                if time.time() - self.last_report_time >= self.report_interval:
                    self.send_status_report()
                    self.last_report_time = time.time()

                time.sleep(self.check_interval)
        except KeyboardInterrupt:
            logger.info("收到停止指令...")
        finally:
            self.running = False
            if self.orders:
                ids = [o.order_id for o in self.orders.values()]
                logger.info(f"撤销 {len(ids)} 个订单...")
                if not self.client.cancel_orders(ids):
                    for oid in ids:
                        self.client.cancel_order(oid)
                logger.success("订单已撤销")
            logger.info("监控结束")


# ── 日志 & 入口 ──────────────────────────────────────────────

class BoundedFileSink:
    """将日志写入固定文件，并将文件大小限制在指定字节数内。"""

    def __init__(self, path: str, max_bytes: int = 50 * 1024):
        self.path = path
        self.max_bytes = max_bytes
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def write(self, message):
        text = str(message)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(text)
        self._trim()

    def _trim(self):
        try:
            size = os.path.getsize(self.path)
            if size <= self.max_bytes:
                return

            with open(self.path, "rb") as f:
                f.seek(-self.max_bytes, os.SEEK_END)
                data = f.read()

            newline_pos = data.find(b"\n")
            if newline_pos != -1 and newline_pos + 1 < len(data):
                data = data[newline_pos + 1:]

            with open(self.path, "wb") as f:
                f.write(data)
        except OSError:
            pass

def setup_logging(log_dir="log", account_id="default"):
    os.makedirs(log_dir, exist_ok=True)
    logger.remove()

    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | <level>{message}</level>",
        level="INFO", colorize=True
    )
    
    # 文件日志过滤器：屏蔽每3秒一次的周期性心跳，仅保留实际动作
    def file_filter(record):
        msg = record["message"]
        # 屏蔽名单：心跳信息（包含“滴答”和“周期”）不写入文件
        if "滴答" in msg and "周期" in msg: return False
        if "正在检查盘口" in msg: return False
        if "盘口状况" in msg: return False
        if "最新挂单计算结果" in msg: return False
        return True

    log_file = os.path.join(log_dir, f"predict_{account_id}.log")
    logger.add(
        BoundedFileSink(log_file),
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
        level="INFO",
        filter=file_filter
    )
    events_file = os.path.join(log_dir, f"events_{account_id}.log")
    logger.add(
        BoundedFileSink(events_file),
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
        level="SUCCESS",
        filter=lambda r: r["level"].name in ["SUCCESS", "ERROR", "CRITICAL"]
    )


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Predict.fun Solo Market 自动挂单")
    parser.add_argument("--config-file", default="config/account_1.config.yaml")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--sim", action="store_true", help="模拟模式")
    parser.add_argument("--log-dir", default="log")
    args = parser.parse_args()

    # 从 config 文件名推导 account_id: account_1.config.yaml -> account_1
    cfg_base = os.path.basename(args.config_file)
    account_id = cfg_base.split('.')[0]
    
    setup_logging(args.log_dir, account_id)

    # 加载 env: 优先用 --env-file, 否则从 config 文件名推导
    env_candidates = []
    if args.env_file:
        env_candidates.append(args.env_file)
    else:
        # 从 config 文件名推导: account_1.config.yaml -> account_1.env
        cfg_base = os.path.basename(args.config_file)
        cfg_dir = os.path.dirname(args.config_file) or 'config'
        env_name = cfg_base.replace('.config.yaml', '.env').replace('.yaml', '.env')
        env_candidates.extend([
            os.path.join(cfg_dir, env_name),
            os.path.join(cfg_dir, '.env'),
            '.env',
        ])
    env_loaded = False
    for ef in env_candidates:
        if ef and os.path.exists(ef):
            load_dotenv(ef, override=True)
            logger.info(f"环境变量: {ef}")
            env_loaded = True
            break
    if not env_loaded:
        logger.warning(f"未找到环境文件 (尝试: {env_candidates})，使用系统环境变量")

    if not os.path.exists(args.config_file):
        logger.error(f"配置文件不存在: {args.config_file}")
        sys.exit(1)

    with open(args.config_file, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    if args.sim:
        logger.info(">>> 模拟模式 <<<")

        class MockClient:
            def __init__(self, *a, **k):
                self.step = 0
            def fetch_market_info(self, mid):
                return {
                    'id': mid, 'question': f'SimMarket-{mid}',
                    'outcomes': [
                        {'name': 'PARI', 'indexSet': 1, 'onChainId': f'0xtoken_{mid}_1'},
                        {'name': 'NIP', 'indexSet': 2, 'onChainId': f'0xtoken_{mid}_2'},
                    ]
                }
            def fetch_orderbook(self, mid):
                self.step += 1
                bids = [
                    OrderBookLevel(price=0.50, size=200, total=100),
                    OrderBookLevel(price=0.48, size=2000, total=1000),
                    OrderBookLevel(price=0.45, size=5000, total=2250),
                ]
                return OrderBook(
                    bids=bids,
                    asks=[OrderBookLevel(price=0.52, size=100, total=52)],
                    best_bid=0.50, best_ask=0.52
                )
            def place_limit_order(self, *a, **k):
                import random
                return f"sim-{random.randint(1000,9999)}"
            def cancel_order(self, *a, **k): return True
            def cancel_orders(self, *a, **k): return True
            def get_balances(self):
                return {'availableBalance': '14580000', 'frozenBalance': '120000000'}

        def mock_init(self_m, config):
            solo = config.get('solo_market', {})
            self_m.config = config
            self_m.markets_input = PredictSoloMonitor._normalize_markets_input(solo.get('markets', []))
            self_m.option_name = (solo.get('option') or '').strip()
            self_m.order_side = str(solo.get('YON', PredictSoloMonitor.DEFAULT_SIDE)).strip().upper()
            self_m.order_shares = solo.get('order_shares', 101)
            self_m.check_interval = int(solo.get('check_interval_seconds', 30))
            self_m.target_offset = float(solo.get('target_offset_cents', 2.0)) / 100.0
            self_m.lower_bound_offset = float(solo.get('lower_bound_offset_cents', 3.5)) / 100.0
            self_m.upper_bound_offset = float(solo.get('upper_bound_offset_cents', 1.3)) / 100.0
            self_m.client = MockClient()
            self_m.orders = {}
            self_m.market_cache = {}
            self_m.running = False
            self_m.last_report_time = 0
            self_m.report_interval = 2 * 3600
            self_m.wallet_address = "0xSim"
            self_m.wallet_alias = "Sim"
            self_m.proxy = None

        PredictSoloMonitor.__init__ = mock_init

    logger.info(f"配置: {args.config_file}")
    try:
        monitor = PredictSoloMonitor(config)
        monitor.run()
    except Exception as e:
        logger.critical(f"异常退出: {e}")
        logger.critical(traceback.format_exc())
        sys.exit(1)


if __name__ == '__main__':
    main()
