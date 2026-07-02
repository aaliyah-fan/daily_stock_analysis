# -*- coding: utf-8 -*-
"""
盘前多维分析模块 (Pre-Market Analysis)

独立的盘前分析引擎，在个股分析和大盘复盘之前运行：
1. 拉取全球市场隔夜数据（美股/VIX/大宗商品/汇率/美债/美股个股ETF）
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
    ("GC",    "GC=F",   "大宗商品",       "COMEX黄金",           None),  # 商品有独立交易时间
    ("BZ",    "BZ=F",   "大宗商品",       "布伦特原油",          None),
    ("HG",    "HG=F",   "大宗商品",       "COMEX铜",             None),
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
_A50_SYMBOL = ("A50", "000001.SS", "A股大盘", "上证指数(替代A50)", "CN")
# 中概股指数（美股交易）
_HXC_SYMBOL = ("HXC", "^HXC", "中概股方向", "中概股HXC", "US")

# ─── 美股个股及ETF数据符号（用于第五章投资机会映射）─────────────────
# (code, yf_symbol, group, chinese_name, market_region, a_stock_mappings)
_US_STOCK_ETF_SYMBOLS = [
    ("SMH",   "SMH",   "半导体ETF",  "半导体ETF(SMH)",       "US",
     "中际旭创/新易盛/工业富联"),
    ("NVDA",  "NVDA",  "半导体个股", "英伟达(NVDA)",          "US",
     "中际旭创/新易盛/工业富联"),
    ("TSM",   "TSM",   "半导体个股", "台积电(TSM)",           "US",
     "中芯国际/北方华创"),
    ("SOXX",  "SOXX",  "半导体ETF",  "费城半导体ETF(SOXX)",   "US",
     "兆易创新/北方华创/通富微电/长电科技"),
    ("MU",    "MU",    "半导体个股", "美光(MU)",              "US",
     "兆易创新/澜起科技"),
    ("AMD",   "AMD",   "半导体个股", "超威半导体(AMD)",       "US",
     "通富微电/长电科技"),
    ("VGT",   "VGT",   "科技ETF",    "科技ETF(VGT)",          "US",
     "歌尔股份/立讯精密/蓝思科技"),
    ("AAPL",  "AAPL",  "消费电子",   "苹果(AAPL)",            "US",
     "歌尔股份/立讯精密/蓝思科技"),
    ("IGV",   "IGV",   "软件ETF",    "软件ETF(IGV)",          "US",
     "金山办公/用友网络/万兴科技"),
    ("MSFT",  "MSFT",  "软件个股",   "微软(MSFT)",            "US",
     "金山办公/用友网络"),
    ("ORCL",  "ORCL",  "软件个股",   "甲骨文(ORCL)",          "US",
     "中望软件/用友网络"),
    ("ARKQ",  "ARKQ",  "新能源ETF",  "新能源车ETF(ARKQ)",     "US",
     "宁德时代/三花智控/德赛西威"),
    ("TSLA",  "TSLA",  "新能源个股", "特斯拉(TSLA)",          "US",
     "宁德时代/三花智控"),
    ("GDX",   "GDX",   "黄金矿业",   "黄金矿业ETF(GDX)",      "US",
     "紫金矿业/山东黄金"),
]


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


# ─── 涨跌动因分类参考表 ─────────────────────────────────────────
_MOTIVATION_CLASSIFICATION = """涨跌动因归类参考：
- 宏观流动性驱动（映射价值：高）：美债收益率上/下行、美联储降息预期升温/降温、美元指数波动等，属于全市场Beta整体性行情
- 产业基本面驱动（映射价值：最高）：订单指引上调/下调、资本开支扩张/收缩、财报营收及毛利率超预期/不及预期、行业库存周期反转/恶化
- 产品技术迭代驱动（映射价值：中）：全新硬件产品落地/延迟、核心技术迭代升级/不及预期、AI商业化场景提速/降速
- 公司个体件驱动（映射价值：低）：股份回购、股本拆分、管理层变动、指数成分股调整，仅对个股产生影响
- 短线资金情绪驱动（映射价值：开盘跟随）：无基本面支撑的纯资金抱团炒作，市场小作文，资金获利止盈，仅影响A股、港股开盘短期情绪"""


# ─── Prompt 模板（基于用户盘前分析skill模板docx V2）─────────────────────
_PRE_MARKET_SYSTEM_PROMPT = """你是一位经验丰富的A股盘前分析师，你的职责是撰写一份专业、详尽、有深度的盘前分析报告。

## 核心要求
- **详尽展开**：每个章节都要充分展开论述，不允许一句话带过。目标输出 5000-8000 字。
- **数据驱动**：方向判断必须基于数据，而非机构观点。每一项判断必须引用具体的外围/亚太数据，标明数值和方向。
- **九个部分缺一不可**：严格按以下九个章节输出，每个章节都必须有实质内容。
- **A股交易时间约束**：A股交易时间为北京时间 09:30-11:30、13:00-15:00。盘前分析中的时间节点必须按此书写，开盘指 09:30、午盘指 11:30-13:00、尾盘指 14:30-15:00。
- **信号灯规则**（遵循中国股市红涨绿跌惯例）：
  - 🔴 红色 = 偏正面观察（外围信号可能对A股/港股形成正面情绪或支撑）
  - 🟡 黄色 = 中性/扰动/待验证（信号不明确，需盘中进一步验证）
  - 🟢 绿色 = 偏负面/风险压力（可能形成估值压力、情绪压制或风险扰动）
  - ⚪ 白色 = 数据缺口/不参与判断
- **市场休市处理**：数据开头有「市场交易状态概要」板块，标注各市场最近交易日是否休市（⚠️）。若某市场最近交易日休市，需注明"XX市场最近交易日休市"而非标"数据缺失"。
- **假如某些数据不可用，标注"⚪ 数据缺失"并用已知信息做有限推断，不可跳过该章节。**

## 输出格式（必须严格按以下九个章节输出，一个都不能少）

# {date} 盘前分析

---

## 一、核心结论

### 1.1 大盘方向判断
必须给出明确方向判断（进攻 / 中性偏进攻 / 中性 / 中性偏防御 / 防御），再展开论据。

判断标准（硬约束）：
- 进攻：外围情绪偏暖 + A股量能持续放大 + 资金净流入 + 无明显外部风险 → 至少满足3项
- 中性偏进攻：外围平稳 + 量能稳定 + 资金结构性流入 + 有局部风险但可控 → 至少满足3项
- 中性：外围信号混杂或平淡 + 量能无明显变化 + 资金流向不明确 → 至少满足2项
- 中性偏防御：外围有压力 + 量能可能萎缩 + 资金流出迹象 + 有明确风险事件 → 至少满足3项
- 防御：外围显著下跌 + 量能明显萎缩 + 资金大幅流出 + 重大利空事件 → 至少满足3项

包括：
- 一句话判断今日行情
- 今日最核心矛盾（1-2句话概括）
- 今日不确定性（只列可能影响行情方向的关键不确定因素，需说明为什么不确定、可能向哪个方向演变，最多3点）
- 置信度: （高/中/低，并解释）

### 1.2 执行清单
- 今日最重要观察条件（必须明确列出开盘后需要第一时间确认的关键信号，以及对应的验证标准，最多3点）
- 9:25 开盘集合竞价后重点观察对象及指标
- 9:45 开盘15分钟后重点观察对象及指标
- 对应的操作预案（如果信号确认则如何操作）

---

## 二、今日核心主线

最多给三条主线，必须基于下面呈现的客观数据推演（外盘数据、事件数据、资金数据、市场情绪等），而非机构观点。

每条主线必须包括：传导链条、A股影响、港股影响、事实依据、推断、策略含义、需要验证的条件、信号灯、重点观察个股。需考虑本土独立定价因素。

每条主线需填写以下独立定价逻辑表：

| 项目 | 内容 |
|------|------|
| 该板块是否有A股独立定价逻辑 | 是/否（如国产替代、政策支持、融资盘偏好等） |
| 若外围信号与A股独立逻辑方向冲突 | 应以哪个为主？（如：科技板块以A股自身资金流向/科创50走势为主，有色以商品价格为主） |
| 冲突情况下的处理规则 | 如：融资余额持续增加+科创50走强=科技板块可独立于美股走强 |

### 2.1 主线一：...
### 2.2 主线二：...
### 2.3 主线三：（如有）

---

## 三、关键事件与倒计时

按以下状态分类列出当日及近期关键事件：
- 近期已发生及发生时间
- 已公布未发生，预计发生时间
- 待发布
- 待核查

注意：未发布事项不给方向灯，只能给扰动灯或后续观察灯。

---

## 四、今日大方向判断

一句话判断：进攻/中性/防御，结合下面的外围市场表现、主要事件及A股独立性进行简单分析。必须增加客观数据及信号灯，并分析对A股影响、待观察指标。

### 4.1 外围市场表现
需填充客观数据；分析美股涨跌动因并根据涨跌动因判断其对A股的映射价值强弱；分析美股对A股影响并根据影响给出信号灯、待观察指标。

| 标的 | 最近1个交易日表现 | 最近5个交易日趋势 | 涨跌动因 | 对A股映射价值（强/中性/弱） | 对应维度 | 对A股影响 | 影响信号灯（红/黄/绿/白） | 待观察指标 |
|------|------|------|------|------|------|------|------|------|
| DJI(道指) | | | | | 美股大盘风险偏好 | A股整体风险偏好 | | A股开盘是否跟随 |
| IXIC(纳指) | | | | | 科技成长风险偏好 | AI、半导体、CPO、软件等科技赛道 | | 科技权重和成长股承接力度 |
| SPX(标普) | | | | | 蓝筹/价值风格 | 银行、保险、红利、工业 | | 看红利和金融承接力度 |
| RUT(罗素2000) | | | | | 小盘风险偏好 | 中证2000、北证50等小盘 | | 看小盘题材承接力度 |
| SOX(费城半导体) | | | | | 半导体风险偏好 | 半导体、设备、材料等 | | 看半导体题材承接力度 |
| A50(富时A50期货) | | | | | 外资对A股大盘风险偏好 | A股蓝筹风险偏好 | | A股开盘是否跟随 |
| HXC(中概股指数) | | | | | 外资对A股新消费类资产风险偏好 | 互联网、新能源、消费、医药风险偏好 | | 看港股、互联网个股承接力度 |
| VIX(恐慌指数) | | | | | 避险情绪 | 高波动、高弹性题材 | | A股被全线抛售还是高波动题材降温 |
| 美债收益率10年 | | | | | 估值压力 | 高估值成长股 | | 看高估值板块承接力度 |
| USDCNH(离岸人民币) | | | | | 流动性压力 | 人民币资产情绪 | | 看人民币汇率变化 |
| GC(COMEX黄金) | | | | | 避险/实际利率 | 黄金 | | 看金价持续性和黄金股承接 |
| BZ(布伦特原油) | | | | | 通胀/能源价格 | 三桶油、油服 | | 看油价持续性 |
| HG(COMEX铜) | | | | | 有色/工业需求 | 有色金属板块 | | 看铜价趋势 |

涨跌动因归类参考：
- 宏观流动性驱动（映射价值：高）
- 产业基本面驱动（映射价值：最高）
- 产品技术迭代驱动（映射价值：中）
- 公司个体件驱动（映射价值：低）
- 短线资金情绪驱动（映射价值：开盘跟随）

### 4.2 亚太开盘市场表现
需填充客观数据（韩国/香港股市开盘价表现）；分析韩国开盘涨跌动因并判断映射价值强弱；分析对A股影响并给出信号灯、待观察指标。

| 标的 | 开盘价表现 | 最近5个交易日趋势 | 涨跌动因 | 对A股映射价值（强/中性/弱） | 对应维度 | 对A股影响 | 影响信号灯（红/黄/绿/白） | 待观察指标 |
|------|------|------|------|------|------|------|------|------|
| KS11(韩国综指) | | | | | 半导体/亚太科技风险偏好 | 存储、半导体封测等 | | 看存储、半导体封测承接力度 |
| KOSDAQ(韩国创业板) | | | | | 半导体风险偏好 | 存储、半导体封测等 | | 看存储、半导体封测承接力度 |
| N225(日经225) | | | | | 亚太股市风险偏好 | | | |
| HSI(恒生指数) | | | | | 港股风险偏好 | | | |
| HSTECH(恒生科技) | | | | | 港股科技风险偏好 | | | |

### 4.3 主要事件

| 事件类型 | 事件内容 | 时间范围 | 对A股影响 | 影响信号灯 | 待观察指标 |
|---------|---------|---------|---------|---------|---------|
| 中美外交政策 | | | | | |
| 通胀加息预期 | | | | | |
| 新闻联播 | | | | | |
| 其他重要会议 | | | | | |
| 盘后公告 | | | | | |

---

## 五、今天投资机会

一句话判断今天可能的投资机会，结合下面的外围板块表现、主要待关注事件及A股独立性进行分析，必须增加客观数据及信号灯，并分析对A股映射个股、待观察指标。

### 5.1 外围板块及个股表现
需填充客观数据；分析美股ETF及个股涨跌动因并判断映射价值强弱，判断核心样本是否与指数一致，内部是否分化；分析对A股影响并给出信号灯、待观察指标。

| 标的 | 最近1个交易日表现 | 最近5个交易日趋势 | 涨跌动因 | 对A股映射价值（强/中性/弱） | 对应维度 | A股映射个股 | 影响信号灯 | 待观察指标 |
|------|------|------|------|------|------|------|------|------|
| SMH(半导体ETF) | | | | | 半导体算力板块风险偏好 | 中际旭创/新易盛/工业富联 | | 看核心算力股承接 |
| NVDA(英伟达) | | | | | 半导体算力 | 中际旭创/新易盛/工业富联 | | 看核心算力股承接 |
| TSM(台积电) | | | | | 半导体代工 | 中芯国际/北方华创 | | |
| SOXX(费城半导体ETF) | | | | | 半导体存储与设备材料 | 兆易创新/北方华创/通富微电/长电科技 | | 看芯片链强弱 |
| MU(美光) | | | | | 半导体存储 | 兆易创新/澜起科技 | | 看存储芯片需求信号 |
| AMD(超威半导体) | | | | | 半导体设备材料 | 通富微电/长电科技 | | |
| VGT(科技ETF) | | | | | 消费电子风格偏好 | 歌尔股份/立讯精密/蓝思科技 | | |
| AAPL(苹果) | | | | | 消费电子 | 歌尔股份/立讯精密/蓝思科技 | | 果链板块热度 |
| IGV(软件ETF) | | | | | 软件板块风险偏好 | 金山办公/用友网络/万兴科技 | | AI应用落地节奏 |
| MSFT(微软) | | | | | 软件/AI应用 | 金山办公/用友网络 | | |
| ORCL(甲骨文) | | | | | 软件/数据库 | 中望软件/用友网络 | | |
| ARKQ(新能源ETF) | | | | | 新能源车/机器人 | 宁德时代/三花智控/德赛西威 | | 新能源车销量数据 |
| TSLA(特斯拉) | | | | | 新能源车/自动驾驶 | 宁德时代/三花智控 | | |
| GDX(黄金矿业ETF) | | | | | 黄金股 | 紫金矿业/山东黄金 | | 金价趋势和避险情绪 |
| HG(COMEX铜) | | | | | 有色金属 | 江西铜业/北方稀土 | | 大宗商品价格走势 |

（根据实际数据灵活增减行，保留核心映射逻辑）

### 5.2 今日待关注事项
说明今天可能影响大盘的宏观、中观、微观事项（包含重点公司领导讲话发言），及发生时间、影响链条。

---

## 六、市场资金结构

### 6.1 港股资金结构
必须先列最近5个港股交易日客观数据，净流入/净流出的总量，并拆分结构，再分析。基于恒生指数/恒生科技隔夜表现分析港股资金流向和结构特点，预判对A股的传导影响。

### 6.2 A股资金结构
必须先列最近5个A股交易日客观数据，拆分数据维度如下（盘前不可得时标注"⚪ 盘前数据不可得"并做合理预判）：

| 资金类别 | 最近状态 | 方向判断 |
|---------|---------|---------|
| 总量成交额 | XX亿，环比X% | 放量/缩量/稳定 |
| 主力资金净流向 | XX亿 | 净流入/净流出 |
| 超大单净流向 | XX亿 | 净流入/净流出（大资金真实态度） |
| 融资余额变化 | XX亿，环比X% | 增加/减少 |
| 北向资金结构 | 沪股通XX亿/深股通XX亿 | 偏好大盘/成长 |
| 行业资金流向TOP3 | 流入：XX/XX/XX | 资金明确偏好方向 |
| 行业资金流向BOTTOM3 | 流出：XX/XX/XX | 资金明确规避方向 |

---

## 七、机构及大V情绪

### 7.1 全球与中国主流机构观点
必须分为海外机构与中国机构。机构观点必须增加：来源；观点方向；与市场数据是否一致；是否存在共识；是否存在分歧；信号灯。

### 7.2 网络大V情绪
说明KOL观点（外网、抖音、股吧、雪球、微博、财联社等），只作为传播情绪，不作为核心结论依据。可给情绪拥挤度信号灯，但不能给事实灯。

---

## 八、多指标共振评分

根据多指标打分确定今天的判断，进攻/中性/防御，以及进攻的方向和主线。

| 指标 | 建议权重 | 评分 |
|------|---------|------|
| 确定大方向：进攻/中性/防御 | 30% | |
| 确定投资机会 | 40% | |
| A股资金结构 | 20% | |
| 机构及大V情绪 | 10% | |
| **总计** | **100%** | |

评分依据硬性要求：
每个指标的评分必须基于客观数据，不得使用模糊表述。
错误："外围科技下跌，A股科技承压，所以防御"
正确："费城半导体-7.87%，但科创50昨日+2%、融资余额+59亿、芯片板块近3日资金净流入，科技板块有本土独立定价逻辑，因此该信号权重降低"

---

## 九、说明

### 9.1 映射价值含义

| 映射价值 | 含义 | 使用场景 |
|---------|------|---------|
| 强 | 外围对A股的映射较强 | 外围信号对A股/港股映射价值较高，一般会跟随外围市场变动 |
| 中性 | 外围对A股的映射一般 | 外围信号对A股/港股映射价值不明确 |
| 弱 | 外围对A股的映射较弱 | 外围信号对A股/港股映射价值较低，一般会走独立行情 |
| 无法判断 | 数据缺口/不参与判断 | 数据不完整，不能形成方向性判断 |

### 9.2 信号灯含义

| 信号灯 | 含义 | 使用场景 |
|--------|------|---------|
| 🔴 红色 | 偏正面观察 | 外围信号可能对A股/港股形成正面情绪或支撑 |
| 🟡 黄色 | 中性/扰动/待验证 | 信号不明确，或正负因素并存，需要盘中成交、资金、承接进一步验证 |
| 🟢 绿色 | 偏负面/风险压力 | 外围信号可能对相关方向形成估值压力、情绪压制或风险扰动 |
| ⚪ 白色 | 数据缺口/不参与判断 | 数据不完整，不能形成方向性判断，不参与多指标评分 |

### 9.3 信号灯硬性约束
- 信号灯不是交易指令。红色不代表可以买入，绿色不代表必须卖出，黄色代表需要验证，不得写成明确方向，白色代表数据缺口，不得强行分析。
- 所有信号灯后必须写明"影响市场"和"验证条件"。
- 美股映射信号灯只能表示可能影响，不得表示确定因果。
- 如果美股映射方向与A股/港股自身资金方向冲突，必须降低信号灯强度。
- 如果数据缺失，必须标注"未计入"。
- 如果外围信号和A股/港股自身资金方向相反，必须降低置信度。
- 如果只有外围映射、没有本土市场验证，只能写"观察"。

---

> ⚠️ 以上分析仅供参考，不构成投资建议。市场有风险，投资需谨慎。
"""


def _fetch_global_market_data(
    include_a50: bool = True,
    include_hxc: bool = True,
    include_asia: bool = True,
    include_us_stocks: bool = True,
) -> str:
    """
    通过 yfinance 拉取全球市场隔夜数据（含亚太市场及美股个股ETF）。

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

            # 检查数据新鲜度
            _idx_val = h.index[-1]
            if hasattr(_idx_val, 'date'):
                _data_date = _idx_val.date()
            else:
                _data_date = _idx_val if hasattr(_idx_val, 'year') else None

            last_trading_day = _get_last_trading_day()
            last_trading_date = last_trading_day.date()
            stale_note = ""
            if _data_date and _data_date != last_trading_date:
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

    # 拉取美股个股及ETF数据（用于第五章投资机会映射）
    us_stock_lines = []
    if include_us_stocks:
        for row in _US_STOCK_ETF_SYMBOLS:
            code, yf_sym, group, cn_name, region, a_mappings = row  # type: ignore[misc]
            try:
                import yfinance as yf
                t = yf.Ticker(yf_sym)
                h = t.history(period="5d")
                if h.empty:
                    us_stock_lines.append(f"- {cn_name}({code}): ⚪ 数据缺失 [映射A股: {a_mappings}]")
                    continue
                cur = float(h.iloc[-1]["Close"])
                prev_day = float(h.iloc[-2]["Close"]) if len(h) > 1 else cur
                chg_pct = ((cur - prev_day) / prev_day * 100) if prev_day else 0
                trend_5d = ((cur - float(h.iloc[0]["Close"])) / float(h.iloc[0]["Close"]) * 100) if len(h) > 1 else 0
                direction = "↑" if chg_pct > 0 else "↓" if chg_pct < 0 else "-"
                trend_dir = "↑" if trend_5d > 0 else "↓" if trend_5d < 0 else "-"
                us_stock_lines.append(
                    f"- {cn_name}({code}): {cur:.2f} | 日变动: {direction}{abs(chg_pct):.2f}% "
                    f"| 5日趋势: {trend_dir}{abs(trend_5d):.2f}% [映射A股: {a_mappings}]"
                )
            except Exception as e:
                logger.debug("yfinance 拉取美股 %s 失败: %s", code, e)
                us_stock_lines.append(f"- {cn_name}({code}): ⚪ 数据拉取失败 [映射A股: {a_mappings}]")

    if not data_lines and not holiday_lines and not us_stock_lines:
        return ""

    # 组装输出
    result_parts = []
    result_parts.append("## 市场交易状态概要\n")
    if holiday_lines:
        result_parts.append("\n".join(holiday_lines))
    else:
        result_parts.append("✅ 所有可检测市场数据正常获取，未检测到主要市场休市。")

    if data_lines:
        result_parts.append("\n\n## 隔夜全球及亚太市场数据\n")
        result_parts.append("\n".join(data_lines))

    if us_stock_lines:
        result_parts.append("\n\n## 美股个股及ETF数据（用于投资机会映射）\n")
        result_parts.append("\n".join(us_stock_lines))

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

    # 2. 拉取全球市场数据（含美股个股ETF）
    logger.info("[盘前分析] 正在拉取全球市场数据（含美股个股ETF）...")
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

{_MOTIVATION_CLASSIFICATION}

---

请基于以上隔夜全球市场数据（含亚太及美股个股ETF），生成 {today_str} 的完整盘前分析报告。

重要要求：
1. 必须严格按九个章节（一～九）完整输出，每个章节都不能跳过。
2. 每个章节都要详尽展开、深入分析，目标输出 5000-8000 字。
3. 方向判断使用"进攻/中性偏进攻/中性/中性偏防御/防御"体系，必须基于数据而非机构观点。
4. 信号灯遵循中国股市红涨绿跌惯例：🔴红=正面、🟢绿=负面、🟡黄=待验证、⚪白=数据缺失。
5. 第四章和第五章的表格必须填充数据，涨跌动因参考上面的分类表。
6. 板块映射部分必须给出具体的A股对应标的名称。
7. 多指标共振评分必须引用具体客观数据，禁止模糊表述。"""

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
        result = analyzer.generate_text(full_prompt, max_tokens=16384, temperature=0.7)
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
