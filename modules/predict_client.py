import os
import time
import requests
from dataclasses import asdict
from typing import Dict, List, Optional, Any
from loguru import logger
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3
from predict_sdk import OrderBuilder, ChainId, Side, BuildOrderInput, LimitHelperInput
from .models import OrderBook, OrderBookLevel

class PredictClient:
    """Predict.fun API 客户端包装"""
    
    BASE_URL = "https://api.predict.fun/v1"
    
    def __init__(self, private_key: str, api_key: str, wallet_address: str, 
                 predict_account: Optional[str] = None, proxy: Optional[Dict] = None):
        """
        初始化 Predict 客户端
        
        Args:
            private_key: EOA 钱包私钥 (用于签名)
            api_key: Predict API Key
            wallet_address: EOA 钱包地址
            predict_account: Predict Account 地址 (智能合约钱包，用于存放资金)
            proxy: 代理设置
        """
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        self.private_key = private_key
        self.api_key = api_key
        
        # 使用 eth_account 获取规范化的地址
        self.account = Account.from_key(private_key)
        self.wallet_address = self.account.address
        self.predict_account = predict_account  # 保存 Predict Account 地址
        self.proxy = proxy
        self.token = None
        self.token_expiry = 0
        
        # 初始化 SDK OrderBuilder
        if predict_account:
            logger.info(f"使用 Predict Account: {predict_account}")
            logger.info(f"EOA 签名钱包: {self.wallet_address}")
            logger.warning("正在绕过 SDK 的 Predict Account 所有权验证...")
            
            # 手动初始化 OrderBuilder 以绕过验证
            from predict_sdk import ADDRESSES_BY_CHAIN_ID, generate_order_salt
            from predict_sdk.logger import Logger as SDKLogger
            from predict_sdk._internal.contracts import make_contracts
            from web3.middleware import ExtraDataToPOAMiddleware
            
            addresses = ADDRESSES_BY_CHAIN_ID[ChainId.BNB_MAINNET]
            rpc_url = "https://bsc-dataseed.binance.org/"
            web3_instance = Web3(Web3.HTTPProvider(rpc_url))
            web3_instance.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            
            contracts = make_contracts(web3_instance, addresses, self.account)
            sdk_logger = SDKLogger("INFO")
            
            # 直接调用 __init__ 绕过 make() 的验证
            self.builder = OrderBuilder(
                chain_id=ChainId.BNB_MAINNET,
                precision=18,
                addresses=addresses,
                generate_salt_fn=generate_order_salt,
                logger=sdk_logger,
                signer=self.account,
                predict_account=predict_account,
                contracts=contracts,
                web3=web3_instance
            )
            logger.success("Predict Account 初始化成功（已绕过验证）")
        else:
            logger.info(f"使用 EOA 钱包: {self.wallet_address}")
            self.builder = OrderBuilder.make(ChainId.BNB_MAINNET, signer=self.account)
        
        self.headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

    def perform_approvals(self) -> bool:
        """执行所有交易所需的合约授权 (Approvals)"""
        # 如果使用 Predict Account，跳过授权步骤
        # Predict Account 的授权应该已经在网页端完成
        if self.predict_account:
            logger.info("使用 Predict Account，跳过链上授权步骤")
            return True
            
        try:
            logger.info("正在检查并执行 Predict.fun 合约授权 (Approvals)...")
            # 授权 yield-bearing 以后可以兼容更多市场
            res = self.builder.set_approvals(is_yield_bearing=True)
            if res.success:
                logger.success("Predict.fun 合约授权成功")
                return True
            else:
                logger.error("Predict.fun 部分授权失败，请检查账户余额 (BNB) 或合约状态")
                for i, r in enumerate(res.transactions):
                    if not r.success:
                        logger.debug(f"TX {i} 失败原因: {getattr(r, 'cause', '未知')}")
                return False
        except Exception as e:
            logger.error(f"合约授权发生异常: {e}")
            return False

    def _get_auth_headers(self) -> Dict[str, str]:
        """获取包含 JWT Token 的请求头"""
        token = self.get_jwt_token()
        headers = self.headers.copy()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def get_jwt_token(self) -> Optional[str]:
        """获取或刷新 JWT Token (带重试机制)"""
        if self.token and time.time() < self.token_expiry:
            return self.token
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    logger.info(f"重试获取 JWT Token (第 {attempt + 1}/{max_retries} 次)...")
                else:
                    logger.info("正在获取 Predict.fun 登录消息...")
                
                # 1. 获取签名消息
                resp = requests.get(
                    f"{self.BASE_URL}/auth/message", 
                    headers=self.headers,
                    proxies=self.proxy,
                    timeout=30
                )
                if not resp.ok:
                    logger.error(f"获取登录消息失败: {resp.status_code} {resp.text}")
                    if attempt < max_retries - 1:
                        time.sleep(2)
                        continue
                    return None
                    
                data = resp.json()
                inner_data = data.get("data", {}) if isinstance(data, dict) and "data" in data else data
                message = inner_data.get("message")
                
                if not message:
                    logger.error(f"未能获取登录消息, 完整响应: {data}")
                    if attempt < max_retries - 1:
                        time.sleep(2)
                        continue
                    return None
                    
                # 2. 签署消息 (根据是否使用 Predict Account 选择不同的签名方法)
                logger.info("正在签署登录消息...")
                if self.predict_account:
                    # 使用 Predict Account 专用的签名方法
                    logger.debug("使用 Predict Account 签名方法")
                    signature = self.builder.sign_predict_account_message(message)
                    signer_address = self.predict_account  # JWT 的 signer 必须是 Predict Account
                    logger.debug(f"Predict Account 签名: {signature[:66]}...")
                else:
                    # 使用普通的 EOA 签名方法
                    encoded_msg = encode_defunct(text=message)
                    signed_msg = self.account.sign_message(encoded_msg)
                    signature = signed_msg.signature.hex()
                    if not signature.startswith("0x"):
                        signature = "0x" + signature
                    signer_address = self.wallet_address
                
                # 3. 获取 JWT
                logger.info(f"正在获取 JWT Token (signer: {signer_address})...")
                auth_payload = {
                    "signer": signer_address,
                    "signature": signature,
                    "message": message
                }
                logger.debug(f"认证请求体: {auth_payload}")
                
                resp = requests.post(
                    f"{self.BASE_URL}/auth",
                    json=auth_payload,
                    headers=self.headers,
                    proxies=self.proxy,
                    timeout=30
                )
                
                # 添加详细的错误日志
                if not resp.ok:
                    logger.error(f"认证失败 {resp.status_code}: {resp.text}")
                    if attempt < max_retries - 1:
                        time.sleep(2)
                        continue
                    return None
                    
                resp.raise_for_status()
                login_data = resp.json()
                
                # 兼容处理 login_data["data"]["token"] 或 login_data["token"]
                token_data = login_data.get("data", {}) if isinstance(login_data, dict) and "data" in login_data else login_data
                self.token = token_data.get("token") or token_data.get("jwt")
                
                if not self.token:
                    logger.error(f"登录成功但未获取到 Token: {login_data}")
                    if attempt < max_retries - 1:
                        time.sleep(2)
                        continue
                    return None

                # 假设 Token 有效期较长，保守设置为 20 小时刷新一次
                self.token_expiry = time.time() + 20 * 3600 
                
                logger.success("Predict.fun 登录成功")
                return self.token
                
            except requests.exceptions.Timeout as e:
                logger.warning(f"网络请求超时 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(3)
                    continue
                logger.error("多次重试后仍然超时，登录失败")
                return None
            except Exception as e:
                logger.error(f"登录失败: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return None
        
        return None

    def fetch_market_info(self, id_or_token: str) -> Optional[Dict]:
        """获取市场详情"""
        try:
            # 兼容处理：尝试直接获取
            url = f"{self.BASE_URL}/markets/{id_or_token}"
            resp = requests.get(
                url,
                headers=self.headers,
                proxies=self.proxy,
                timeout=10
            )
            if resp.status_code == 401 or resp.status_code == 403:
                logger.error(f"API 认证失败 (Market Info): {resp.status_code} {resp.text}")
                return None
            if resp.status_code == 500:
                logger.debug(f"ID {id_or_token} 可能不是市场 ID (可能是 Token ID)，跳过详情获取")
                return None
            if not resp.ok:
                logger.warning(f"获取市场详情失败 {id_or_token}: {resp.status_code} {resp.text}")
                return None
            
            data = resp.json()
            return data.get("data") if isinstance(data, dict) and "data" in data else data
        except Exception as e:
            logger.debug(f"获取市场 {id_or_token} 信息时发生异常: {e}")
            return None

    def fetch_orderbook(self, token_id: str) -> Optional[OrderBook]:
        """获取订单簿"""
        try:
            # Predict API 使用的是 market_id/token_id 获取订单簿
            resp = requests.get(
                f"{self.BASE_URL}/markets/{token_id}/orderbook",
                headers=self.headers,
                proxies=self.proxy,
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            # 兼容处理 {"success": true, "data": {"bids": ...}} 和 {"bids": ...}
            inner_data = data.get("data", {}) if isinstance(data, dict) and "data" in data else data
            
            bids_raw = inner_data.get("bids", []) or []
            asks_raw = inner_data.get("asks", []) or []
            
            def parse_level(l):
                p = float(l[0])
                # 如果价格大于 1，说明是以 wei 为单位 (1e18 = 100%)
                if p > 1.2: p = p / 1e18
                
                s = float(l[1])
                # 如果数量极大，说明可能是以 wei 为单位的数量（shares）
                if s > 1e10: s = s / 1e18
                
                # 在 Predict 市场，保护金额通常是指美元价值
                # 美元价值 = 价格 * 份额 (price * shares)
                usd_value = p * s
                return OrderBookLevel(price=p, size=usd_value, total=usd_value)

            bids = [parse_level(b) for b in bids_raw]
            asks = [parse_level(a) for a in asks_raw]
            
            # 排序：买单价从高到低，卖单价从低到高
            bids.sort(key=lambda x: x.price, reverse=True)
            asks.sort(key=lambda x: x.price)
            
            best_bid = bids[0].price if bids else 0.0
            best_ask = asks[0].price if asks else 1.0
            
            return OrderBook(bids=bids, asks=asks, best_bid=best_bid, best_ask=best_ask)
            
        except requests.exceptions.HTTPError as e:
            logger.warning(f"获取订单簿 HTTP 错误 (MarketID: {token_id}): {e}")
            return None
        except Exception as e:
            logger.error(f"获取订单簿异常 (MarketID: {token_id}): {e}")
            return None

    def place_limit_order(self, token_id: str, side: Side, amount: float, price: float, 
                          fee_rate_bps: int = 100, is_neg_risk: bool = False, 
                          is_yield_bearing: bool = False) -> Optional[str]:
        """下限价单"""
        try:
            # 1. 严格确保 price 最多为三位小数 (如 0.551)，Predict API 限定最高 3 位精度
            price = round(price, 3)
            quantity = amount / price
            
            # 由于 t_amt 涉及 10**18 (超过 Python float 53 位精度)，
            # 乘以 float price 会导致尾数精度丢失 (如出现 ...0002048)。
            # 必须全程转换为大整数计算。
            price_int_1000 = int(round(price * 1000))
            
            t_amt = int(round(quantity, 4) * 10**18)
            
            # 买单 (BUY): Maker 给的是 USDC(按价格比例), Taker 得到的是 Shares 
            if side == Side.BUY:
                m_amt = (t_amt * price_int_1000) // 1000
            else:
                m_amt = (t_amt * price_int_1000) // 1000

            # 但根据 Predict API (通常 BUY 时 makerAmount 是付出的投资金额, takerAmount 是期望得到的份额)
            # 所以上面的 m_amt 如果是买入: 54.54 USDC = 101 shares * 0.54
            
            logger.debug(f"下单计算: m={m_amt}, t={t_amt}, price={price}")

            # p_wei 虽然对于构建可能不再严格控制 m_amt，但供打印
            # 【关键修复】: 绝对不能用 int(price * 10**18)，那会产生像 540000000000000064 这样的尾数！
            actual_price_wei = (price_int_1000 * 10**18) // 1000

            build_input = BuildOrderInput(
                maker=self.account.address, # 使用从私钥导出的地址
                token_id=token_id,
                side=side,
                maker_amount=m_amt,
                taker_amount=t_amt,
                fee_rate_bps=fee_rate_bps
            )
            
            order = self.builder.build_order("LIMIT", build_input)
            
            # 3. 生成 EIP-712 类型数据并签署
            typed_data = self.builder.build_typed_data(
                order, 
                is_neg_risk=is_neg_risk, 
                is_yield_bearing=is_yield_bearing
            )
            signed_order = self.builder.sign_typed_data_order(typed_data)
            
            # 4. 手动构建 API 要求的格式 (CamelCase)
            order_payload = {
                "salt": signed_order.salt,
                "maker": signed_order.maker,
                "signer": signed_order.signer,
                "taker": signed_order.taker,
                "tokenId": signed_order.token_id,
                "makerAmount": str(signed_order.maker_amount),
                "takerAmount": str(signed_order.taker_amount),
                "expiration": str(signed_order.expiration),
                "nonce": str(signed_order.nonce),
                "feeRateBps": str(signed_order.fee_rate_bps),
                "side": int(signed_order.side.value),
                "signatureType": int(signed_order.signature_type.value),
                "signature": signed_order.signature if signed_order.signature.startswith("0x") else "0x" + signed_order.signature
            }
            
            # 5. 构建最终 API 要求的包装格式
            final_payload = {
                "data": {
                    "strategy": "LIMIT",
                    "pricePerShare": str(int(actual_price_wei)),
                    "order": order_payload
                }
            }
            
            logger.debug(f"发送最终 Payload: {final_payload}")
            
            # 5. 发送到 API
            resp = requests.post(
                f"{self.BASE_URL}/orders",
                json=final_payload,
                headers=self._get_auth_headers(),
                proxies=self.proxy,
                timeout=10
            )
            if not resp.ok:
                logger.error(f"下单失败 {resp.status_code}: {resp.text}")
                return None
                
            result = resp.json()
            # 兼容 {"success": true, "data": {"orderId": ...}}
            inner_res = result.get("data", {}) if isinstance(result, dict) and "data" in result else result
            order_id = inner_res.get("orderId") or inner_res.get("hash")
            return order_id
            
        except Exception as e:
            logger.error(f"下单失败: {e}")
            if hasattr(e, 'response') and e.response:
                logger.error(f"API 响应: {e.response.text}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """撤单（从订单簿移除）"""
        try:
            logger.debug(f"正在撤单: {order_id}")
            
            # 根据官方文档，需要包装在 data 对象中
            payload = {
                "data": {
                    "ids": [str(order_id)]  # 确保是字符串数组
                }
            }
            headers = self._get_auth_headers()
            
            logger.debug(f"撤单请求体: {payload}")
            
            resp = requests.post(
                f"{self.BASE_URL}/orders/remove",
                json=payload,
                headers=headers,
                proxies=self.proxy,
                timeout=10
            )
            
            logger.debug(f"撤单响应状态: {resp.status_code}")
            logger.debug(f"撤单响应内容: {resp.text}")
            
            if not resp.ok:
                logger.error(f"撤单失败 {order_id}: {resp.status_code} - {resp.text}")
                return False
                
            resp.raise_for_status()
            logger.success(f"撤单成功: {order_id}")
            return True
            
        except Exception as e:
            logger.error(f"撤单异常 {order_id}: {e}")
            if hasattr(e, 'response') and e.response:
                logger.error(f"响应内容: {e.response.text}")
            return False

    def cancel_orders(self, order_ids: list) -> bool:
        """批量撤单（从订单簿移除）
        
        Args:
            order_ids: 订单 ID 列表（最多 100 个）
            
        Returns:
            是否成功
        """
        try:
            if not order_ids:
                logger.warning("没有订单需要撤销")
                return True
                
            # API 限制最多 100 个
            if len(order_ids) > 100:
                logger.warning(f"订单数量超过 100，将分批撤销")
                # 分批处理
                for i in range(0, len(order_ids), 100):
                    batch = order_ids[i:i+100]
                    if not self.cancel_orders(batch):
                        return False
                return True
            
            logger.debug(f"正在批量撤单: {len(order_ids)} 个订单")
            
            # 根据官方文档，需要包装在 data 对象中
            payload = {
                "data": {
                    "ids": [str(oid) for oid in order_ids]  # 确保是字符串数组
                }
            }
            headers = self._get_auth_headers()
            
            logger.debug(f"批量撤单请求体: {payload}")
            
            resp = requests.post(
                f"{self.BASE_URL}/orders/remove",
                json=payload,
                headers=headers,
                proxies=self.proxy,
                timeout=10
            )
            
            logger.debug(f"批量撤单响应状态: {resp.status_code}")
            logger.debug(f"批量撤单响应内容: {resp.text}")
            
            if not resp.ok:
                logger.error(f"批量撤单失败: {resp.status_code} - {resp.text}")
                return False
                
            resp.raise_for_status()
            logger.success(f"批量撤单成功: {len(order_ids)} 个订单")
            return True
            
        except Exception as e:
            logger.error(f"批量撤单异常: {e}")
            if hasattr(e, 'response') and e.response:
                logger.error(f"响应内容: {e.response.text}")
            return False

    def get_balances(self) -> Dict:
        """获取个人余额和持仓 (使用 SDK 读取 USDT 返回实际余额)"""
        try:
            addr = self.predict_account or self.wallet_address
            # balance_of('USDT') 返回的是 wei 级别真实余额 (如 18位精度)
            bal_wei = self.builder.balance_of('USDT', addr)
            
            # 由于底层 API 返回的是 18位小数的整数，将其按比例转换到 `solomarket` 预期的假设 `1e6` 的除法环境
            # (solomarket: float(d.get('availableBalance', 0)) / 1e6)
            # 即: 我们给它 `bal_wei / 1e12` (整数形式)，它再除以 1e6，最终得出真实数量
            bal_scaled = int(bal_wei // 10**12)
            
            return {
                'availableBalance': str(bal_scaled),
                'frozenBalance': '0' # Frozen 不再从 REST 提供
            }
        except Exception as e:
            logger.debug(f"获取余额失败: {e}")
            return {}
