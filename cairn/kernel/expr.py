"""The cairn expression language (docs/API.md §5).

A deliberately tiny boolean grammar used by ``when:`` / ``unless:`` / ``until:``
predicates. Parsed with a hand-written tokenizer + recursive-descent parser —
**never** ``eval`` or ``ast.literal_eval``.

    expr    := or
    or      := and ('||' and)*
    and     := cmp ('&&' cmp)*
    cmp     := value (('=='|'!='|'in') value)?
    value   := literal | path | '!' value | '(' expr ')'
    path    := (params|dims|artifacts|gates|run|cycle) ('.' ident)*
    literal := 'string' | number | true | false

Values come from the caller: ``Expr.evaluate(resolver)`` where
``resolver(root, parts) -> Any``. This module never touches the filesystem —
lazily loading artifact JSON is the resolver's job. A path that resolves to
nothing raises :class:`EvalError`, never a silent falsy, so a misspelling can
never quietly disable a step. ``&&`` / ``||`` short-circuit, so a missing path
on the dead side of a settled boolean is not evaluated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from cairn.kernel.errors import CairnError

# The only legal path roots. `artifacts`/`gates`/`run`/`cycle` are runtime;
# `params`/`dims` are known at plan time — the planner reads roots() to decide.
ROOTS: tuple[str, ...] = ("params", "dims", "artifacts", "gates", "run", "cycle")

Resolver = Callable[[str, tuple[str, ...]], Any]


class ExprError(CairnError):
    """A syntax error while parsing an expression. Carries the source position."""

    def __init__(self, message: str, position: int) -> None:
        super().__init__(f"{message} (at position {position})")
        self.position = position


class EvalError(CairnError):
    """A path resolved to nothing at evaluation time — never a silent falsy."""


# --- tokens ------------------------------------------------------------------

@dataclass(frozen=True)
class _Token:
    kind: str
    value: Any
    pos: int


def _tokenize(text: str) -> list[_Token]:
    toks: list[_Token] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == "'":
            j, buf = i + 1, []
            while j < n and text[j] != "'":
                buf.append(text[j])
                j += 1
            if j >= n:
                raise ExprError("unterminated string literal", i)
            toks.append(_Token("STRING", "".join(buf), i))
            i = j + 1
            continue
        if c == "|":
            if i + 1 < n and text[i + 1] == "|":
                toks.append(_Token("OR", "||", i))
                i += 2
                continue
            raise ExprError("expected '||'", i)
        if c == "&":
            if i + 1 < n and text[i + 1] == "&":
                toks.append(_Token("AND", "&&", i))
                i += 2
                continue
            raise ExprError("expected '&&'", i)
        if c == "=":
            if i + 1 < n and text[i + 1] == "=":
                toks.append(_Token("EQ", "==", i))
                i += 2
                continue
            raise ExprError("expected '==' ('=' alone is not a comparison)", i)
        if c == "!":
            if i + 1 < n and text[i + 1] == "=":
                toks.append(_Token("NE", "!=", i))
                i += 2
                continue
            toks.append(_Token("NOT", "!", i))
            i += 1
            continue
        if c == "(":
            toks.append(_Token("LPAREN", "(", i))
            i += 1
            continue
        if c == ")":
            toks.append(_Token("RPAREN", ")", i))
            i += 1
            continue
        if c == ".":
            toks.append(_Token("DOT", ".", i))
            i += 1
            continue
        if c.isdigit() or (c == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i + 1
            while j < n and (text[j].isdigit() or text[j] == "."):
                j += 1
            raw = text[i:j]
            try:
                num: Any = float(raw) if "." in raw else int(raw)
            except ValueError as e:
                raise ExprError(f"invalid number {raw!r}", i) from e
            toks.append(_Token("NUMBER", num, i))
            i = j
            continue
        # identifiers / keywords — kebab-case allowed so artifact names like
        # `art-review` are single path idents (there is no subtraction operator).
        if c.isalpha() or c == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in "_-"):
                j += 1
            toks.append(_Token("WORD", text[i:j], i))
            i = j
            continue
        raise ExprError(f"unexpected character {c!r}", i)
    toks.append(_Token("EOF", None, n))
    return toks


# --- AST ---------------------------------------------------------------------

class _Node:
    def eval(self, resolver: Resolver) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def paths(self, acc: set[tuple[str, tuple[str, ...]]]) -> None:
        """Collect every (root, parts) reference under this node — purely syntactic."""


@dataclass(frozen=True)
class _Lit(_Node):
    value: Any

    def eval(self, resolver: Resolver) -> Any:
        return self.value


@dataclass(frozen=True)
class _Path(_Node):
    root: str
    parts: tuple[str, ...]

    def _dotted(self) -> str:
        return ".".join((self.root, *self.parts))

    def eval(self, resolver: Resolver) -> Any:
        try:
            return resolver(self.root, self.parts)
        except EvalError:
            raise
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise EvalError(f"unknown path: {self._dotted()}") from e

    def paths(self, acc: set[tuple[str, tuple[str, ...]]]) -> None:
        acc.add((self.root, self.parts))


@dataclass(frozen=True)
class _Not(_Node):
    child: _Node

    def eval(self, resolver: Resolver) -> bool:
        return not bool(self.child.eval(resolver))

    def paths(self, acc: set[tuple[str, tuple[str, ...]]]) -> None:
        self.child.paths(acc)


@dataclass(frozen=True)
class _Cmp(_Node):
    op: str  # '==' | '!=' | 'in'
    left: _Node
    right: _Node

    def eval(self, resolver: Resolver) -> bool:
        left = self.left.eval(resolver)
        right = self.right.eval(resolver)
        if self.op == "==":
            return left == right
        if self.op == "!=":
            return left != right
        try:
            return left in right
        except TypeError as e:
            raise EvalError(
                f"'in' needs an iterable right operand, got {type(right).__name__}"
            ) from e

    def paths(self, acc: set[tuple[str, tuple[str, ...]]]) -> None:
        self.left.paths(acc)
        self.right.paths(acc)


@dataclass(frozen=True)
class _BinBool(_Node):
    op: str  # 'and' | 'or'
    left: _Node
    right: _Node

    def eval(self, resolver: Resolver) -> bool:
        if self.op == "or":
            return True if bool(self.left.eval(resolver)) else bool(self.right.eval(resolver))
        # 'and'
        return bool(self.right.eval(resolver)) if bool(self.left.eval(resolver)) else False

    def paths(self, acc: set[tuple[str, tuple[str, ...]]]) -> None:
        self.left.paths(acc)
        self.right.paths(acc)


# --- parser ------------------------------------------------------------------

class _Parser:
    def __init__(self, toks: list[_Token]) -> None:
        self.toks = toks
        self.i = 0

    def _peek(self) -> _Token:
        return self.toks[self.i]

    def _advance(self) -> _Token:
        t = self.toks[self.i]
        self.i += 1
        return t

    def _expect(self, kind: str) -> _Token:
        t = self._peek()
        if t.kind != kind:
            raise ExprError(f"expected {kind}", t.pos)
        return self._advance()

    def parse_or(self) -> _Node:
        node = self.parse_and()
        while self._peek().kind == "OR":
            self._advance()
            node = _BinBool("or", node, self.parse_and())
        return node

    def parse_and(self) -> _Node:
        node = self.parse_cmp()
        while self._peek().kind == "AND":
            self._advance()
            node = _BinBool("and", node, self.parse_cmp())
        return node

    def parse_cmp(self) -> _Node:
        left = self.parse_value()
        t = self._peek()
        op = None
        if t.kind == "EQ":
            op = "=="
        elif t.kind == "NE":
            op = "!="
        elif t.kind == "WORD" and t.value == "in":
            op = "in"
        if op is not None:
            self._advance()
            return _Cmp(op, left, self.parse_value())
        return left

    def parse_value(self) -> _Node:
        t = self._peek()
        if t.kind == "NOT":
            self._advance()
            return _Not(self.parse_value())
        if t.kind == "LPAREN":
            self._advance()
            node = self.parse_or()
            self._expect("RPAREN")
            return node
        if t.kind in ("STRING", "NUMBER"):
            self._advance()
            return _Lit(t.value)
        if t.kind == "WORD":
            if t.value == "true":
                self._advance()
                return _Lit(True)
            if t.value == "false":
                self._advance()
                return _Lit(False)
            if t.value in ROOTS:
                return self.parse_path()
            raise ExprError(
                f"unknown identifier {t.value!r}; a path must start with one of {ROOTS}",
                t.pos,
            )
        raise ExprError("expected a value", t.pos)

    def parse_path(self) -> _Path:
        root = self._advance()  # WORD in ROOTS
        parts: list[str] = []
        while self._peek().kind == "DOT":
            self._advance()
            ident = self._peek()
            if ident.kind != "WORD":
                raise ExprError("expected an identifier after '.'", ident.pos)
            self._advance()
            parts.append(ident.value)
        return _Path(root.value, tuple(parts))


class Expr:
    """A parsed expression. Reusable: no state carried between evaluations."""

    def __init__(self, node: _Node, source: str) -> None:
        self._node = node
        self.source = source

    def evaluate(self, resolver: Resolver) -> Any:
        """Evaluate against ``resolver(root, parts) -> Any``."""
        return self._node.eval(resolver)

    def paths(self) -> frozenset[tuple[str, tuple[str, ...]]]:
        """Every path reference as (root, parts), collected from ALL branches.

        Purely syntactic — the symmetric counterpart to :meth:`roots`. Includes
        the dead side of short-circuited ``&&``/``||``, inside parens/negation,
        and both operands of comparisons, so the planner can leaf-check every
        ``params``/``dims`` path against the schema even where evaluation would
        skip it (misspellings on a lazy branch must not hide until runtime).
        """
        acc: set[tuple[str, tuple[str, ...]]] = set()
        self._node.paths(acc)
        return frozenset(acc)

    def roots(self) -> set[str]:
        """The set of path root names this expression touches (e.g. {'params'})."""
        return {root for root, _ in self.paths()}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Expr({self.source!r})"


def parse(text: str) -> Expr:
    """Parse ``text`` into an :class:`Expr`, or raise :class:`ExprError`."""
    parser = _Parser(_tokenize(text))
    node = parser.parse_or()
    trailing = parser._peek()
    if trailing.kind != "EOF":
        raise ExprError("unexpected trailing input", trailing.pos)
    return Expr(node, text)
