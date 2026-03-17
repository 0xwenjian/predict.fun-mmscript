#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Predict.fun Solo Market 自动挂单脚本
用户提供市场 URL/ID，脚本自动计算最优价格并挂单
优先级: 1) 得分 (价格在 BestAsk - 0.06 以内)  2) 前方保护 (> min_protection)
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

    def __init__(self, config: Dict):
        socket.setdefaulttimeout(20)
        self.config = config
        solo = config.get('solo_market', {})

        # 市场配置 (支持 URL / slug / market_id)
        self.markets_input = solo.get('markets', [])
        self.min_protection = solo.get('min_protection_amount', 500.0)
        self.order_shares = solo.get('order_shares', 101)

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

    def _resolve_slug_to_id(self, slug: str, outcome: str = "") -> Optional[str]:
        """通过解析 Predict.fun 网页或 API，将 slug 转换为 market_id"""
        if outcome and outcome.upper() != "YES":
            logger.info(f"正在从网页解析 {slug} (选项: {outcome}) 的 market ID ...")
        else:
            logger.info(f"正在从网页解析 market ID: {slug} ...")
        import requests as _req
        import re
        try:
            # 方案1: 直接抓取网页内容 (适用于大多数单选市场)
            resp = _req.get(
                f"https://predict.fun/market/{slug}",
                headers={'User-Agent': 'Mozilla/5.0'},
                proxies=self.proxy, timeout=10
            )
            if resp.ok:
                # 网页的 og:image 链接通常包含 ?marketId=XXXX
                m = re.search(r'marketId=(\d+)', resp.text)
                if m:
                    mid = m.group(1)
                    logger.success(f"网页解析成功: slug '{slug}' -> market ID {mid}")
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
                                    logger.success(f"多选项专项解析成功: 选项 '{outcome}' -> market ID {mid}")
                                    return mid
                    
                    # 方案2: 对于多选市场 (如 polymarket-fdv...)，从 Next.js 脱水数据中提取 category ID
                    # 匹配 React state 中的 "category":{"id":"9327" 或类似结构
                    m_cat = re.search(r'\"category\"\[^\}]+?\"id\":\"(\d+)\"', resp.text) or \
                            re.search(r'\\\"category\\\":\{\\\"id\\\":\\\"(\d+)\\\"', resp.text) or \
                            re.search(r'\"category\":\{\"id\":\"(\d+)\"', resp.text)
                    if m_cat:
                        mid = m_cat.group(1)
                        logger.success(f"网页数据解析成功: slug '{slug}' -> market ID {mid}")
                        return mid
                    
                    # 方案3: 模糊搜索 id 邻近 categorySlug
                    m_near = re.search(r'\"id\":\"(\d+)\"[^\}]*?\"categorySlug\":\"'+slug+r'\"', resp.text.replace('\\\"', '\"'))
                    if m_near:
                        mid = m_near.group(1)
                        logger.success(f"网页结构解析成功: slug '{slug}' -> market ID {mid}")
                        return mid
                        
                    logger.warning(f"所有网页解析方案均未找到对应 slug 的市场 ID (slug={slug})")
            else:
                logger.warning(f"访问网页失败 (slug={slug}): HTTP {resp.status_code}")
        except Exception as e:
            logger.error(f"解析市场 ID 异常: {e}")
            
        return None

    def _resolve_market(self, raw: str) -> Optional[Dict]:
        """解析市场输入 -> 缓存信息 {market_id, title, token_id, outcome, fee_rate, ...}"""
        market_key, outcome = self._parse_market_input(raw)

        cache_key = f"{market_key}:{outcome}"
        # 如果缓存中已有且是同一个 URL/ID，避免重复解析日志
        if cache_key in self.market_cache:
            return self.market_cache[cache_key]

        # 1. 尝试从网页直接提取
        market_id = None
        info = None
        
        # 降级不必要的解析日志
        logger.debug(f"正在从网页解析 {market_key} (选项: {outcome}) ...")
        # 如果是纯数字，直接用作 market_id
        # 否则当作 slug，搜索转换
        if not market_key.isdigit():
            resolved_id = self._resolve_slug_to_id(market_key, outcome)
            if resolved_id:
                market_key = resolved_id
                cache_key = f"{market_key}:{outcome}"
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
            if (name.upper() == outcome.upper() or
                (outcome.upper() == 'YES' and o.get('indexSet') == 1) or
                (outcome.upper() == 'NO' and o.get('indexSet') == 2)):
                token_id = o.get('onChainId') or o.get('tokenId')
                outcome_name = name
                break

        if not token_id and outcomes:
            first = outcomes[0]
            token_id = first.get('onChainId') or first.get('tokenId')
            outcome_name = first.get('name', outcome)
            logger.debug(f"未精确匹配 '{outcome}'，使用第一个选项: {outcome_name}")

        if not token_id:
            logger.error(f"市场 {market_id} 无可用选项")
            return None

        result = {
            'market_id': str(market_id),
            'title': title,
            'token_id': token_id,
            'outcome': outcome_name,
            'fee_rate': int(info.get('feeRateBps') or info.get('fee_rate_bps') or 100),
            'is_neg_risk': info.get('isNegRisk') or info.get('is_neg_risk') or False,
            'is_yield_bearing': info.get('isYieldBearing') or info.get('is_yield_bearing') or False,
            'cache_key': cache_key,
        }
        self.market_cache[cache_key] = result
        logger.debug(f"解析成功: {title[:30]} -> {market_id}")
        return result

    # ── 核心价格逻辑 ──────────────────────────────────────────

    def calculate_best_price(self, ob: OrderBook) -> Optional[Tuple[float, int, float, str]]:
        """
        计算最优挂单价格
        优先级: 1) 得分 (在 BestAsk - 0.06 以内)  2) 保护 (> min_protection)

        Returns:
            (price, rank, protection, reason) 或 None
        """
        if not ob or not ob.bids:
            return None

        best_ask = ob.asks[0].price if ob.asks else 1.0
        min_score_price = best_ask - 0.06

        # 扫描 bids，累计保护金额
        cumulative_protection = 0.0
        best_safe_price = None  # 满足保护条件的最优价格
        best_safe_rank = 0
        best_safe_prot = 0.0

        for i, level in enumerate(ob.bids):
            cumulative_protection += level.total
            # 价格精确到三位小数 (0.1美分)，减去最小刻度 0.001
            target_price = math.floor((level.price - 0.001) * 1000) / 1000.0
            if target_price < 0.001:
                target_price = 0.001
            rank = i + 2

            # 检查是否在得分范围内
            if target_price < min_score_price:
                continue  # 价格太低，不得分，跳过

            # 在得分范围内，检查保护
            if cumulative_protection >= self.min_protection and best_safe_price is None:
                best_safe_price = target_price
                best_safe_rank = rank
                best_safe_prot = cumulative_protection

        # 情况 1: 找到了同时满足得分+保护的位置 (最佳)
        if best_safe_price is not None:
            return (
                round(best_safe_price, 3), best_safe_rank, best_safe_prot,
                f"✅ 得分+保护 (保护=${best_safe_prot:,.0f})"
            )

        # 情况 2: 得分范围内没有足够保护 → 仍然下单 (得分优先)
        # 选择得分范围内最深的位置 (离 min_score_price 最近但仍 >= min_score_price)
        best_score_price = None
        best_score_rank = 0
        best_score_prot = 0.0
        cumulative_protection = 0.0

        for i, level in enumerate(ob.bids):
            cumulative_protection += level.total
            target_price = math.floor((level.price - 0.001) * 1000) / 1000.0
            if target_price < 0.001:
                target_price = 0.001
            rank = i + 2

            if target_price >= min_score_price:
                # 记录这个位置（持续更新到最深的合格位置）
                best_score_price = target_price
                best_score_rank = rank
                best_score_prot = cumulative_protection

        if best_score_price is not None:
            return (
                round(best_score_price, 3), best_score_rank, best_score_prot,
                f"⚠️ 得分OK但保护不足 (保护=${best_score_prot:,.0f} < ${self.min_protection:,.0f})"
            )

        # 情况 3: 没有 bids 在得分范围内 → 直接在 MinScorePrice 挂单 (保证得分)
        if min_score_price >= 0.001:
            target_price = math.floor(min_score_price * 1000) / 1000.0
            return (
                round(target_price, 3), 0, 0.0,
                f"🔻 无前方买单，直接挂在得分线 (BestAsk {best_ask:.3f} - 0.06)"
            )

        return None

    def _log_orderbook_depth(self, title: str, ob: OrderBook, target_price: float, target_rank: int):
        """打印市场深度详情 (前 10 档)"""
        logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        logger.info(f"[{title[:50]}] 市场深度 (前10档):")
        
        cumulative = 0.0
        for i, lv in enumerate(ob.bids[:10]):
            cumulative += lv.total
            marker = " -> " if abs(lv.price - target_price) < 0.0005 else "    "
            logger.info(f"{marker}买{i+1}: {lv.price:.4f} (本档: ${lv.total:,.0f} | 累计保护: ${cumulative:,.0f})")
        
        logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        logger.info(f"[下单准备] {title[:30]} | 目标价格: {target_price:.4f} (买{target_rank}价)")

    # ── 下单 ──────────────────────────────────────────────────

    def place_order(self, market_info: Dict) -> bool:
        """在指定市场下单"""
        try:
            market_id = market_info['market_id']
            title = market_info['title']
            token_id = market_info['token_id']
            cache_key = market_info['cache_key']

            ob = self.client.fetch_orderbook(market_id)
            if not ob or not ob.bids:
                return False

            calc = self.calculate_best_price(ob)
            if not calc:
                return False

            price, rank, protection, reason = calc
            amount = self.order_shares * price

            # 打印详细深度日志
            self._log_orderbook_depth(title, ob, price, rank)
            logger.info(f"下单: {market_id} BUY {market_info['outcome']} ${amount:.2f} @ {price:.3f}")

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
                logger.success(f"[挂单成功] {title[:30]} @ {price:.4f} (买{rank}价 ${protection:,.0f}) | 单号: {order_id}")
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

                best_ask = ob.asks[0].price if ob.asks else 1.0
                calc = self.calculate_best_price(ob)

                if not calc:
                    # 完全无合格位置 → 撤单
                    logger.info(f"执行调整(无合格价格): {order.price:.4f}(原挂单) | BestAsk={best_ask:.3f}")
                    if self.client.cancel_order(order.order_id):
                        del self.orders[cache_key]
                        logger.success(f"撤单成功: {order.order_id}")
                    continue

                new_price, new_rank, new_prot, reason = calc

                # 价格无变化 → 跳过
                if abs(new_price - order.price) <= 0.0005:
                    continue

                # 买3稳定守卫: 如果当前已在买3以内，且新价格是向前(更高)，不改单
                cur_rank, _ = self._get_rank_prot(ob, order.price)
                if cur_rank <= 3 and new_price > order.price:
                    continue

                # 需要改单
                direction = "前进" if new_price > order.price else "后退"
                logger.info(f"执行调整({direction}): {order.price:.4f}(买{cur_rank}) -> {new_price:.4f}(买{new_rank})")
                
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
            market_key, outcome = self._parse_market_input(raw)
            cache_key = f"{market_key}:{outcome}"
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

            msg = f"📊 <b>Solo Market 状态报告</b>\n"
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

                # 获取实时排名和保护
                rank, prot = 0, 0.0
                if minfo:
                    ob = self.client.fetch_orderbook(mid)
                    if ob:
                        rank, prot = self._get_rank_prot(ob, order.price)

                msg += f"\n📌 {order.title[:30]}\n"
                msg += f"   价格: {order.price:.3f} | 买{rank}价 | 保护: ${prot:,.0f}\n"
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

    def _get_rank_prot(self, ob: OrderBook, price: float) -> Tuple[int, float]:
        if not ob:
            return 0, 0.0
        rank = 1
        prot = ob.get_protection_amount("BUY", price)
        for lv in ob.bids:
            if lv.price > price + 0.0005: # 精度 0.001 级别
                rank += 1
            else:
                break
        return rank, prot

    def run(self):
        """主循环 (3 秒)"""
        self.running = True
        logger.info("━━━ Solo Market 启动 ━━━")
        logger.info(f"市场: {self.markets_input}")
        logger.info(f"市场数量: {len(self.markets_input)} | 固定份额: {self.order_shares} | 最小保护: ${self.min_protection}")

        self._scan_new_orders()
        self.send_status_report()
        self.last_report_time = time.time()
        
        # 心跳计数器，控制日志输出频率
        loop_counter = 0

        try:
            while self.running:
                loop_counter += 1
                
                # 心跳日志
                if self.orders:
                    active_info = [f"{o.title[:12]}@{o.price:.3f}" for o in self.orders.values()]
                    logger.debug(f"--- 周期 {loop_counter} | 监控中: {active_info} ---")
                else:
                    logger.debug(f"--- 周期 {loop_counter} | 等待挂单 ---")

                self._maintain_orders()
                self._scan_new_orders()

                if time.time() - self.last_report_time >= self.report_interval:
                    self.send_status_report()
                    self.last_report_time = time.time()

                time.sleep(3)
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

def setup_logging(log_dir="log"):
    os.makedirs(log_dir, exist_ok=True)
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | <level>{message}</level>",
        level="DEBUG", colorize=True
    )
    
    # 文件日志过滤器：屏蔽每3秒一次的周期性心跳，仅保留实际动作
    def file_filter(record):
        msg = record["message"]
        # 屏蔽名单：心跳信息不写入文件
        if "周期" in msg and "监控中" in msg: return False
        if "正在检查盘口" in msg: return False
        if "盘口状况" in msg: return False
        if "最新挂单计算结果" in msg: return False
        return True

    log_file = os.path.join(log_dir, f"predict_{datetime.now():%Y%m%d}.log")
    logger.add(log_file, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
               level="INFO", rotation="00:00", retention="30 days", encoding="utf-8",
               filter=file_filter)
    events_file = os.path.join(log_dir, f"events_{datetime.now():%Y%m%d}.log")
    logger.add(events_file, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
               level="SUCCESS", rotation="00:00", retention="90 days", encoding="utf-8",
               filter=lambda r: r["level"].name in ["SUCCESS", "ERROR", "CRITICAL"])


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Predict.fun Solo Market 自动挂单")
    parser.add_argument("--config-file", default="config/account_1.config.yaml")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--sim", action="store_true", help="模拟模式")
    parser.add_argument("--log-dir", default="log")
    args = parser.parse_args()

    setup_logging(args.log_dir)

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
            self_m.markets_input = solo.get('markets', [])
            self_m.min_protection = solo.get('min_protection_amount', 500.0)
            self_m.order_shares = solo.get('order_shares', 101)
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
