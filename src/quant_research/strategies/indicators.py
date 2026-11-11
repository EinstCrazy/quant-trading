"""自定义指标：用户用算术表达式从内置指标派生自己的指标。

DSL（算术层，叠在内置指标之上）：

    指标 = 数字 / 内置指标 / 已定义的自定义指标 的 + - * / ( ) 组合
    示例：bias  = (close - ma20) / ma20 * 100          # MA20 乖离率 %
          range = (close - low20) / (high20 - low20)   # 20 日箱体位置

设计取舍（诚实优先，与自定义策略 DSL 同一套纪律）：

- 用户输入不进 eval：tokenize + 递归下降，错误带字符位置逐条报告
- 定义只能引用**内置指标或更早定义的自定义指标**——不许前向引用，
  顺序敏感；指标间没有隐藏依赖
- 除以零如实处理：结果 inf 归为 NaN——「算不出来」在比较里不触发，
  与「窗口不足为不知道」同语义；这个口径写进每个指标的展示里
- 名称必须合法标识符且不与内置指标、保留字（AND/OR）冲突——撞名
  会静默改变内置含义，那比拒绝贵得多

存储在 data/custom_indicators.json（名称 → 定义+说明+时间），CRUD
由本模块提供，web 层直接调用。删除指标不做引用追踪——策略表达式
是文本，指标没了回测的语法检测会如实报「不认识指标」。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from quant_research.strategies.custom import METRIC_SPEC

__all__ = [
    "ARITHMETIC_GRAMMAR_NOTE",
    "IndicatorRegistry",
    "evaluate_indicator",
    "validate_indicator_expr",
    "validate_name",
]

ARITHMETIC_GRAMMAR_NOTE = (
    "算术：+ - * / 与括号；比较右边也可以直接写指标名。除以零或窗口不足算出的「不知道」不触发信号。"
)

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")


def validate_name(name: str) -> str | None:
    """名称合法性：合法则返回 None，否则返回原因。"""
    if not (name or "").strip():
        return "指标名不能为空"
    if not _NAME_RE.match(name):
        return "指标名只能是字母/数字/下划线，字母或下划线开头，不超过 32 字符"
    if name.upper() in ("AND", "OR"):
        return "指标名不能用保留字 AND/OR"
    if name.lower() in METRIC_SPEC:
        return f"指标名「{name}」与内置指标撞名——撞名会静默改变内置含义，换个名字"
    return None


# --- 词法与解析（算术层：metric | number | +-*/() ） ---------------


@dataclass
class _Token:
    kind: str  # metric | number | plus | minus | star | slash | lparen | rparen
    text: str
    pos: int


@dataclass
class _Error:
    pos: int
    message: str


_TOKEN_RE = re.compile(
    r"\s*(?:(?P<number>-?\d+(?:\.\d+)?)|(?P<metric>[A-Za-z_][A-Za-z0-9_]*)|(?P<op>[+\-*/()]))"
)


def _tokenize(expr: str) -> tuple[list[_Token], list[_Error]]:
    tokens: list[_Token] = []
    errors: list[_Error] = []
    pos = 0
    while pos < len(expr):
        match = _TOKEN_RE.match(expr, pos)
        if match is None:
            rest = expr[pos:].strip() or "末尾"
            errors.append(
                _Error(pos, f"无法识别的字符「{rest[:8]}」——合法成分：指标名、数字、+ - * /、括号")
            )
            break
        pos = match.end()
        piece = match.group("number") or match.group("metric") or match.group("op")
        kind = (
            "number"
            if match.group("number")
            else (
                "metric"
                if match.group("metric")
                else {
                    "+": "plus",
                    "-": "minus",
                    "*": "star",
                    "/": "slash",
                    "(": "lparen",
                    ")": "rparen",
                }[piece]
            )
        )
        tokens.append(_Token(kind, piece, match.start()))
    return tokens, errors


def validate_indicator_expr(expr: str, available: dict[str, str]) -> list[_Error]:
    """算术表达式语法检测。available = 可引用的指标名 → 说明。

    检查三层：token 识别、未知指标点名、语法结构（缺操作数/括号
    不配对/连续运算符）。
    """
    tokens, errors = _tokenize(expr or "")
    errors = list(errors)
    for token in tokens:
        if token.kind == "metric" and token.text.lower() not in available:
            errors.append(
                _Error(
                    token.pos, f"不认识指标「{token.text}」——可用：{', '.join(sorted(available))}"
                )
            )
    if tokens and not errors:
        errors = _Parser(tokens).parse()
    if not tokens and not errors:
        errors.append(_Error(0, "表达式是空的——用指标和 + - * / 组合，如 (close - ma20) / ma20"))
    return errors


class _Parser:
    """递归下降：expr → term ((+|-) term)*；term → factor ((*|/) factor)*；
    factor → ( expr ) | metric | number。一元负号不支持——用括号或
    0 - x 表达（负号数字已由 tokenizer 识别）。"""

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

    def parse(self) -> list[_Error]:
        errors = self._expr()
        if errors:
            return errors
        leftover = self._peek()
        if leftover is None:
            return []
        if leftover.kind == "rparen":
            return [_Error(leftover.pos, "多余的右括号——检查括号是否配对")]
        if leftover.kind in ("plus", "minus", "star", "slash"):
            return [
                _Error(leftover.pos, f"表达式被「{leftover.text}」截断——运算符右边要接指标或数字")
            ]
        return [_Error(leftover.pos, f"「{leftover.text}」多余——两个成分之间要写运算符")]

    def _expr(self) -> list[_Error]:
        errors = self._term()
        while True:
            token = self._peek()
            if token is None or token.kind not in ("plus", "minus"):
                return errors
            self._take()
            errors += self._term()

    def _term(self) -> list[_Error]:
        errors = self._factor()
        while True:
            token = self._peek()
            if token is None or token.kind not in ("star", "slash"):
                return errors
            self._take()
            errors += self._factor()

    def _factor(self) -> list[_Error]:
        token = self._take()
        if token is None:
            last = self.tokens[-1]
            return [_Error(last.pos, "表达式不完整——运算符右边要接指标或数字")]
        if token.kind == "lparen":
            errors = self._expr()
            closing = self._take()
            if closing is None or closing.kind != "rparen":
                errors.append(_Error(token.pos, "括号不配对——「(」缺对应的「)」"))
            return errors
        if token.kind == "rparen":
            return [_Error(token.pos, "括号不配对——「)」没有对应的「(」")]
        if token.kind in ("plus", "minus", "star", "slash"):
            return [_Error(token.pos, f"「{token.text}」左边缺指标或数字")]
        return []


def _parse_ast(expr: str) -> object:
    """validate 通过后的求值闭包：evaluate(metrics) -> DataFrame。"""
    tokens, _ = _tokenize(expr)
    parser = _Parser(tokens)
    parser.parse()
    parser.index = 0  # 校验只看结构，求值从头再走

    def apply_op(left, op, right):
        if op.kind == "plus":
            return lambda metrics: left(metrics) + right(metrics)
        if op.kind == "minus":
            return lambda metrics: left(metrics) - right(metrics)
        if op.kind == "star":
            return lambda metrics: left(metrics) * right(metrics)
        return lambda metrics: _safe_div(left(metrics), right(metrics))

    def expr_node():
        node = term_node()
        while parser._peek() is not None and parser._peek().kind in ("plus", "minus"):
            op = parser._take()
            node = apply_op(node, op, term_node())
        return node

    def term_node():
        node = factor_node()
        while parser._peek() is not None and parser._peek().kind in ("star", "slash"):
            op = parser._take()
            node = apply_op(node, op, factor_node())
        return node

    def factor_node():
        token = parser._take()
        if token.kind == "lparen":
            inner = expr_node()
            parser._take()  # rparen
            return inner
        if token.kind == "number":
            value = float(token.text)

            def constant(metrics, v=value):
                frame = next(iter(metrics.values()))
                return pd.DataFrame(v, index=frame.index, columns=frame.columns)

            return constant
        name = token.text.lower()

        def lookup(metrics, n=name):
            return metrics[n]

        return lookup

    return expr_node()


def _safe_div(left: pd.DataFrame, right: pd.DataFrame) -> pd.DataFrame:
    """除法：除零的 inf 归为 NaN——「算不出来」不触发，不编 0。"""
    with np.errstate(divide="ignore", invalid="ignore"):
        result = left / right
    return result.replace([np.inf, -np.inf], np.nan)


def evaluate_indicator(expr: str, metrics: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """求值（语法已 validate）。除零 → NaN。"""
    return _parse_ast(expr)(metrics)


# --- 存储与注册表 -------------------------------------------------


class IndicatorRegistry:
    """自定义指标的持久注册表：定义顺序敏感（只能引用更早的定义）。

    更新同名指标视为重定义——重定义后的表达式仍只能引用内置与
    更早定义，不允许自引用或引用后面的指标。
    """

    def __init__(self, path) -> None:
        self.path = path
        self._items: list[dict[str, str]] = []
        if path.exists():
            try:
                self._items = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._items = []  # 坏文件如实当空表——不编数据

    def _available(self, upto: int | None = None) -> dict[str, str]:
        available = dict(METRIC_SPEC)
        for item in self._items if upto is None else self._items[:upto]:
            available[item["name"].lower()] = item["desc"]
        return available

    def available(self) -> dict[str, str]:
        """当前全部可引用指标：内置 + 自定义（含说明）。"""
        return self._available()

    def list(self) -> list[dict[str, str]]:
        return [dict(item) for item in self._items]

    def define(self, name: str, expr: str, desc: str) -> dict[str, Any]:
        """新增或重定义；语法/名称不合法时返回 errors，不写入。"""
        name = (name or "").strip()
        errors = []
        name_error = validate_name(name)
        if name_error:
            errors.append({"pos": 0, "message": name_error})
        expr = (expr or "").strip()
        # 重定义时排除自身：定义里引用旧自己的值没有意义
        others = [item for item in self._items if item["name"].lower() != name.lower()]
        available = {**METRIC_SPEC, **{item["name"].lower(): item["desc"] for item in others}}
        expr_errors = validate_indicator_expr(expr, available)
        errors += [{"pos": e.pos, "message": e.message} for e in expr_errors]
        if errors:
            return {"ok": False, "errors": errors}

        item = {
            "name": name,
            "expr": expr,
            "desc": (desc or "").strip() or "用户自定义指标",
            "defined_at": __import__("datetime").date.today().isoformat(),
        }
        self._items = [x for x in self._items if x["name"].lower() != name.lower()] + [item]
        self._save()
        return {"ok": True, "indicators": self.list()}

    def remove(self, name: str) -> bool:
        before = len(self._items)
        self._items = [item for item in self._items if item["name"].lower() != (name or "").lower()]
        changed = len(self._items) < before
        if changed:
            self._save()
        return changed

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._items, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def evaluate_all(self, metrics: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        """按定义顺序求值：后面的指标可以引用前面的结果。"""
        values = dict(metrics)
        for item in self._items:
            values[item["name"].lower()] = evaluate_indicator(item["expr"], values)
        return values
