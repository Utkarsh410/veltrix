"""
The maths-channel expression language: text → a restricted syntax tree.

Formulas are parsed by this module's own tokenizer and parser. Nothing is ever
handed to Python's `eval`, `exec` or `ast`: the only things a formula can
become are the node types below, and the only functions it can call are the
ones in `functions.FUNCTIONS`.

Grammar (lowest to highest precedence)

    expr     := or
    or       := and ( "or" and )*
    and      := not ( "and" not )*
    not      := "not" not | compare
    compare  := sum ( ("<" | "<=" | ">" | ">=" | "==" | "!=") sum )?
    sum      := product ( ("+" | "-") product )*
    product  := unary ( ("*" | "/") unary )*
    unary    := ("-" | "+") unary | power
    power    := postfix ( "^" unary )?
    postfix  := atom ( ("in" | "as") UNIT )*
    atom     := NUMBER | NAME | "[" channel name "]" | "$" NAME | NAME "(" args ")" | "(" expr ")"
    args     := ( arg ( "," arg )* )?
    arg      := NAME "=" expr | expr

  Speed                    a channel
  [Brake Pres Front]       a channel whose name has spaces
  $slip_limit              a named parameter of the channel set
  Speed in m/s             convert to a unit (dimensions must match)
  x as g                   state the unit of a result without converting it
  if(Throttle > 90, 1, 0)  functions take positional and named arguments
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_LENGTH = 2000         # characters in a formula
MAX_NODES = 400           # nodes in its tree
MAX_DEPTH = 40            # nesting

KEYWORDS = {"and", "or", "not", "in", "as", "true", "false"}


class FormulaError(ValueError):
    """A formula that cannot be parsed or evaluated. `position` is the
    character it was noticed at, when there is one."""

    def __init__(self, message: str, formula: str = "", position: int | None = None):
        self.message, self.formula, self.position = message, formula, position
        text = message
        if formula and position is not None:
            text += f"\n  {formula}\n  {' ' * position}^"
        super().__init__(text)


@dataclass(frozen=True)
class Num:
    value: float
    pos: int = 0


@dataclass(frozen=True)
class Ref:
    name: str
    pos: int = 0


@dataclass(frozen=True)
class Param:
    name: str
    pos: int = 0


@dataclass(frozen=True)
class Unary:
    op: str            # "-" | "not"
    x: object
    pos: int = 0


@dataclass(frozen=True)
class Binary:
    op: str            # + - * / ^ < <= > >= == != and or
    a: object
    b: object
    pos: int = 0


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple
    kwargs: tuple      # ((name, node), ...)
    pos: int = 0


@dataclass(frozen=True)
class Convert:
    x: object
    unit: str
    assert_only: bool  # "as": state the unit; "in": convert to it
    pos: int = 0


_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<bracket>\[[^\[\]]*\])
  | (?P<param>\$[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op><=|>=|==|!=|[-+*/^()<>,=])
  | (?P<sym>[°%µ²·])
""", re.X)
_UNIT = re.compile(r"\s*([A-Za-z°%µ][A-Za-z0-9°%µ/^²·*]*)")


def _tokens(text: str) -> list:
    out, pos = [], 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise FormulaError(f"Unexpected character '{text[pos]}'.", text, pos)
        kind = m.lastgroup
        if kind != "ws":
            out.append((kind, m.group(), pos))
        pos = m.end()
    out.append(("end", "", len(text)))
    return out


class _Parser:
    def __init__(self, text: str):
        self.text, self.tok, self.i, self.nodes, self.depth = text, _tokens(text), 0, 0, 0

    def error(self, message, pos=None):
        return FormulaError(message, self.text, self.tok[self.i][2] if pos is None else pos)

    def peek(self):
        return self.tok[self.i]

    def take(self, value=None):
        kind, text, pos = self.tok[self.i]
        if value is not None and text != value:
            raise self.error(f"Expected '{value}' here" + (f", found '{text}'." if text else ", but the formula ends."))
        self.i += 1
        return kind, text, pos

    def node(self, n):
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise FormulaError(f"The formula is too complex (more than {MAX_NODES} terms). Split it into several channels.",
                               self.text)
        return n

    def nest(self, rule):
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise self.error(f"The formula is nested too deeply (more than {MAX_DEPTH} levels).")
        try:
            return rule()
        finally:
            self.depth -= 1

    def is_word(self, word):
        kind, text, _ = self.peek()
        return kind == "name" and text.lower() == word

    # ── grammar ───────────────────────────────────────────────────────────────

    def expr(self):
        return self.nest(self.or_)

    def or_(self):
        a = self.and_()
        while self.is_word("or"):
            pos = self.take()[2]
            a = self.node(Binary("or", a, self.and_(), pos))
        return a

    def and_(self):
        a = self.not_()
        while self.is_word("and"):
            pos = self.take()[2]
            a = self.node(Binary("and", a, self.not_(), pos))
        return a

    def not_(self):
        if self.is_word("not"):
            pos = self.take()[2]
            return self.node(Unary("not", self.nest(self.not_), pos))
        return self.compare()

    def compare(self):
        a = self.sum()
        kind, text, pos = self.peek()
        if kind == "op" and text in ("<", "<=", ">", ">=", "==", "!="):
            self.take()
            a = self.node(Binary(text, a, self.sum(), pos))
            if self.peek()[1] in ("<", "<=", ">", ">=", "==", "!="):
                raise self.error("Comparisons cannot be chained; join two of them with 'and'.")
        elif kind == "op" and text == "=":
            raise self.error("Use '==' to compare; '=' only names an argument inside a function call.")
        return a

    def sum(self):
        a = self.product()
        while self.peek()[0] == "op" and self.peek()[1] in "+-":
            _, op, pos = self.take()
            a = self.node(Binary(op, a, self.product(), pos))
        return a

    def product(self):
        a = self.unary()
        while self.peek()[0] == "op" and self.peek()[1] in "*/":
            _, op, pos = self.take()
            a = self.node(Binary(op, a, self.unary(), pos))
        return a

    def unary(self):
        kind, text, pos = self.peek()
        if kind == "op" and text in "+-":
            self.take()
            x = self.nest(self.unary)
            return x if text == "+" else self.node(Unary("-", x, pos))
        return self.power()

    def power(self):
        a = self.postfix()
        if self.peek()[1] == "^":
            pos = self.take()[2]
            a = self.node(Binary("^", a, self.nest(self.unary), pos))
        return a

    def postfix(self):
        a = self.atom()
        while self.is_word("in") or self.is_word("as"):
            _, word, pos = self.take()
            m = _UNIT.match(self.text, pos + 2)
            if not m:
                raise self.error(f"'{word}' must be followed by a unit, for example '{word} m/s'.", pos)
            end = m.end()
            while self.tok[self.i][2] < end and self.tok[self.i][0] != "end":      # skip the unit's own tokens
                self.i += 1
            a = self.node(Convert(a, m.group(1), word.lower() == "as", pos))
        return a

    def atom(self):
        kind, text, pos = self.take()
        if kind == "num":
            return self.node(Num(float(text), pos))
        if kind == "bracket":
            name = text[1:-1].strip()
            if not name:
                raise self.error("Empty channel name in [ ].", pos)
            return self.node(Ref(name, pos))
        if kind == "param":
            return self.node(Param(text[1:], pos))
        if kind == "name":
            low = text.lower()
            if low in ("true", "false"):
                return self.node(Num(1.0 if low == "true" else 0.0, pos))
            if low in KEYWORDS:
                raise self.error(f"'{text}' cannot be used as a name.", pos)
            if self.peek()[1] == "(":
                self.take()
                args, kwargs = [], []
                while self.peek()[1] != ")":
                    k, t, p = self.peek()
                    if k == "name" and self.tok[self.i + 1][1] == "=":
                        self.take(), self.take()
                        kwargs.append((t.lower(), self.expr()))
                    elif kwargs:
                        raise self.error("Named arguments must come after the positional ones.")
                    else:
                        args.append(self.expr())
                    if self.peek()[1] == ",":
                        self.take()
                    elif self.peek()[1] != ")":
                        raise self.error("Expected ',' or ')' in the argument list.")
                self.take(")")
                return self.node(Call(low, tuple(args), tuple(kwargs), pos))
            return self.node(Ref(text, pos))
        if kind == "op" and text == "(":
            inner = self.expr()
            self.take(")")
            return inner
        if kind == "end":
            raise self.error("The formula ends where a value was expected.", pos)
        raise self.error(f"Unexpected '{text}'.", pos)


def parse(formula: str):
    """Parse a formula into its syntax tree, or raise FormulaError."""
    if not isinstance(formula, str) or not formula.strip():
        raise FormulaError("The formula is empty.")
    if len(formula) > MAX_LENGTH:
        raise FormulaError(f"The formula is too long ({len(formula)} characters; the limit is {MAX_LENGTH}).")
    p = _Parser(formula)
    tree = p.expr()
    kind, text, pos = p.peek()
    if kind != "end":
        raise p.error(f"Unexpected '{text}' after the end of the expression.", pos)
    return tree


def references(tree) -> tuple[set, set]:
    """(channel names, parameter names) a parsed formula uses."""
    refs, params, stack = set(), set(), [tree]
    while stack:
        n = stack.pop()
        if isinstance(n, Ref):
            refs.add(n.name)
        elif isinstance(n, Param):
            params.add(n.name)
        elif isinstance(n, Unary):
            stack.append(n.x)
        elif isinstance(n, Binary):
            stack += [n.a, n.b]
        elif isinstance(n, Convert):
            stack.append(n.x)
        elif isinstance(n, Call):
            stack += list(n.args) + [v for _, v in n.kwargs]
    return refs, params
