# -*- coding: utf-8 -*-
"""
盘前多维分析模块 (Pre-Market Analysis)

独立的盘前分析引擎，在个股分析和大盘复盘之前运行：
1. 拉取全球市场隔夜数据（美股/VIX/大宗商品/汇率/美债）
2. 使用 LLM 生成结构化的盘前分析报告
3. 作为邮件报告的新增章节

与大盘复盘的区别：
- 大盘复盘：回顾 A 股当天发生了什么
- 盘前分析：开盘前根据外围环境预判当日方向和投资机会
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from src.config import get_config, Config

logger = logging.getLogger(__name__)

# ─── 外围市场数据符号 ───────────────────────────────────────────
# market_region: US/HK/JP/KR/SG — 用于休市检测；None 表示 24h 商品/外汇不检测
_GLOBAL_MARKET_SYMBOLS = [
    # (code, yf_symbol, group, chinese_name, market_region)
    ("DJI",   "^DJI",   "美股三大指数",   "道琼斯",              "US"),
    ("IXIC",  "^IXIC",  "美股三大指数",   "纳斯达克",            "US"),
    ("SPX",   "^GSPC",  "美股三大指数",   "标普500",            "US"),
    ("RUT",   "^RUT",   "罗素2000小盘",   "罗素2000",           "US"),
    ("SOX",   "^SOX",   "半导体风向标",   "费城半导体SOX",       "US"),
    ("VIX",   "^VIX",   "恐慌情绪",       "VIX恐慌指数",         "US"),
    ("USDCNH","CNH=X",  "汇率",           "离岸人民币",          None),  # 外汇24h，不检测休市
    ("TNX",   "^TNX",   "美债",           "美国10Y国债收益率",   "US"),
    ("GC",    "GC=F",   "大宗商品",       "黄金期货",            None),  # 商品有独立交易时间
    ("CL",    "CL=F",   "大宗商品",       "WTI原油",            None),
]

# ─── 亚太市场数据符号 ───────────────────────────────────────────
_ASIA_PACIFIC_SYMBOLS = [
    ("KS11",   "^KS11",   "亚太市场", "韩国综合KOSPI",      "KR"),
    ("KOSDAQ", "^KQ11",   "亚太市场", "韩国创业板KOSDAQ",   "KR"),
    ("N225",   "^N225",   "亚太市场", "日经225",            "JP"),
    ("HSI",    "^HSI",    "亚太市场", "恒生指数",           "HK"),
    ("HSTECH", "^HSTECH", "亚太市场", "恒生科技指数",       "HK"),
]

# A50 期货在新加坡交易所，yfinance 无稳定符号；改用上证指数(000001.SS)作为A股大盘参考
# 注：A50 期货在新交所（SGX）代码在 yfinance 中不稳定，部分环境用 "XINA50" 或 "FFI"
# 此处用上证指数作为替代，足够反映 A 股大盘方向
_A50_SYMBOL = ("A50", "000001.SS", "A股大盘", "上证指数(替代A50)", "CN")
# 中概股指数（美股交易）
_HXC_SYMBOL = ("HXC", "^HXC", "中概股方向", "中概股HXC", "US")


# ─── 各市场节假日表 ─────────────────────────────────────────────
# 格式：{ region: { (month, day): "节日名称", ... } }
# 仅收录主要休市日；农历节日（春节/中秋等）按近似公历日期覆盖近年常见区间
_MARKET_HOLIDAYS = {
    "US": {
        (1, 1):   "元旦 (New Year's Day)",
        (1, 20):  "马丁·路德·金纪念日附近",
        (2, 17):  "总统日附近 (Presidents' Day)",
        (4, 18):  "耶稣受难日附近 (Good Friday)",
        (5, 26):  "阵亡将士纪念日附近 (Memorial Day)",
        (6, 19):  "六月节 (Juneteenth)",
        (7, 4):   "独立日 (Independence Day)",
        (9, 1):   "劳动节附近 (Labor Day)",
        (11, 27): "感恩节附近 (Thanksgiving)",
        (12, 25): "圣诞节 (Christmas Day)",
    },
    "HK": {
        (1, 1):   "元旦",
        (1, 29):  "农历新年假期附近",
        (1, 30):  "农历新年假期附近",
        (1, 31):  "农历新年假期附近",
        (2, 1):   "农历新年假期附近",
        (2, 17):  "农历新年假期附近（近年）",
        (4, 4):   "清明节附近",
        (4, 5):   "清明节附近",
        (4, 18):  "耶稣受难日附近",
        (4, 21):  "复活节星期一附近",
        (5, 1):   "劳动节",
        (5, 5):   "佛诞附近",
        (5, 31):  "端午节附近",
        (6, 19):  "端午节附近（近年）",
        (7, 1):   "香港特别行政区成立纪念日",
        (9, 22):  "中秋节翌日附近",
        (9, 29):  "中秋节翌日附近（近年）",
        (10, 1):  "国庆节",
        (10, 7):  "重阳节附近",
        (10, 29): "重阳节附近（近年）",
        (12, 25): "圣诞节",
        (12, 26): "圣诞节翌日",
    },
    "JP": {
        (1, 1):   "元旦 (元日)",
        (1, 2):   "元旦假期",
        (1, 3):   "元旦假期",
        (1, 13):  "成人日附近",
        (2, 11):  "建国纪念日",
        (2, 23):  "天皇诞生日",
        (3, 20):  "春分日附近",
        (4, 29):  "昭和日",
        (5, 3):   "宪法纪念日",
        (5, 4):   "绿之日",
        (5, 5):   "儿童日",
        (7, 21):  "海之日附近",
        (8, 11):  "山之日",
        (9, 15):  "敬老日附近",
        (9, 23):  "秋分日附近",
        (10, 13): "体育日附近",
        (11, 3):  "文化日",
        (11, 23): "勤劳感谢日",
    },
    "KR": {
        (1, 1):   "元旦 (신정)",
        (2, 10):  "农历新年附近 (설날)",
        (3, 1):   "三一节",
        (5, 5):   "儿童节 (어린이날)",
        (5, 15):  "佛诞附近 (부처님오신날)",
        (6, 6):   "显忠日 (현충일)",
        (8, 15):  "光复节 (광복절)",
        (9, 15):  "秋夕附近 (추석)",
        (10, 3):  "开天节 (개천절)",
        (10, 9):  "韩文日 (한글날)",
        (12, 25): "圣诞节 (크리스마스)",
    },
    "SG": {
        (1, 1):   "元旦 (New Year's Day)",
        (2, 10):  "农历新年附近 (Chinese New Year)",
        (4, 18):  "耶稣受难日附近 (Good Friday)",
        (5, 1):   "劳动节 (Labour Day)",
        (5, 12):  "卫塞节附近 (Vesak Day)",
        (8, 9):   "国庆日 (National Day)",
        (10, 27): "屠妖节附近 (Deepavali)",
        (12, 25): "圣诞节 (Christmas Day)",
    },
    "CN": {
        # A股/港股通节假日（A股休市时北向资金不交易，需特别标注）
        (1, 1):   "元旦",
        (1, 28):  "农历新年假期附近",
        (1, 29):  "农历新年假期附近",
        (1, 30):  "农历新年假期附近",
        (1, 31):  "农历新年假期附近",
        (2, 1):   "农历新年假期附近",
        (2, 17):  "农历新年假期附近（近年）",
        (4, 4):   "清明节附近",
        (4, 5):   "清明节附近",
        (5, 1):   "劳动节",
        (5, 31):  "端午节附近",
        (6, 19):  "端午节附近（近年）",
        (10, 1):  "国庆节",
        (10, 2):  "国庆节假期附近",
        (10, 3):  "国庆节假期附近",
        (10, 7):  "国庆节假期附近",
        (10, 29): "重阳节附近（近年）",
        (12, 25): "圣诞节（部分机构）",
    },
}


def _get_last_trading_day() -> datetime:
    """获取上一个交易日日期（用于盘前分析检测休市）。

    盘前分析在早上 08:30 运行，所有数据都是上一个交易日的数据。
    如果今天是周一，上一个交易日是上周五；其他日期是昨天。
    返回带时区的 datetime（UTC+8），与 data_date 比较时保持一致。
    """
    tz_cn = timezone(timedelta(hours=8))
    today = datetime.now(tz_cn)
    if today.weekday() == 0:  # 周一
        return today - timedelta(days=3)
    elif today.weekday() == 6:  # 周日（理论上不会在周日运行，但兜底）
        return today - timedelta(days=2)
    elif today.weekday() == 5:  # 周六（同上）
        return today - timedelta(days=1)
    else:
        return today - timedelta(days=1)


def _detect_market_holiday(region: Optional[str], check_date: Optional[datetime] = None) -> Optional[str]:
    """检测指定市场区域在指定日期是否在已知假期附近（±2 天容差）。

    盘前分析场景下，check_date 应为上一个交易日（昨天），
    因为此时所有数据都是上一个交易日的数据。

    Args:
        region: 市场区域代码（US/HK/JP/KR/SG）
        check_date: 要检测的日期，默认为上一个交易日

    Returns:
        假期名称字符串，或 None（非假期）
    """
    if region is None or region not in _MARKET_HOLIDAYS:
        return None

    if check_date is None:
        check_date = _get_last_trading_day()

    holidays = _MARKET_HOLIDAYS[region]

    # 精确匹配 ±2 天（覆盖周末调休和近似的农历节日）
    for offset in (0, -1, -2, 1, 2):
        try:
            d = check_date + timedelta(days=offset)
            key = (d.month, d.day)
            if key in holidays:
                return holidays[key]
        except Exception:
            continue
    return None

# ─── Prompt 模板（详细版——基于用户盘前分析docx模板）─────────────────────
_PRE_MARKET_SYSTEM_PROMPT = """你是一位经验丰富的A股盘前分析师，你的职责是撰写一份专业、详尽的盘前分析报告。

## 核心要求
- **详尽展开**：每个章节都要充分展开论述，不允许一句话带过。目标输出 4000-6000 字。
- **数据驱动**：每一项判断必须引用具体的外围/亚太数据，标明数值和方向。
- **逻辑链条完整**：从外围/亚太数据 → 大盘判断 → 板块映射 → 具体标的 → 操作策略，必须有清晰的推导过程。
- **九个部分缺一不可**：严格按以下九个章节输出，每个章节都必须有实质内容。
- **可操作**：给出具体的板块和个股方向，让读者知道开盘后该怎么做。
- 避免使用绝对化词语，保持客观冷静。
- **市场休市处理**：数据开头有「市场交易状态概要」板块，标注了各市场最近交易日是否休市（⚠️）。若某市场最近交易日休市（如港股假期），该市场数据为更早的交易日数据，分析时需注明"XX市场最近交易日休市"而非简单标"数据缺失"。休市不影响其他市场的分析判断。
- **A股交易时间约束**：A股（沪深/创业板/科创板）交易时间为北京时间 **09:30-11:30**、**13:00-15:00**。报告中的时间节点必须按此书写，开盘指 09:30、午盘指 11:30-13:00、尾盘指 14:30-15:00，绝不可以用其他市场的开盘时间（如美股 21:30、港股 09:30）替代。
- **假如某些数据不可用，标注"⚪ 数据缺失"并用已知信息做有限推断，不可跳过该章节。**

## 输出格式（必须严格按以下九个章节输出，一个都不能少）

# {date} 盘前分析

---

## 一、核心结论

>> 用一段话（100-200字）概括今日盘前总体判断，说明偏多/偏空/中性及核心逻辑。

### 1.1 大盘方向判断
- 总体判断: （看多 / 偏多 / 中性 / 偏空 / 看空）
- 判断标准（硬约束）：
  - 看多：外围环境偏暖 + 量能可能放大 + 资金北上流入预期 + 无重大外部利空 → 需满足至少3条
  - 偏多：外围环境平稳 + 量能稳定 + 资金结构偏多 + 有局部热点题材驱动 → 需满足至少3条
  - 中性：外围信号混杂或平稳 + 量能无明显变化 + 资金方向不明确 → 需满足至少2条
  - 偏空：外围环境有压制 + 量能可能萎缩 + 北向流出预期 + 有不确定性事件 → 需满足至少3条
  - 看空：外围环境大幅下跌 + 量能显著萎缩 + 资金大规模流出预期 + 重大利空事件 → 需满足至少3条
- 判断依据: （至少 4-5 条有数据支撑的判断依据，每条要解释"为什么"）
- 如果判断依据之间存在矛盾，用 1-2 句话说明矛盾点和关键不确定性
- 置信度: （高/中/低，并解释为什么是这个置信度）

### 1.2 执行清单
- 今日核心策略: （用一段话阐明今天的总体应对思路）
- 9:25 开盘集合竞价后重点关注指标
- 9:45 开盘15分钟后重点关注指标
- 对应的操作预案（如果信号确认则如何操作）
- 风险评估: （今日最大的 2-3 个风险点，触发条件和应对方式）
- 特别关注: （需要重点盯盘的指标或事件，看到什么信号后做什么动作）

---

## 二、盘前多空素材

对以下维度逐一分析，每个维度需要包含：客观数据（具体数字）、对A股的影响分析、映射逻辑、信号灯（🟢偏多 / 🟡中性待验证 / 🔴偏空 / ⚪数据缺失）、重点关注个股、边界条件和例外情况。

### 2.1 外围市场环境

| 指标 | 最新数据 | 5日趋势 | 映射维度 | 对A股影响 | 信号灯 | 重点观察 |
|------|---------|---------|---------|----------|--------|---------|
| DJI(道琼斯) | | | 大盘蓝筹方向偏好 | | | 大金融、消费白马 |
| IXIC(纳斯达克) | | | 科技成长方向偏好 | | | AI/半导体/CPO等科技板块 |
| SPX(标普500) | | | 广泛/价值方向 | | | 周期和金融承接 |
| RUT(罗素2000) | | | 小盘方向偏好 | | | 中证2000/中证500小盘 |
| SOX(费城半导体) | | | 半导体方向偏好 | | | 半导体板块承接力度 |
| A50(富时A50期货) | | | 外资对A股大盘方向偏好 | | | A股开盘涨跌幅 |
| HXC(中概股指数) | | | 外资对A股核心资产偏好 | | | 互联网/新能源/消费 |
| VIX(恐慌指数) | | | 市场恐慌程度 | | | A股被全球恐慌情绪波及程度 |
| 美债10年期收益率 | | | 估值压力 | | | 高估值成长股 |
| USDCNH(离岸人民币) | | | 资本流动压力 | | | 北向资金净流向变化 |
| GC(黄金) | | | 避险/实际利率 | | | 黄金股承接和避险情绪 |
| CL(原油) | | | 通胀/能源价格 | | | 油气/化工/航运 |

### 2.2 亚太盘前市场环境

| 指标 | 最新数据 | 5日趋势 | 映射维度 | 对A股影响 | 信号灯 | 重点观察 |
|------|---------|---------|---------|----------|--------|---------|
| KS11(韩国综合) | | | 半导体/亚太科技方向偏好 | | | 存储半导体承接 |
| KOSDAQ(韩国创业板) | | | 半导体设备方向偏好 | | | 存储半导体设备方向 |
| N225(日经225) | | | 亚太科技方向偏好 | | | — |
| HSI(恒生指数) | | | 港股方向偏好 | | | — |
| HSTECH(恒生科技) | | | 港股科技方向偏好 | | | — |

### 2.3 重要事件

列出当日影响A股走势的关键事件，包括：
- 事件名称和描述
- 时间范围（已发生/预计发生）
- 对A股的影响方向
- 信号灯
- 重点观察指标

### 2.4 盘前综合信号
- 偏多因子: 逐项列出并解释
- 偏空因子: 逐项列出并解释
- 中性因子: 逐项列出并解释
- 综合评分: （多空力量对比的总体判断）

---

## 三、今日驱动逻辑推演

构建完整的驱动逻辑链，说明外围/亚太信号如何传导到A股板块。推演公式示例：「纳斯达克强势 + 费城半导体大涨 = A股科技板块开盘偏强，AI/芯片方向承接力度是关键」。如果出现新的变量会如何改变逻辑链。

- 基准路径: （最可能的走势，概率约XX%）
  - 逻辑链条: （从外围→开盘→盘中→收盘的完整推演）
  - 触发条件: （什么情况下这个路径成立）
  - 关键节点: （09:30 开盘、10:00/10:30、11:30 午盘前、13:00 下午开盘、14:30 尾盘、15:00 收盘，各阶段可能的表现；必须严格使用A股交易时间）

- 乐观路径: （偏多情景，概率约XX%）
  - 逻辑链条: （什么因素共振才能走出乐观行情）
  - 触发条件: （需要看到什么盘面信号）
  - 目标空间: （如果走乐观路径，指数可能到什么位置）

- 悲观路径: （偏空情景，概率约XX%）
  - 逻辑链条: （什么因素可能导致悲观走势）
  - 触发条件: （需要警惕什么风险信号）
  - 防守位: （如果走悲观路径，关键支撑在哪）

---

## 四、大盘环境判断

### 4.1 外围市场环境小结
一句话总结外围市场对A股的影响方向，用具体数据支撑。

### 4.2 核心矛盾与关键变量
列出当前市场面临的核心矛盾、关键不确定性因素，以及需要跟踪的数据变化方向（至少 4 点）。每个变量要说明：矛盾点是什么、为什么重要、如何影响今日走势。

---

## 五、潜在投资机会

### 5.1 外围板块映射机会

对每个外围强势/弱势板块，列出对应的A股映射标的（给出具体股票名称和代码，帮助读者快速锁定目标）：

| 外围标的 | 方向 | 映射板块 | A股对应标的 | 信号灯 | 重点观察 |
|---------|------|---------|-----------|--------|---------|
| SMH(半导体ETF) / NVDA / TSM | | 半导体 | 中芯国际/北方华创/韦尔股份 | | 芯片板块承接力度 |
| SOXX / MU / AMD | | 存储/半导体设备 | 兆易创新/澜起科技/通富微电 | | 存储芯片需求信号 |
| VGT(科技ETF) / AAPL | | 消费电子 | 立讯精密/歌尔股份/蓝思科技 | | 果链板块热度 |
| IGV(软件ETF) / MSFT / ORCL | | 云计算/AI软件 | 金山办公/用友网络/中望软件 | | AI应用落地节奏 |
| ARKQ / TSLA | | 新能源车/自动驾驶 | 比亚迪/宁德时代/德赛西威 | | 新能源车销量数据 |
| GDX(黄金矿业ETF) | | 黄金 | 紫金矿业/山东黄金 | | 金价趋势和避险情绪 |
| LME铜 / LME铝 | | 有色金属 | 江西铜业/中国铝业/北方稀土 | | 大宗商品价格走势 |

（根据实际外围数据灵活添加或删减行，但要保留核心映射逻辑）

### 5.2 盘中重点关注
列出当日盘中最需要关注的影响因素，包括时间节点、影响的板块或个股、以及看到信号后的应对。

---

## 六、市场资金结构

### 6.1 港股资金结构
基于恒生指数/恒生科技隔夜表现，分析港股资金流向和结构特点，预判对A股的传导影响。

### 6.2 A股资金结构预判

如果无法获取实时A股数据，请在"最新数据"栏标注"⚪ 盘前数据不可得"，并基于外围和逻辑做合理预判：

| 资金维度 | 最新数据 | 趋势状态 | 方向判断 |
|---------|---------|---------|---------|
| 两市成交额预估 | ⚪ 盘前数据不可得 | 基于外围情绪预估 | 活跃/低迷/中性 |
| 北向资金预判 | ⚪ 盘前数据不可得 | 基于汇率+A50期货预估 | 偏多/偏空/中性 |
| 主力资金预判 | ⚪ 盘前数据不可得 | 基于外围板块映射预估 | 态度偏多/偏空 |
| 融资余额变化 | ⚪ 盘前数据不可得 | 基于市场情绪预估 | 杠杆偏多/偏空 |
| 资金风格预判 | ⚪ 盘前数据不可得 | 基于大小盘信号预估 | 偏好大盘/成长 |
| 行业资金流入预判 | ⚪ 盘前数据不可得 | 基于外围映射预估 | 列出3个预判流入方向 |
| 行业资金流出预判 | ⚪ 盘前数据不可得 | 基于外围利空预估 | 列出3个预判流出方向 |

---

## 七、舆情与V反

### 7.1 全球机构观点汇总
梳理主流机构对A股/中国资产的最新观点（基于已知信息和市场常识）：
- 观点来源和核心论点
- 与市场实际走势是否一致
- 是否存在共识或分歧
- 信号灯

### 7.2 舆情V反分析
分析市场情绪和可能的反向指标：
- 主要讨论方向和情绪偏向
- 可作为反向指标或确认信号的讨论点
- ⚠️ 社交媒体观点仅供参考，不能替代客观数据分析

### 7.3 隔夜要闻与事件日历
- 梳理隔夜发生的重要新闻和政策动态
- 今日/近期可能影响市场的政策节点、经济数据发布、重要会议
- 舆情温度: （市场情绪是偏乐观、偏谨慎还是恐慌，有什么信号）

---

## 八、指标汇总决策

**硬性要求：** 每个指标必须引用具体的客观数据，禁止使用模糊表述。例如不要说「外围科技偏弱，A股科技承压」，要说「费城半导体指数-2.1%，纳指-1.5%，北向资金近3日净流出科创50合计-15.7亿，科技板块面临外围压力+资金外流双重考验，信号灯偏空，权重扣减」。

| 指标 | 权重 | 方向判断 | 数据支撑 |
|------|------|---------|---------|
| 外围市场环境（确定大方向） | 30% | 偏多/偏空/中性 | 引用具体数据 |
| 板块映射（确定投资机会） | 40% | 存在/不存在/局部 | 引用具体数据 |
| A股资金结构 | 20% | 偏多/偏空/中性 | 引用具体数据或标注预判 |
| 舆情与V反 | 10% | 偏多/偏空/中性 | 引用具体数据 |
| **综合判断** | **100%** | 给出最终方向+置信度 | 汇总以上 |

---

## 九、信号灯使用规则

| 信号灯 | 含义 | 使用场景 |
|--------|------|---------|
| 🟢 绿色 | 偏多/积极 | 外围信号、资金流向、个股映射等多维度形成共振支持 |
| 🟡 黄色 | 中性/待验证 | 信号不明确或有矛盾，需要盘中成交量、资金承接进一步验证 |
| 🔴 红色 | 偏空/承压 | 外围信号、资金流向、个股映射等多维度形成压制或利空共振 |
| ⚪ 灰色 | 数据缺失/无法判断 | 数据不完整或无法形成方向性判断，不可强行判断 |

**重要约束：**
- 信号灯不是交易指令。🟢不等于买入，🔴不等于卖出
- 🟡黄色是最常用的信号，意味着需要进一步验证
- 灰色信号下不可强行给出方向判断
- 外围信号灯只表示外围影响方向，不代表A股确定性走势
- 外围映射 + A股/港股本地资金流向共振 = 信号增强
- 外围映射 + A股/港股本地资金流向背离 = 信号减弱，标注「待观察」
- 数据缺失时标注「⚪ 未纳入」，不可用推测替代数据
- 如果只有外围映射而没有本地市场验证，只写「观察」，不写确定结论

### 操作约束
（至少列出 5 条今日操作纪律，每条要结合今日的市场判断来制定，不能是泛泛之谈）

---

> ⚠️ 以上分析仅供参考，不构成投资建议。市场有风险，投资需谨慎。
"""


def _fetch_global_market_data(include_a50: bool = True, include_hxc: bool = True, include_asia: bool = True) -> str:
    """
    通过 yfinance 拉取全球市场隔夜数据（含亚太市场）。

    数据缺失时会检测对应市场是否休市，并明确标注而非静默跳过。

    Returns:
        格式化的数据文本块，失败时返回空字符串
    """
    symbols = list(_GLOBAL_MARKET_SYMBOLS)
    if include_a50:
        symbols.append(_A50_SYMBOL)
    if include_hxc:
        symbols.append(_HXC_SYMBOL)
    if include_asia:
        symbols.extend(_ASIA_PACIFIC_SYMBOLS)

    data_lines = []       # 有数据的行
    holiday_lines = []    # 休市/数据缺失的标注行

    for row in symbols:
        code, yf_sym, group, cn_name, region = row  # type: ignore[misc]
        try:
            import yfinance as yf
            t = yf.Ticker(yf_sym)
            h = t.history(period="5d")
            if h.empty:
                # 数据为空 → 检测上一个交易日是否休市
                # 盘前分析在早上运行，所有数据都是上一个交易日的数据
                last_trading_day = _get_last_trading_day()
                holiday = _detect_market_holiday(region, check_date=last_trading_day)
                date_str = last_trading_day.strftime("%m月%d日")
                if holiday:
                    holiday_lines.append(
                        f"- ⚠️ {cn_name}({code}): 最近交易日（{date_str}）休市 — {holiday} [{group}]"
                    )
                else:
                    holiday_lines.append(
                        f"- ⚠️ {cn_name}({code}): 数据不可用（可能{date_str}休市、非交易日或数据延迟）[{group}]"
                    )
                continue

            cur = float(h.iloc[-1]["Close"])
            prev_day = float(h.iloc[-2]["Close"]) if len(h) > 1 else cur
            chg_pct = ((cur - prev_day) / prev_day * 100) if prev_day else 0
            trend_5d = ((cur - float(h.iloc[0]["Close"])) / float(h.iloc[0]["Close"]) * 100) if len(h) > 1 else 0
            direction = "↑" if chg_pct > 0 else "↓" if chg_pct < 0 else "-"
            trend_dir = "↑" if trend_5d > 0 else "↓" if trend_5d < 0 else "-"

            # 检查数据新鲜度：数据日期是否为上一个交易日
            # h.index[-1] 可能是 Timestamp 或 datetime，统一转为 date
            _idx_val = h.index[-1]
            if hasattr(_idx_val, 'date'):
                _data_date = _idx_val.date()
            else:
                # 兼容纯 datetime 对象
                _data_date = _idx_val if hasattr(_idx_val, 'year') else None

            last_trading_day = _get_last_trading_day()
            last_trading_date = last_trading_day.date()
            stale_note = ""
            if _data_date and _data_date != last_trading_date:
                # 数据不是上一个交易日的数据
                days_behind = (last_trading_date - _data_date).days
                if days_behind == 1:
                    stale_note = f" [最新数据日期: {_data_date}，为前一个交易日数据]"
                elif days_behind >= 2:
                    stale_note = f" [最新数据日期: {_data_date}，隔{days_behind}个交易日，可能期间休市]"

            data_lines.append(
                f"- {cn_name}({code}): {cur:.2f} | 日变动: {direction}{abs(chg_pct):.2f}% "
                f"| 5日趋势: {trend_dir}{abs(trend_5d):.2f}% [{group}]{stale_note}"
            )
        except Exception as e:
            logger.debug("yfinance 拉取 %s 失败: %s", code, e)
            last_trading_day = _get_last_trading_day()
            holiday = _detect_market_holiday(region, check_date=last_trading_day)
            date_str = last_trading_day.strftime("%m月%d日")
            if holiday:
                holiday_lines.append(
                    f"- ⚠️ {cn_name}({code}): 最近交易日（{date_str}）休市 — {holiday} [{group}]"
                )
            else:
                holiday_lines.append(
                    f"- ⚠️ {cn_name}({code}): 数据拉取失败 [{group}]"
                )

    if not data_lines and not holiday_lines:
        return ""

    # 组装输出：先放数据，再放休市/缺失标注
    result_parts = []
    result_parts.append("## 市场交易状态概要\n")
    if holiday_lines:
        result_parts.append("\n".join(holiday_lines))
    else:
        result_parts.append("✅ 所有可检测市场数据正常获取，未检测到主要市场休市。")

    if data_lines:
        result_parts.append("\n\n## 隔夜全球及亚太市场数据\n")
        result_parts.append("\n".join(data_lines))

    return "\n".join(result_parts)


def generate_pre_market_analysis(
    analyzer: Optional[Any] = None,
    config: Optional[Config] = None,
) -> str:
    """
    生成盘前多维分析报告。

    Args:
        analyzer: AI 分析器实例（GeminiAnalyzer 等，需支持 generate_text）
        config: 配置对象（可选，未传时读取全局配置）

    Returns:
        Markdown 格式的盘前分析报告文本；失败时返回空字符串
    """
    runtime_config = config or get_config()

    # 1. 检查 AI 分析器是否可用
    if analyzer is None:
        logger.warning("[盘前分析] 无可用 AI 分析器，跳过盘前分析")
        return ""

    if not hasattr(analyzer, "generate_text") or not callable(getattr(analyzer, "generate_text", None)):
        logger.warning("[盘前分析] 分析器不支持 generate_text，跳过盘前分析")
        return ""

    # 2. 拉取全球市场数据
    logger.info("[盘前分析] 正在拉取全球市场数据...")
    global_data = _fetch_global_market_data()
    if not global_data:
        logger.warning("[盘前分析] 未能获取任何全球市场数据，跳过盘前分析")
        return ""

    logger.info("[盘前分析] 全球市场数据获取成功，数据行数=%d", global_data.count("\n") + 1)

    # 3. 构建 Prompt
    tz_cn = timezone(timedelta(hours=8))
    today_str = datetime.now(tz_cn).strftime("%Y年%m月%d日")
    prompt = _PRE_MARKET_SYSTEM_PROMPT.format(date=today_str)

    full_prompt = f"""{prompt}

---

{global_data}

---

请基于以上隔夜全球市场数据（含亚太），生成 {today_str} 的完整盘前分析报告。

重要要求：
1. 必须严格按九个章节（一～九）完整输出，每个章节都不能跳过。
2. 每个章节都要详尽展开、深入分析，目标输出 4000-6000 字。
3. 数据缺失的维度标注「⚪ 数据缺失」并按已知信息做有限推断。
4. 板块映射部分必须给出具体的A股对应标的名称。
5. 操作约束部分必须结合今日市场判断来写，不能泛泛而谈。"""

    # 4. 调用 LLM（含可选的 LLM 用量记录）
    logger.info("[盘前分析] 正在调用 AI 生成盘前分析报告...")
    llm_started_at = time.perf_counter()
    
    # 安全导入 LLM 记录器（模块可能不存在）
    _record_llm_run = _record_llm_run_started = None
    try:
        from src.core.llm_recorder import record_llm_run as _r, record_llm_run_started as _rs
        _record_llm_run = _r
        _record_llm_run_started = _rs
    except ImportError:
        pass

    _model = getattr(runtime_config, "litellm_model", None)
    if _record_llm_run_started:
        _record_llm_run_started(
            provider="litellm",
            model=_model,
            call_type="pre_market_analysis",
        )
    try:
        result = analyzer.generate_text(full_prompt, max_tokens=12288, temperature=0.7)
    except Exception as exc:
        if _record_llm_run:
            _record_llm_run(
                success=False,
                provider="litellm",
                model=_model,
                call_type="pre_market_analysis",
                duration_ms=int((time.perf_counter() - llm_started_at) * 1000),
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        logger.error("[盘前分析] AI 生成失败: %s", exc)
        return ""

    if _record_llm_run:
        _record_llm_run(
            success=bool(result),
            provider="litellm",
            model=_model,
            call_type="pre_market_analysis",
            duration_ms=int((time.perf_counter() - llm_started_at) * 1000),
            error_type=None if result else "EmptyResponse",
            error_message=None if result else "empty pre-market analysis response",
        )

    if not result:
        logger.warning("[盘前分析] AI 返回空内容")
        return ""

    logger.info("[盘前分析] 盘前分析报告生成成功，长度=%d", len(result))
    return result
