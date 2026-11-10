"""战法清单：把盘中临时拍脑袋的决策，提前到收盘后冷静的时候做。

三条立场，决定了这个模块不是一堆抄来的口诀：

1. **战法是 checklist，不是信号机。** 本项目没有对它们做回测验证，胜率
   未知——所以页面上不出现任何胜率数字。战法的价值在于：把「什么时候进」
   和更重要的「什么时候承认错了」写成白纸黑字，盘中照着核对。
2. **每个战法的观察面都要能用项目里的数据核对。** 量比、四象限、外围情绪
   都是现成的分析输出；核对不了的（盘口大单、消息面）明确标注为外部变量，
   不脑补。
3. **环境适配规则透明。** 「今日顺 / 慎 / 避」由昨日量价四象限与外围情绪
   两个客观状态推出，规则写在本模块里可读可测；环境数据缺一块就少一块
   标注，战法本体照给——缺数据不等于战法失效。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

__all__ = ["PLAYS", "analyze"]

_DISCLAIMER = (
    "这些是看盘 checklist，不是信号机：本项目没有对它们做回测验证，胜率未知。"
    "战法的价值是把盘中临时决策提前到收盘后冷静时做，并把「什么时候承认错了」写在前面。"
    "观察面里能用页面数据核对的都标了出处，核对不了的（盘口大单、消息面）是外部变量——别脑补。"
)


@dataclass(frozen=True)
class Play:
    key: str
    name: str
    scope: str  # 日内 / 开盘 / 波段 / 布局 / 通用 / 板块
    one_liner: str
    watch: tuple[str, ...]  # 观察面：看什么、为什么
    enter: tuple[str, ...]  # 成立机制
    exit: tuple[str, ...]  # 失败与退出机制
    caveats: tuple[str, ...]  # 注意事项


PLAYS: tuple[Play, ...] = (
    Play(
        key="first-red",
        name="一红定江山",
        scope="日内",
        one_liner="开盘第一根小时 K 的红与量，决定今天做多还是看戏",
        watch=(
            "昨日量价（量能页四象限）：昨日放量上涨还是缩量下跌，决定今天的剧本基调——弱势里的一红多是反抽",
            "隔夜外围（外围页情绪分）：美股与恒指的方向决定开盘高低开的成色",
            "集合竞价：高开幅度与竞价量——高开超 3% 警惕出货型高开",
            "首小时 K 线颜色与量比：红 K 且量比 ≥1 气势确立；黑 K 量增则空头占优",
            "分时均价线：首小时回踩均价线不破，是日内多头守住的证据",
        ),
        enter=(
            "首小时红 K 且量比 ≥1：日内偏多，回踩均价线或开盘价不破是低吸点",
            "当日收盘仍为红 K：次日开盘惯性可参考，但只作参考、不作依据",
        ),
        exit=(
            "冲高后跌破开盘价：假红，当日转空头思维，不再低吸",
            "跌破昨日收盘价：红 K 气势失效，按弱势处理",
            "尾盘 30 分钟翻黑：次日低开概率大，收盘前把日内仓位收掉",
        ),
        caveats=(
            "只在偏强市有效：连跌市里的「一红」多数是反抽，反弹不追",
            "这是大盘战法，个股要打折：强势个股可以逆势，弱势股不跟",
            "红而无量等于没红：缩量红 K 尾盘变盘风险大，不视为气势确立",
            "时间窗只有 10:00 前：之后的 K 线颜色不再是「定江山」，只是普通走势",
            "它给的是今天的天气，不是仓位决策：别用一根 K 线决定加仓",
        ),
    ),
    Play(
        key="pullback-ma20",
        name="回踩 MA20 不破",
        scope="波段",
        one_liner="趋势没坏的前提下回踩生命线低吸——破线与不破线是两件事",
        watch=(
            "MA20 走向（指标页）：走平或向上才有「回踩」资格，下行的 MA20 是压力不是支撑",
            "回踩方式：缩量阴跌到线是健康；放量砸到线是有人跑，性质完全不同",
            "回踩后 1–3 天的收复速度：站稳收复，趋势延续",
            "所属板块量比（量能页板块层）：板块还有量，个股回踩才有资金接",
        ),
        enter=(
            "缩量回踩 MA20（±2%）不破，且当日收红或十字星：低吸仓位",
            "站回 MA20 且量比 ≥1.2：确认回升，可加回",
        ),
        exit=(
            "放量跌破 MA20 且 2 天收不回：趋势破坏，先出后想",
            "反抽 MA20 无量：这是回抽确认下跌，清掉剩余",
        ),
        caveats=(
            "下行 MA20 的「支撑」是错觉：均线只有向上或走平时才谈支撑",
            "一只票历史上破线次数越多，这条线越不可信",
            "最难受的是破线后的第二天：多数大跌发生在破线次日，别用「明天会回来」赌",
        ),
    ),
    Play(
        key="breakout-20d-high",
        name="放量突破 20 日高",
        scope="波段",
        one_liner="突破新高本身不是买点，突破后的回踩不破新高才是",
        watch=(
            "突破当日量比：≥1.5 的突破才有跟随价值，缩量新高要打折",
            "突破后 2–3 天：站稳新高之上（缩量回踩不破）才是真突破",
            "大盘环境（量能页）：大盘放量下跌日的个股突破，跟随盘不足",
            "前高形态：平台越久突破意义越大；刚创新低就反弹出的「高点」不算前高",
        ),
        enter=(
            "突破日收盘确认（收在前高之上）：先建底仓",
            "回踩前高不破且缩量：加仓点；再次放量上攻：顺势加",
        ),
        exit=(
            "收盘跌回前高下方且次日不收回：假突破，止损不等 5%",
            "突破后 3 天不涨反跌：跟随盘不足，先撤",
        ),
        caveats=(
            "大盘暴跌日的假突破最多：突破要看大盘量能，不看个股孤勇",
            "突破买点本质是追高：仓位必须小，止损必须硬",
            "连续三次假突破后，第四次突破往往最真——但也最需要记住前三次的教训",
        ),
    ),
    Play(
        key="ground-volume",
        name="地量地价",
        scope="布局",
        one_liner="地量不是买点，是「开始盯」的信号——右侧确认之前只看不买",
        watch=(
            "量比 <0.5（20 日均量，量能页）：成交萎缩到没人交易",
            "地量之后的第一根放量阳线：右侧确认——方向比量本身更重要",
            "地量位置：深跌后的地量与横盘中的地量含义不同，后者多为流动性枯竭",
            "波动收敛：地量 + 窄幅横盘 = 变盘前夜，等方向而不是猜方向",
        ),
        enter=(
            "地量后放量阳线站上 MA20：第一笔试探仓（小）",
            "放量后回踩不破地量日低点：确认仓",
        ),
        exit=(
            "地量之后继续缩量阴跌：还在寻底，撤出观察",
            "试探仓后跌破地量日低点：止损无条件",
        ),
        caveats=(
            "地量之后还有地量：没人交易可以持续很久，左侧买点不存在",
            "「地量地价」的前提是基本面没坏：业绩暴雷前的地量是出完货，不是见底",
            "板块整体地量才有效：单票地量常是流动性问题，不是估值问题",
        ),
    ),
    Play(
        key="volume-discipline",
        name="量价四象限纪律",
        scope="通用",
        one_liner="价格可以说谎，量很难——放量上涨跟随，缩量上涨不追，放量下跌不抄",
        watch=(
            "当日量比（相对 20 日均量）：1.2 以上算放量，0.8 以下算缩量，中间是平常的一天",
            "量价组合四象限（量能页大盘卡）：每个象限的含义不同，别把放量一律当看涨",
            "连续放量天数：连续 3 天以上放量后，量能峰值常是情绪峰值",
        ),
        enter=(
            "放量上涨且站上 MA20：跟随资金在，可持有或顺势",
            "缩量回踩（回调量比 <0.8）：健康的回调，持有不动",
        ),
        exit=(
            "放量下跌：别在量能峰值处抄底，等缩量后的止跌",
            "缩量上涨持续 2 天以上：没人跟，减仓而不加仓",
        ),
        caveats=(
            "放量滞涨（量大涨幅小）是派发信号：比放量下跌更隐蔽，更值得警惕",
            "阈值是经验值不是铁律：不同市值与板块的正常量比不同，先看自己的基准",
            "量比给的是状态描述，不是方向预测：它告诉你在哪一象限，不告诉你明天",
        ),
    ),
    Play(
        key="sector-leader",
        name="强势板块回调买龙头",
        scope="板块",
        one_liner="普涨时人人是龙头，回调时才知道谁是——超额是试金石",
        watch=(
            "板块量比（量能页板块层）：钱还在不在板块里——板块量比 <1 时整个逻辑失效",
            "分化度（板块页）：分化大时选股比选板块重要，先看个股相对板块的超额",
            "龙头特征：回调时跌得比板块少（超额为正），反弹时率先收复",
            "第二梯队补涨股：涨幅大波动更大，那是「别人不要的」，不是便宜的龙头",
        ),
        enter=(
            "板块量比 ≥1.2 且龙头缩量回调 3–5%：低吸",
            "龙头率先放量创新高：地位确认，跟随",
        ),
        exit=(
            "板块量比 <0.8（钱走了）：龙头也扛不住，减仓",
            "龙头回调时跌得比板块多：地位动摇——换标的，而不是摊薄",
        ),
        caveats=(
            "龙头会换：上一轮的龙头不一定是这一轮的，只认本轮的超额",
            "普涨日的「龙头」是伪命题：分化度低时谁都涨，回调才见真章",
            "追补涨股的逻辑通常是「它还没涨」——这恰恰是它最弱的理由",
        ),
    ),
    Play(
        key="tail-30min",
        name="尾盘 30 分钟定势",
        scope="日内",
        one_liner="主动资金多在尾盘表态——尾盘的方向比全天更诚实",
        watch=(
            "尾盘量能：全天缩量但尾盘放量，是有人赶在收盘前表态",
            "尾盘方向与分时均价线：尾盘拉回均价线上方与尾盘跳水破位，是两种次日剧本",
            "14:57 集合竞价的大单方向：竞价异动比尾盘拉升更真实",
            "全天四象限 + 尾盘修正（量能页）：放量下跌但尾盘拉回 = 有承接；放量上涨但尾盘跳水 = 兑现",
        ),
        enter=(
            "放量上涨且尾盘维持均价线上方：持有过夜合理",
            "尾盘放量拉升站回关键位：可小仓跟次日惯性",
        ),
        exit=(
            "全天强势但尾盘跳水破均价线：兑现盘出现，减仓过夜",
            "尾盘砸出放量长上影：次日低开概率大",
        ),
        caveats=(
            "尾盘拉升也可能是做图：单日尾盘异动不如连续 3 日的尾盘趋势可信",
            "过夜仓位等于承担隔夜风险：外围暴跌的隔夜，尾盘战法救不了你（先看外围页）",
            "别用尾盘异动去追涨停：封单结构比尾盘拉升更早说真话",
        ),
    ),
    Play(
        key="gap-trap",
        name="高开陷阱与真高开",
        scope="开盘",
        one_liner="高开是情绪的显影——量跟着上来是真高开，量缩着是出货预备",
        watch=(
            "高开幅度：<2% 常规，2–4% 情绪，>4% 警惕（尤其没有实质利好时）",
            "高开后的量（10:00 前量比 ≥1.5）：承接真实；缩量高开随时回落",
            "高开后的位置：站住开盘价上方与冲高跌破开盘价，是两种剧本",
            "隔夜外围（外围页）：外围大涨日的高开，情绪来源不是基本面，最易冲高回落",
        ),
        enter=(
            "高开 + 前 30 分钟量比 ≥1.5 + 不破开盘价：强势，可持有观察",
            "高开回踩开盘价不破：回踩确认，低吸点",
        ),
        exit=(
            "高开 30 分钟内跌破开盘价且无量承接：出货型高开，不接",
            "高开 + 天量长上影：兑现日，逢高减",
        ),
        caveats=(
            "高开 >4% 的追入本质是博傻：利润来自更晚的人，不来自逻辑",
            "外围大涨日的高开最危险：先看 10:00 前量比，别开盘即追",
            "盘口大单方向比分时红绿更早说真话——这是外部变量，页面核对不了，自己盯",
        ),
    ),
)

# --- 今日环境适配 ----------------------------------------------------------------
#
# 每条规则输入同一个 env（昨日量价四象限 + 外围情绪），返回（状态，一句话）。
# 状态：顺 = 今天按这个战法盯；慎 = 可用但有明确前提；避 = 今天别用；
# None = 今日无事可做或环境数据不足。规则全部写在这里，可读可测。

Status = str | None


def _first_red(env: dict[str, Any]) -> tuple[Status, str]:
    quadrant = env["quadrant"]
    if quadrant is None:
        return None, "昨日量价数据缺失，无法判定基调——按通用注意事项执行，10:00 前重点盯量比。"
    if quadrant == "放量上涨":
        note = "昨日放量上涨：今天的一红更可信，10:00 前量比 ≥1 就是顺势日。"
    elif quadrant in ("放量下跌", "缩量下跌"):
        return "避", f"昨日{quadrant}：弱势里的一红多是反抽，今天别用多头剧本——先等止跌信号。"
    elif quadrant == "缩量上涨":
        return "慎", "昨日缩量上涨：红而无量等于没红，今天的「一红」要多打一个问号。"
    else:
        return "慎", f"昨日{quadrant}：没有明确的多头基调，一红只按半小时行情对待。"
    if env["tone"] == "风险偏好回升":
        note += " 外围偏暖，高开概率大——警惕「高开出货」版的一红失效，回踩开盘价再确认。"
    return "顺", note


def _pullback_ma20(env: dict[str, Any]) -> tuple[Status, str]:
    quadrant = env["quadrant"]
    if quadrant is None:
        return None, "昨日量价数据缺失，按通用注意事项执行。"
    if quadrant == "放量下跌":
        return "避", "大盘放量下跌日，MA20 支撑会大面积失效——先出后想，别找票低吸。"
    if quadrant == "放量上涨":
        return "顺", "大盘放量：板块资金在动，回踩 MA20 有承接——挑板块量比 ≥1.2 的票。"
    return "慎", f"昨日{quadrant}：回踩可以看，但板块量比必须 ≥1，否则回踩没人接。"


def _breakout(env: dict[str, Any]) -> tuple[Status, str]:
    quadrant = env["quadrant"]
    if quadrant is None:
        return None, "昨日量价数据缺失，按通用注意事项执行。"
    if quadrant == "放量下跌":
        return "避", "大盘放量下跌日的突破假信号最多——跟随盘不存在，今天不做突破。"
    if quadrant == "放量上涨":
        return "顺", "大盘放量：突破有跟随盘——但突破仓位永远小、止损永远硬。"
    return "慎", f"昨日{quadrant}：缩量环境的突破缺跟随盘，突破后 2–3 天站稳才算数。"


def _ground_volume(env: dict[str, Any]) -> tuple[Status, str]:
    level, ratio = env["level"], env["ratio"]
    if level is None:
        return None, "大盘量能数据缺失，无法判断是否处于地量。"
    if level == "地量":
        return "顺", f"大盘正在地量（量比 {ratio}）：开始盯，等右侧放量阳线——不抢跑。"
    if ratio is not None and ratio >= 1.2:
        return None, f"大盘量比 {ratio}，量能充沛：地量战法今日无事可做——它等的是没量的时候。"
    return None, "大盘量能平常：地量战法今日无事可做。"


def _volume_discipline(env: dict[str, Any]) -> tuple[Status, str]:
    if env["quadrant"] is None:
        return None, "昨日量价数据缺失——纪律不依赖环境，但今天没法对照四象限。"
    return (
        "顺",
        f"昨日大盘{env['quadrant']}（量比 {env['ratio']}）：今天先看四象限再动手，纪律优先于观点。",
    )


def _sector_leader(env: dict[str, Any]) -> tuple[Status, str]:
    quadrant = env["quadrant"]
    if quadrant is None:
        return None, "昨日量价数据缺失，按通用注意事项执行。"
    if quadrant == "放量上涨":
        return "顺", "大盘放量：板块逻辑活跃——在板块页看分化度与超额，回调买跌得最少的。"
    if quadrant in ("放量下跌", "缩量下跌"):
        return "避", f"昨日{quadrant}：板块整体承压时没有「强势板块」，今天别接回调。"
    return "慎", f"昨日{quadrant}：只做板块量比 ≥1.2 的板块，量比 <1 的板块回调是没人接的。"


def _tail_30min(env: dict[str, Any]) -> tuple[Status, str]:
    quadrant = env["quadrant"]
    if quadrant is None:
        return None, "昨日量价数据缺失，按通用注意事项执行。"
    if quadrant == "放量上涨":
        return "顺", "昨日放量上涨：今天尾盘的维护动作更有意义——尾盘跳水破均价线就减仓过夜。"
    return "慎", f"昨日{quadrant}：尾盘表态要打折看，连续 3 日尾盘同向才可信。"


def _gap_trap(env: dict[str, Any]) -> tuple[Status, str]:
    tone = env["tone"]
    if tone == "风险偏好回升":
        return (
            "慎",
            "外围偏暖：高开概率大——高开 >4% 的追入是博傻，先看 10:00 前量比是否 ≥1.5。",
        )
    if tone == "风险偏好回落":
        return (
            "慎",
            "外围偏冷：低开概率大——低开反而好观察真承接，破开盘价无量就走。",
        )
    if tone is None:
        return None, "外围快照缺失：先去「外围」页刷新——开盘战法缺了隔夜这一面就只剩一半。"
    return None, "外围分化，无一致方向：高开陷阱战法今日按常规执行。"


_TODAY_RULES = {
    play.key: rule
    for play, rule in zip(
        PLAYS,
        (
            _first_red,
            _pullback_ma20,
            _breakout,
            _ground_volume,
            _volume_discipline,
            _sector_leader,
            _tail_30min,
            _gap_trap,
        ),
        strict=True,
    )
}


def _summary(
    env: dict[str, Any], volume_env: dict[str, Any] | None, tone_env: dict[str, Any] | None
) -> dict[str, Any]:
    parts: list[str] = []
    if volume_env:
        parts.append(
            f"昨日大盘{volume_env['quadrant']}（量比 {volume_env['ratio']}，{volume_env['level']}）"
            + (
                f"，已连续 {volume_env['streak']} 天放量"
                if volume_env.get("streak", 0) >= 2
                else ""
            )
        )
    else:
        parts.append("大盘量能数据缺失（先在采集页补数据）")
    if tone_env:
        parts.append(f"隔夜外围{tone_env['label']}（情绪分 {tone_env['score']}）")
    else:
        parts.append("无外围快照（去外围页刷新）")
    return {
        "quadrant": env["quadrant"],
        "ratio": env["ratio"],
        "level": env["level"],
        "streak": env["streak"],
        "tone_label": env["tone"],
        "tone_score": tone_env.get("score") if tone_env else None,
        "as_of": volume_env.get("as_of") if volume_env else None,
        "summary": "；".join(parts)
        + "。「今日」标注由这两个客观状态推出，规则在 playbook 里可读。",
    }


def analyze(volume_env: dict[str, Any] | None, tone_env: dict[str, Any] | None) -> dict[str, Any]:
    """战法清单 + 今日环境标注。环境缺一块就少一块标注，战法本体照给。"""
    env = {
        "quadrant": volume_env.get("quadrant") if volume_env else None,
        "ratio": volume_env.get("ratio") if volume_env else None,
        "level": volume_env.get("level") if volume_env else None,
        "streak": volume_env.get("streak") if volume_env else None,
        "tone": tone_env.get("label") if tone_env else None,
    }
    plays = []
    for play in PLAYS:
        status, note = _TODAY_RULES[play.key](env)
        plays.append({**asdict(play), "status": status, "today_note": note})
    return {
        "environment": _summary(env, volume_env, tone_env),
        "plays": plays,
        "n_plays": len(plays),
        "disclaimer": _DISCLAIMER,
    }
