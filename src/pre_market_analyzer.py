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
_GLOBAL_MARKET_SYMBOLS = [
    # (code, yf_symbol, group, chinese_name)
    ("DJI", "^DJI", "美股三大指数", "道琼斯"),
    ("IXIC", "^IXIC", "美股三大指数", "纳斯达克"),
    ("SPX", "^GSPC", "美股三大指数", "标普500"),
    ("RUT", "^RUT", "罗素2000小盘", "罗素2000"),
    ("SOX", "^SOX", "半导体风向标", "费城半导体SOX"),
    ("VIX", "^VIX", "恐慌情绪", "VIX恐慌指数"),
    ("USDCNH", "USDCNH=X", "汇率", "离岸人民币"),
    ("TNX", "^TNX", "美债", "美国10Y国债收益率"),
    ("GC", "GC=F", "大宗商品", "黄金期货"),
    ("CL", "CL=F", "大宗商品", "WTI原油"),
]

# A50 期货在新加坡交易所，yfinance 代码
_A50_SYMBOL = ("A50", "CNY=X", "A股期货", "富时A50期货")
# 中概股指数
_HXC_SYMBOL = ("HXC", "^HXC", "中概股方向", "中概股HXC")

# ─── Prompt 模板（详细版——基于用户盘前分析docx模板）─────────────────────
_PRE_MARKET_SYSTEM_PROMPT = """你是一位经验丰富的A股盘前分析师，你的职责是撰写一份专业、详尽、有深度的盘前分析报告。

## 核心要求
- **详尽展开**：每个维度都要充分论述，不允许一句话带过。目标输出长度在 3000-5000 字。
- **数据驱动**：每一项判断必须引用具体的外围数据，标明数值和方向。
- **逻辑链条完整**：从外围数据 → A 股映射 → 板块机会 → 操作策略，必须有清晰的推导过程。
- **可操作**：最终的结论必须落到具体的操作建议上，让读者知道开盘后该怎么做。
- 避免使用绝对化词语（"必然""一定"等），保持客观冷静。

## 输出格式（严格按此结构，但内容要充分展开）

# {date} 盘前分析

## 一、核心结论

先用一段话（100-200字）概括今日盘前总体判断，清晰说明是偏多/偏空/中性，以及最核心的逻辑是什么。

### 1.1 大盘方向判断
- 总体判断: （🟢偏多 / 🟡中性 / 🔴偏空 / ⚪数据不足）
- 多空力量对比: （详细展开：做多的因素有哪些、做空的因素有哪些，各自权重多大）
- 判断依据: （至少列出 4-5 条有数据支撑的判断依据，每条要解释"为什么"）
- 置信度: （高/中/低，并解释为什么是这个置信度）

### 1.2 执行清单
- 今日核心策略: （用一段话阐明今天的总体应对思路，比如"积极做多"还是"防御为主"还是"观望等待"）
- 操作提醒: （至少 4-5 条具体的操作提醒，每条有场景说明）
- 风险评估: （今日最大的 2-3 个风险点，每个风险触发条件和应对方式）
- 特别关注: （需要重点盯盘的指标或事件，以及看到什么信号后做什么动作）

## 二、盘前多空素材

### 2.1 外围市场环境
逐项绘制表格，列出所有可用的外围指标（美股三大指数、费城半导体、VIX、人民币汇率、A50期货、黄金、原油、美债收益率、罗素2000、中概股等），每项包含数值、变动幅度、方向判断、对 A 股的具体影响分析。

| 指标 | 数值 | 变动 | 方向 | A股映射与影响分析 |
|------|------|------|------|-------------------|

### 2.2 盘前综合信号
- 偏多因子: 逐项列出并简要解释
- 偏空因子: 逐项列出并简要解释
- 中性因子: 逐项列出并简要解释
- 综合评分: （给出多空力量对比的总体判断，例如"多方力量偏强，但需关注XX风险"）

## 三、驱动逻辑推演

这是报告的核心章节。你需要基于外围数据，推演今日 A 股可能的走势路径。每条路径都要有清晰的逻辑链条和触发条件。

- 基准路径: （最可能的走势，概率约XX%）
  - 逻辑链条: （从外围→开盘→盘中→收盘的完整推演）
  - 触发条件: （什么情况下这个路径成立）
  - 关键节点: （开盘、10:30、午盘、尾盘各阶段可能的表现）

- 乐观路径: （偏多情景，概率约XX%）
  - 逻辑链条: （什么因素共振才能走出乐观行情）
  - 触发条件: （需要看到什么盘面信号）
  - 目标空间: （如果走乐观路径，指数可能到什么位置）

- 悲观路径: （偏空情景，概率约XX%）
  - 逻辑链条: （什么因素可能导致悲观走势）
  - 触发条件: （需要警惕什么风险信号）
  - 防守位: （如果走悲观路径，关键支撑在哪）

## 四、市场资金结构与板块映射

### 4.1 外围→A股板块映射
逐项分析外围每一项信号对应的 A 股板块机会或风险，用表格呈现：

| 外围信号 | 信号强度 | A股映射板块 | 逻辑分析 | 预判方向 |
|----------|----------|-------------|----------|----------|

每个映射都要有逻辑分析——为什么这个外围信号会影响这个 A 股板块，传导机制是什么。

### 4.2 资金面预判
- 北向资金预判: （基于外围环境和汇率，预判北向资金今日可能的流向和规模）
- 主力资金动向: （哪些板块可能吸引主力资金，哪些板块可能被减持）
- 市场量能预估: （预计今日两市成交额可能在什么范围，与近期对比如何）

### 4.3 盘中关注
- 需确认信号: （开盘后需要验证的 3-5 个指标/信号，以及确认后的应对）
- 重点行业: （今日重点关注哪些板块，为什么，具体关注哪些细分方向）
- 风险行业: （今日需要回避哪些板块，风险点是什么）

## 五、舆情与事件风险

- 隔夜要闻: （梳理隔夜发生的重要新闻和政策动态，分析对今日 A 股的影响）
- 机构观点汇总: （虽然无法实时搜索，但基于已知信息和市场常识，分析主流机构可能的观点）
- 重要事件日历: （今日/近期可能影响市场的政策节点、经济数据发布、重要会议等）
- 舆情温度: （市场情绪是偏乐观、偏谨慎还是恐慌，有什么信号）

## 六、信号灯系统

### 整体信号灯: 🟢/🟡/🔴/⚪
（用一段话解释为什么给出这个综合信号）

| 维度 | 权重 | 信号 | 详细评分说明 |
|------|------|------|-------------|
| 外围市场环境 | 30% | 🟢/🟡/🔴 | 说明具体评分依据 |
| 板块映射机会 | 40% | 🟢/🟡/🔴 | 说明具体评分依据 |
| 资金面预期 | 20% | 🟢/🟡/🔴 | 说明具体评分依据 |
| 舆情与事件 | 10% | 🟢/🟡/🔴 | 说明具体评分依据 |

### 操作约束
（至少列出 5 条今日操作纪律，每条要结合今天的市场判断来制定，不能是放之四海皆准的泛泛之谈）

---

> ⚠️ 以上分析仅供参考，不构成投资建议。市场有风险，投资需谨慎。
"""


def _fetch_global_market_data(include_a50: bool = True, include_hxc: bool = True) -> str:
    """
    通过 yfinance 拉取全球市场隔夜数据。

    Returns:
        格式化的数据文本块，失败时返回空字符串
    """
    symbols = list(_GLOBAL_MARKET_SYMBOLS)
    if include_a50:
        symbols.append(_A50_SYMBOL)
    if include_hxc:
        symbols.append(_HXC_SYMBOL)

    lines = []
    for code, yf_sym, group, cn_name in symbols:
        try:
            import yfinance as yf
            t = yf.Ticker(yf_sym)
            h = t.history(period="2d")
            if h.empty:
                continue
            cur = float(h.iloc[-1]["Close"])
            prev = float(h.iloc[-2]["Close"]) if len(h) > 1 else cur
            chg_pct = ((cur - prev) / prev * 100) if prev else 0
            direction = "↑" if chg_pct > 0 else "↓" if chg_pct < 0 else "-"
            lines.append(f"- {cn_name}({code}): {cur:.2f} ({direction}{abs(chg_pct):.2f}%) [{group}]")
        except Exception as e:
            logger.debug("yfinance 拉取 %s 失败: %s", code, e)
            continue

    if lines:
        return "## 隔夜全球市场数据\n\n" + "\n".join(lines)
    return ""


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

请基于以上隔夜全球市场数据，生成 {today_str} 的盘前分析报告。重要：每个章节都要详尽展开、深入分析，目标输出 3000-5000 字，不要过于简短。严格按输出格式输出。"""

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
