# -*- coding: utf-8 -*-
from dataclasses import dataclass
from typing import List, Optional

@dataclass
class OrderBookLevel:
    price: float  # 0.0 ~ 1.0
    size: float   # 美元价值
    total: float  # 当前层级的累计价值 (price * size)

@dataclass
class OrderBook:
    bids: List[OrderBookLevel]
    asks: List[OrderBookLevel]
    best_bid: float
    best_ask: float
    
    def get_protection_amount(self, side: str, price: float, my_order_amount: float = 0.0) -> float:
        """
        计算在指定价格前方的保护金额。
        遵循 First-In-Queue 假设：同价位的单子不计入保护。
        """
        protection = 0.0
        if side == "BUY":
            # 计算价格高于我们的单子
            for level in self.bids:
                if level.price > price + 0.00001:
                    protection += level.size
                else:
                    break
        else:
            # 计算价格低于我们的单子
            for level in self.asks:
                if level.price < price - 0.00001:
                    protection += level.size
                else:
                    break
        return protection

@dataclass
class PredictOrder:
    order_id: str
    token_id: str
    title: str
    price: float
    amount: float
    create_time: float
    last_check_time: float
