用我脚本的老师欢迎走我的邀请链接：https://predict.fun?ref=A5A1D

# Predict.fun Solo Market 自动挂单脚本

现在有的脚本都太复杂了，这个bot自动在 Predict.fun 市场挂单做市，赚取平台积分。
行情不好没必要用那么精细的脚本。0撸就完事了。

**Author**: @0xwenjian

---

## 快速开始

```bash
# 1. 创建虚拟环境
python3 -m venv env
source env/bin/activate
pip install -r requirements.txt

# 2. 配置
cp config/config_example.yaml config/account_1.config.yaml
# 编辑 config/account_1.config.yaml 填入市场和参数
# 编辑 config/account_1.env 填入私钥和 API Key

# 3. 运行
python3 solomarket.py --config-file config/account_1.config.yaml

# 模拟模式 (不消耗资金)
python3 solomarket.py --sim --config-file config/account_1.config.yaml
```

---

## 配置说明

### 市场配置

在 `config/account_1.config.yaml` 中配置要挂单的市场：

```yaml
solo_market:
  markets:
    - "https://predict.fun/market/cs2-prv-nip-2026-03-19:PARI"  URL + 选项名称
```

> 市场 ID 可在 predict.fun 网页的 URL 或页面源码中找到。

### 核心参数

| 参数 | 说明 | 示例 |
|------|------|------|
| `order_shares` | 每次挂单的固定份额 | `101` |
| `min_protection_amount` | 最小前方保护金额 ($) | `500` |

### 多账号

```bash
python3 solomarket.py --config-file config/account_1.config.yaml
python3 solomarket.py --config-file config/account_2.config.yaml
```

---

## 挂单策略

### 优先级

1. **得分优先**：挂单价格必须在 `BestAsk - 0.06` (6 美分) 以内
2. **保护其次**：在得分范围内，优先选择前方保护金额 ≥ `min_protection_amount` 的位置

### 三种情况

| 情况 | 行为 |
|------|------|
| ✅ 得分+保护都满足 | 在满足保护的最优位置挂单 |
| ⚠️ 得分OK但保护不足 | 仍然挂单 (得分优先) |
| 🔻 得分范围内无买单 | 直接在 BestAsk - 0.06 挂单 (保证得分) |

### 自动调整

脚本每 **3 秒** 检查一次：
- 已挂订单是否仍合格 → 不合格则撤单
- **买3价格守卫**：如果当前已在买1-买3以内，且新价格是向前调整（更高），则**跳过改单**以减少手续费和频繁撤单。
- 最佳价格是否变化（且触发后退或掉出买3） → 改单
- 扫描候选池补位 (如果 `markets` 列表有新内容)

### Telegram 报告

每 **2 小时** 自动发送一次状态报告，包含：
- 账户余额 (可用/冻结/总计)
- 挂单数量和总额
- 每笔挂单详情 (市场、价格、排名、保护金额、已挂时长)

---

## 保护金额计算

保护金额 = 挂单**前方**所有买单的总金额。

采用 **First-In-Queue 假设**：
- 只计算价格**严格高于**我们的档位金额
- **同价位保护计为 $0**（假设我们排在最前）
- 永远不挂买 1 价（避免被快速成交）

```
示例：
  买1: 0.352 @ $800
  买2: 0.351 @ $400
  我们的挂单: 0.351

  前方保护 = $800 (只有买1)
  同价位保护 = $0
```

---

## 目录结构

```
predict-mmscript/
├── config/
│   ├── account_1.config.yaml   # 配置文件
│   ├── account_1.env           # 环境变量 (私钥/API Key)
│   └── config_example.yaml     # 配置模板
├── modules/
│   ├── models.py               # 数据模型
│   └── predict_client.py       # API 客户端
├── log/                        # 运行日志
├── solomarket.py               # 主程序
├── requirements.txt
└── readme.md
```

---

## 环境变量

在 `config/account_1.env` 中配置：

```env
PREDICT_PRIVATE_KEY=你的私钥
PREDICT_API_KEY=你的API_Key
PREDICT_WALLET_ADDRESS=你的钱包地址
PREDICT_ACCOUNT=Predict_Account地址(可选)
```