

# Predict.fun Solo Market 自动挂单脚本

## 用我脚本的老师欢迎走我的邀请链接：https://predict.fun?ref=A5A1D
**Author**: @0xwenjian
现在有的脚本都太复杂了，这个bot自动在 Predict.fun 市场挂单做市，赚取平台积分。
行情不好没必要用那么精细的脚本。0撸就完事了。



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
    - "https://predict.fun/market/opensea-fdv-above-one-day-after-launch"
  option: "500m"
  YON: "NO"
  order_shares: 101
  target_offset_cents: 2.0
  lower_bound_offset_cents: 3.5
  upper_bound_offset_cents: 1.3
  check_interval_seconds: 30
```


### 核心参数

| 参数 | 说明 | 示例 |
|------|------|------|
| `order_shares` | 每次挂单的固定份额 | `101` |
| `option` | 多选市场的英文选项名 | `500m` |
| `YON` | 买 `YES` 还是买 `NO` | `NO` |
| `target_offset_cents` | 首次下单/重挂目标偏移（美分） | `2.0` |
| `lower_bound_offset_cents` | 存活区间下限偏移（美分） | `3.5` |
| `upper_bound_offset_cents` | 存活区间上限偏移（美分） | `1.3` |
| `check_interval_seconds` | 订单检查间隔（秒） | `30` |

### 多账号

```bash
python3 solomarket.py --config-file config/account_1.config.yaml
python3 solomarket.py --config-file config/account_2.config.yaml
```

---

## 挂单策略

### 核心原则

挂单价格的存活区间必须在 `[BestAsk - 0.035, BestAsk - 0.013]` 内。

### 首次下单

首次下单按 `BestAsk - 0.02`（2 美分）挂单。

### 自动调整

脚本每 **30 秒** 检查一次：
- 已挂订单是否仍然在 `[BestAsk - 0.035, BestAsk - 0.013]` 存活区间内
- 如果不合格，则撤单并按最新 `BestAsk - 0.02` 重新挂单

### Telegram 报告

每 **2 小时** 自动发送一次状态报告，包含：
- 账户余额 (可用/冻结/总计)
- 挂单数量和总额
- 每笔挂单详情 (市场、选项方向、价格、BestAsk、存活区间、已挂时长)

### 日志

- 每个账号只保留一份 `predict_<account>.log` 和一份 `events_<account>.log`
- 两个日志文件都会限制在 **50KB** 以内，超出后自动覆盖最旧内容

---

## NO 侧说明

Predict API 的 orderbook 文档说明盘口价格默认基于 `YES` 返回；如果你配置 `YON: "NO"`，脚本会按文档里的补价逻辑把 `YES` 盘口换算成 `NO` 的 `BestAsk` 后再挂单。

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
