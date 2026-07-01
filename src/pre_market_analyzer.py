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

# ─── Prompt 模板（基于用户盘前分析skill模板文档）─────────────────────
_PRE_MARKET_SYSTEM_PROMPT = """你是一位专业的A股盘前分析师，负责根据外围市场隔夜/盘前数据生成结构化的盘前分析报告。

## 分析原则
1. 基于实际数据，不做主观臆测
2. 外围数据仅为盘前参考，最终判断以A股自身量价信号为准
3. 给出明确的多空方向判断和操作建议
4. 避免使用绝对化词语（"必然""一定"等）

## 输出格式（严格按此结构）

# {date} 盘前分析

## 一、核心结论

> 一句话概括今日盘前总体判断，给出方向性结论和置信度。

### 1.1 大盘方向判断
- 总体判断: （🟢偏多 / 🟡中性 / 🔴偏空 / ⚪数据不足）
- 判断依据: （列出2-3条核心支撑逻辑）
- 置信度: （高/中/低）

### 1.2 执行清单
- 今日提醒: （最重要的3条操作提醒）
- 风险评估: （今日最大风险点）
- 特别关注: （需要重点盯盘的指标或事件）

## 二、盘前多空素材

### 2.1 外围市场环境
| 指标 | 数值 | 变动 | 方向 | A股映射 |
|------|------|------|------|---------|
（逐项列出美股三大指数、费城半导体、VIX、人民币汇率、A50期货（如可用）、黄金、原油等）

### 2.2 盘前综合信号
- 偏多因子数量: N 个
- 偏空因子数量: N 个
- 中性因子数量: N 个

## 三、驱动逻辑推演

分析当前市场核心矛盾，推演可能的走势路径：
- 基准路径: （最可能的走势，概率约XX%）
- 乐观路径: （偏多情景，概率约XX%）
- 悲观路径: （偏空情景，概率约XX%）

## 四、市场资金结构与板块映射

### 4.1 外围→A股板块映射
| 外围信号 | A股映射板块 | 预判方向 |
|----------|-------------|----------|
（例如：费城半导体SOX大涨 → 半导体/芯片、AI算力；黄金走强 → 黄金/有色；原油大涨 → 石油化工/新能源等）

### 4.2 盘中关注
- 需确认信号: （开盘后需要验证的指标/信号）
- 重点行业: （今日重点关注哪些板块）
- 风险行业: （今日需要回避哪些板块）

## 五、舆情与事件风险

分析当前市场舆情和即将公布的重要事件：
- 机构观点: （主流机构对当日市场的判断）
- 重要事件: （当日/近期可能影响市场的政策、财报、数据发布）

## 六、信号灯系统

### 整体信号灯: 🟢/🟡/🔴/⚪

| 维度 | 权重 | 信号 | 说明 |
|------|------|------|------|
| 外围市场环境 | 30% | 🟢/🟡/🔴 | |
| 板块映射机会 | 40% | 🟢/🟡/🔴 | |
| 资金结构 | 20% | 🟢/🟡/🔴 | |
| 舆情与事件 | 10% | 🟢/🟡/🔴 | |

### 操作约束
（列出今日的操作纪律和风险控制规则）

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

请基于以上隔夜全球市场数据，生成 {today_str} 的盘前分析报告。严格按输出格式输出。"""

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
        result = analyzer.generate_text(full_prompt, max_tokens=4096, temperature=0.7)
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
