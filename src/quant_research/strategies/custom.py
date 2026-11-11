"""自定义策略：用户在界面写条件表达式，回测引擎跑「本样本轮廓」。

DSL（一个刻意收窄的子集，不是让用户写 Python）：

    进场/出场条件 = 比较的组合；比较 = 指标 运算符 (数字 | 另一指标)
    组合用 AND / OR（不区分大小写）与括号，AND 优先于 OR
    示例：close > ma20 AND vol_ratio > 1.5
          close > high20 OR (chg > 5 AND vol_ratio > 2)

为什么不用 Python eval：用户输入进 eval 是注入面，而且语法错误只给
一行 traceback。这里 tokenize + 递归下降解析，每类错误（未知指标、
悬挂运算符、括号不配对）逐条带字符位置报告——「初步检测语法正确性」
的意思是先让用户低成本知道写错在哪，而不是把异常原样抛到页面上。

指标口径（诚实优先，逐条写清含不含当日）：
- close    收盘价
- ma20/ma60 均线（含当日收盘）
- high20/low20 前 20 日最高/最低（shift(1) 不含当日——「破位」判定的
  前高前低不能包含当日，否则今日新高永远不算突破）
- vol_ratio 量比 = 当日成交额 / 前 20 日均额（不含当日，与量能页口径
  一致）；面板没有成交额时**如实报错**，不静默放行量条件
- chg      当日涨跌幅（%，2.5 表示涨 2.5%）

窗口不足（如上市不满 20 日）的指标值为 NaN → 比较结果 False——
「不知道」不是「不满足」的漂亮说法，但对信号矩阵它只能是不触发；
这个语义在 spec 的 note 里如实说明。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

from quant_research.strategies.base import register
from quant_research.strategies.tactics import _hold_state

__all__ = ["METRIC_SPEC", "custom_signal", "validate_expr"]


# --- 指标定义：口径说明随定义走，spec 接口原样给出 -------------

METRIC_SPEC: dict[str, str] = {
    "close": "收盘价",
    "ma20": "20 日均线（含当日收盘）",
    "ma60": "60 日均线（含当日收盘）",
    "high20": "前 20 日最高（不含当日——「突破」的前高不能包含当日）",
    "low20": "前 20 日最低（不含当日）",
    "vol_ratio": "量比 = 当日成交额 / 前 20 日均额（不含当日；面板无成交额时本策略拒绝执行）",
    "chg": "当日涨跌幅 %（2.5 表示涨 2.5%）",
}

OPS = (">=", "<=", "==", ">", "<")
LOGIC = ("AND", "OR")


def _metrics_panel(close: pd.DataFrame, amount: pd.DataFrame | None) -> dict[str, pd.DataFrame]:
    """从面板算全部指标；需要成交额而缺失时如实报错。"""
    metrics = {
        "close": close,
        "ma20": close.rolling(20, min_periods=20).mean(),
        "ma60": close.rolling(60, min_periods=60).mean(),
        "high20": close.shift(1).rolling(20, min_periods=20).max(),
        "low20": close.shift(1).rolling(20, min_periods=20).min(),
        "chg": (close / close.shift(1) - 1) * 100,
    }
    if amount is not None:
        metrics["vol_ratio"] = amount / amount.shift(1).rolling(20, min_periods=20).mean()
    return metrics


# --- 词法与语法 --------------------------------------------------


@dataclass
class _Token:
    kind: str  # metric | op | number | logic | lparen | rparen
    text: str
    pos: int  # 字符位置（错误报告用）


@dataclass
class _SyntaxError:
    pos: int
    message: str


_TOKEN_RE = re.compile(
    r"\s*(?:(?P<number>-?\d+(?:\.\d+)?)|(?P<metric>[A-Za-z_][A-Za-z0-9_]*)|(?P<op>>=|<=|==|>|<)|(?P<lparen>\()|(?P<rparen>\)))"
)


def _tokenize(expr: str) -> tuple[list[_Token], list[_SyntaxError]]:
    tokens: list[_Token] = []
    errors: list[_SyntaxError] = []
    pos = 0
    while pos < len(expr):
        match = _TOKEN_RE.match(expr, pos)
        if match is None:
            rest = expr[pos:].strip() or "末尾"
            errors.append(
                _SyntaxError(
                    pos,
                    f"无法识别的字符「{rest[:8]}」——合法成分：指标名、> >= < <= ==、数字、AND/OR、括号",
                )
            )
            break
        pos = match.end()
        if match.group("number"):
            tokens.append(_Token("number", match.group("number"), match.start()))
        elif match.group("metric"):
            word = match.group("metric")
            if word.upper() in LOGIC:
                tokens.append(_Token("logic", word.upper(), match.start()))
            else:
                tokens.append(_Token("metric", word, match.start()))
        elif match.group("op"):
            tokens.append(_Token("op", match.group("op"), match.start()))
        elif match.group("lparen"):
            tokens.append(_Token("lparen", "(", match.start()))
        elif match.group("rparen"):
            tokens.append(_Token("rparen", ")", match.start()))
    return tokens, errors


class _Parser:
    """递归下降：expr → term (OR term)*；term → factor (AND factor)*；
    factor → ( expr ) | metric op number。"""

    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.index = 0

    def _peek(self) -> _Token | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _take(self) -> _Token | None:
        token = self._peek()
        if token is not None:
            self.index += 1
        return token

    def parse(self) -> list[_SyntaxError]:
        errors = self._expr()
        if errors:
            return errors  # 已有准确错误：不再用「多余成分」的猜测干扰
        leftover = self._peek()
        if leftover is None:
            return errors
        # 悬挂的右括号或孤立数字：报告第一处
        if leftover.kind == "rparen":
            errors.append(_SyntaxError(leftover.pos, "多余的右括号「)」——检查括号是否配对"))
        elif leftover.kind in ("op", "logic"):
            errors.append(
                _SyntaxError(leftover.pos, f"条件被「{leftover.text}」截断——比较后面要有数字")
            )
        else:
            errors.append(
                _SyntaxError(leftover.pos, f"「{leftover.text}」多余——两个条件之间要写 AND 或 OR")
            )
        return errors

    def _expr(self) -> list[_SyntaxError]:
        errors = self._term()
        while True:
            token = self._peek()
            if token is None:
                return errors
            if token.kind == "logic" and token.text.upper() == "OR":
                self._take()
                errors += self._term()
                continue
            if token.kind in ("metric", "lparen"):
                errors.append(
                    _SyntaxError(token.pos, f"「{token.text}」前面缺 AND 或 OR——两个条件要连接词")
                )
                return errors
            return errors

    def _term(self) -> list[_SyntaxError]:
        errors = self._factor()
        while True:
            token = self._peek()
            if token is None:
                return errors
            if token.kind == "logic" and token.text.upper() == "AND":
                self._take()
                errors += self._factor()
                continue
            return errors

    def _factor(self) -> list[_SyntaxError]:
        token = self._take()
        if token is None:
            return [
                _SyntaxError(
                    len(self.tokens[-1].text) if self.tokens else 0, "条件不完整——缺少比较"
                )
            ]
        if token.kind == "lparen":
            errors = self._expr()
            closing = self._take()
            if closing is None or closing.kind != "rparen":
                errors.append(_SyntaxError(token.pos, "括号不配对——「(」缺对应的「)」"))
            return errors
        if token.kind != "metric":
            hint = (
                "指标名（close/ma20/ma60/high20/low20/vol_ratio/chg）"
                if token.kind == "number"
                else "条件"
            )
            return [_SyntaxError(token.pos, f"「{token.text}」不是{hint}——比较左边应是指标名")]
        op = self._take()
        if op is None or op.kind != "op":
            return [_SyntaxError(token.pos, f"指标「{token.text}」后缺比较符（> >= < <= ==）")]
        right = self._take()
        if right is None or right.kind not in ("number", "metric"):
            return [_SyntaxError(op.pos, f"比较符「{op.text}」右边缺数字或指标——如 1.5 或 ma20")]
        return []


def validate_expr(expr: str) -> list[_SyntaxError]:
    """语法初步检测：逐条带字符位置。空表达式也如实报。"""
    tokens, errors = _tokenize(expr or "")
    errors = list(errors)
    if not tokens and not errors:
        errors.append(_SyntaxError(0, "表达式是空的——写一个比较，如 close > ma20"))
        return errors
    if any(token.kind != "metric" or token.text.lower() not in METRIC_SPEC for token in tokens):
        # 未知指标在解析前先点名——解析器只能看到「一个词」
        for token in tokens:
            if token.kind == "metric" and token.text.lower() not in METRIC_SPEC:
                errors.append(
                    _SyntaxError(
                        token.pos, f"不认识指标「{token.text}」——可用：{', '.join(METRIC_SPEC)}"
                    )
                )
    if errors:
        return errors
    return _Parser(tokens).parse()


@dataclass
class _Node:
    metric: str = ""
    op: str = ""
    value: float | str = 0.0  # 数字，或另一个指标名（metric-metric 比较）
    logic: str = ""
    left: _Node | None = None
    right: _Node | None = field(default=None)

    def evaluate(self, metrics: dict[str, pd.DataFrame]) -> pd.DataFrame:
        if self.metric:
            other = metrics[self.value] if isinstance(self.value, str) else self.value
            return self._compare(metrics[self.metric], self.op, other)
        left, right = self.left.evaluate(metrics), self.right.evaluate(metrics)
        return (left & right) if self.logic == "AND" else (left | right)

    @staticmethod
    def _compare(metric: pd.DataFrame, op: str, other: pd.DataFrame | float) -> pd.DataFrame:
        # NaN（窗口不足）在比较中为 False——「不知道」不触发
        if op == ">":
            return metric > other
        if op == ">=":
            return metric >= other
        if op == "<":
            return metric < other
        if op == "<=":
            return metric <= other
        return metric == other


def _parse_ast(expr: str) -> _Node:
    """validate_expr 通过后的求值树（内部使用：错误已在 validate 报告）。"""
    tokens, _ = _tokenize(expr)
    parser = _Parser(tokens)
    parser.parse()
    parser.index = 0  # parse 只做校验，求值树从头再走一遍

    def expr_node() -> _Node:
        node = term_node()
        while (
            parser._peek() is not None
            and parser._peek().kind == "logic"
            and parser._peek().text.upper() == "OR"
        ):
            parser._take()
            node = _Node(logic="OR", left=node, right=term_node())
        return node

    def term_node() -> _Node:
        node = factor_node()
        while (
            parser._peek() is not None
            and parser._peek().kind == "logic"
            and parser._peek().text.upper() == "AND"
        ):
            parser._take()
            node = _Node(logic="AND", left=node, right=factor_node())
        return node

    def factor_node() -> _Node:
        token = parser._take()
        if token.kind == "lparen":
            inner = expr_node()
            parser._take()  # rparen（validate 已确认配对）
            return inner
        op = parser._take()
        right = parser._take()
        value = float(right.text) if right.kind == "number" else right.text.lower()
        return _Node(metric=token.text.lower(), op=op.text, value=value)

    return expr_node()


# --- 信号工厂 ----------------------------------------------------


def _condition_frame(expr: str, metrics: dict[str, pd.DataFrame]) -> pd.DataFrame:
    errors = validate_expr(expr)
    if errors:
        raise ValueError(f"表达式语法错误：{errors[0].message}")
    used = {token.text.lower() for token in _tokenize(expr)[0] if token.kind == "metric"}
    unavailable = used - set(metrics)
    if unavailable:
        raise ValueError(
            f"表达式用了 {', '.join(sorted(unavailable))}，但面板没有成交额——vol_ratio 算不出来，"
            "拒绝执行而不是静默放行量条件"
        )
    return _parse_ast(expr).evaluate(metrics)


@register("custom")
def custom_signal(
    close: pd.DataFrame,
    amount: pd.DataFrame | None = None,
    expr: str = "",
    exit_expr: str | None = None,
) -> pd.DataFrame:
    """用户自定义条件策略：进场条件触发持有，出场条件（可选）触发离场。

    出场条件为空时持有到样本结束——如实标注在回测说明里，不假装
    有出场。需要成交额而面板缺失时拒绝执行（vol_ratio 算不出来）。
    """
    if not (expr or "").strip():
        raise ValueError("进场条件不能为空")
    metrics = _metrics_panel(close, amount)
    entry = _condition_frame(expr, metrics)
    leave = _condition_frame(exit_expr, metrics) if (exit_expr or "").strip() else None
    return _hold_state(entry, leave if leave is not None else entry * 0)
