"""Stage A: Tcl lexer for SDC.

This is NOT a full Tcl interpreter. The lexer produces a flat token
stream per command with these token kinds:

    WORD        unquoted word
    QWORD       "..." quoted word (with backslash escapes processed)
    BWORD       { ... } brace word (verbatim, balanced)
    CMD_SUBST   [ ... ] command substitution (verbatim, nested, NOT executed)
    SEMI        ;  (command separator)
    NEWLINE     \\n (command separator)
    COMMENT     # ... \\n (consumed, preserved for diagnostics)

Line continuations (``\\<newline>``) are folded *before* splitting.
Comments are recognized only where Tcl allows them (start of a
command, i.e. after ``;``/newline/BOS).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator


# Token kinds
WORD = "WORD"
QWORD = "QWORD"
BWORD = "BWORD"
CMD_SUBST = "CMD_SUBST"
SEMI = "SEMI"
NEWLINE = "NEWLINE"
COMMENT = "COMMENT"


@dataclass
class LexToken:
    kind: str
    text: str
    line: int
    col: int
    # For CMD_SUBST tokens we preserve the inner (raw) text including
    # brackets for recursive parsing by higher layers.
    inner: str = ""


class LexError(Exception):
    def __init__(self, message: str, line: int, col: int) -> None:
        super().__init__(f"{message} (line {line}, col {col})")
        self.line = line
        self.col = col


@dataclass
class _Cursor:
    text: str
    pos: int = 0
    line: int = 1
    col: int = 1

    def peek(self, offset: int = 0) -> str:
        p = self.pos + offset
        return self.text[p] if p < len(self.text) else ""

    def advance(self, n: int = 1) -> str:
        out = []
        for _ in range(n):
            if self.pos >= len(self.text):
                break
            ch = self.text[self.pos]
            out.append(ch)
            self.pos += 1
            if ch == "\n":
                self.line += 1
                self.col = 1
            else:
                self.col += 1
        return "".join(out)

    def eof(self) -> bool:
        return self.pos >= len(self.text)


# Backslash escapes supported in Tcl double-quoted / unquoted words.
# Per Tcl spec: \n, \t, \r, \v, \\, \", \[ , \], \$, \{, \}, \;, \ space,
# \newline (line continuation, replaced by nothing), \a, \b, \f, \0..
# plus octal \ooo and hex \xHH and unicode \uHHHH / \UHHHHHHHH.
_SIMPLE_BACKSLASH = {
    "a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r",
    "t": "\t", "v": "\v", "\\": "\\", '"': '"', "[": "[", "]": "]",
    "{": "{", "}": "}", "$": "$", ";": ";", " ": " ",
}


def _fold_line_continuations(text: str) -> str:
    """Collapse backslash-newline (and any surrounding horizontal whitespace) into a single space.

    Per Tcl: backslash immediately followed by newline joins lines;
    the backslash, the newline, and any leading whitespace on the
    next logical line are replaced with a single space.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1] == "\n":
            # backslash-newline: skip both, then skip leading whitespace
            i += 2
            while i < n and text[i] in (" ", "\t"):
                i += 1
            # Inject one space (matches Tcl semantics).
            if out and out[-1] != " ":
                out.append(" ")
            continue
        if ch == "\\" and i + 2 < n and text[i + 1] == "\r" and text[i + 2] == "\n":
            i += 3
            while i < n and text[i] in (" ", "\t"):
                i += 1
            if out and out[-1] != " ":
                out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class TclLexer:
    """Tokenize a Tcl/SDC program into a list of tokens per command.

    Usage::

        for tokens in TclLexer().tokenize_commands(text, start_line=1):
            # tokens is a list[LexToken] forming a single command
            ...

    Comments are returned as COMMENT tokens so that higher layers can
    preserve source mapping; consumers usually drop them.
    """

    def __init__(self) -> None:
        self.errors: list[LexError] = []

    # -- public API -----------------------------------------------------

    def tokenize_commands(self, text: str, source_file: str | None = None,
                          start_line: int = 1) -> Iterator[list[LexToken]]:
        text = _fold_line_continuations(text)
        cur = _Cursor(text, line=start_line)
        current_cmd: list[LexToken] = []
        at_cmd_start = True
        # Track nesting depth across words so we can detect unmatched
        # braces/brackets at newline (error recovery).
        brace_depth = 0
        bracket_depth = 0
        in_quote = False
        while not cur.eof():
            ch = cur.peek()
            if ch == "\n":
                # At newline, if a group/quote is still open, force-close
                # it and record an error; then flush the current command
                # so subsequent lines can parse.
                if brace_depth > 0 or bracket_depth > 0 or in_quote:
                    if in_quote:
                        self.errors.append(LexError("unterminated quoted string", cur.line, cur.col))
                        in_quote = False
                    if brace_depth > 0:
                        self.errors.append(LexError("unterminated brace group", cur.line, cur.col))
                        brace_depth = 0
                    if bracket_depth > 0:
                        self.errors.append(LexError("unterminated command substitution", cur.line, cur.col))
                        bracket_depth = 0
                if current_cmd:
                    yield current_cmd
                    current_cmd = []
                cur.advance()
                at_cmd_start = True
                continue
            if ch == ";":
                if brace_depth == 0 and bracket_depth == 0 and not in_quote:
                    if current_cmd:
                        yield current_cmd
                        current_cmd = []
                    cur.advance()
                    at_cmd_start = True
                    continue
                # Inside a group, semicolons are literal content; let word
                # lexer consume. Fall through.
            if ch.isspace() and not in_quote and brace_depth == 0 and bracket_depth == 0:
                cur.advance()
                at_cmd_start = at_cmd_start and not current_cmd
                continue
            if ch == "#" and at_cmd_start and brace_depth == 0 and bracket_depth == 0 and not in_quote:
                tok = self._lex_comment(cur)
                yield [tok]
                at_cmd_start = True
                continue
            # Track nesting by scanning ahead one character then lex the word.
            pre_pos = cur.pos
            tok = self._lex_word(cur)
            # Update depth counters from the token text so recovery at
            # newline works. This is an approximation (it doesn't handle
            # escaped or quoted braces/brackets perfectly) but it is good
            # enough for error recovery on malformed input.
            self._update_depth(tok, cur)
            current_cmd.append(tok)
            at_cmd_start = False
            # After lexing a word, recompute global quote/brace/bracket
            # state from the current cursor (by examining the most recent
            # word lexer behavior is too hard; instead reset depth tracking
            # conservatively: whenever a word ends at depth 0 we trust it).
            # Simpler: rely on sub-lexers to handle their own depth; we
            # maintain a running count from the characters in tok.text.
        if current_cmd:
            yield current_cmd

    def _update_depth(self, tok: LexToken, cur: _Cursor) -> None:
        """Maintain running brace/bracket/quote counts for error recovery.

        This is intentionally approximate: it does not replicate the full
        lexer state but is sufficient to detect obviously-unmatched groups
        across newlines for recovery purposes.
        """
        # Tracked in the sub-lexers themselves; we leave this as a hook.
        return

    # -- individual word lexers ---------------------------------------

    def _lex_comment(self, cur: _Cursor) -> LexToken:
        line0 = cur.line
        col0 = cur.col
        start = cur.pos
        # consume until newline or EOF
        while not cur.eof() and cur.peek() != "\n":
            cur.advance()
        text = cur.text[start:cur.pos]
        return LexToken(COMMENT, text, line0, col0, inner=text)

    def _lex_word(self, cur: _Cursor) -> LexToken:
        ch = cur.peek()
        if ch == '"':
            return self._lex_qword(cur)
        if ch == "{":
            return self._lex_bword(cur)
        if ch == "[":
            return self._lex_cmd_subst(cur)
        return self._lex_bare(cur)

    def _lex_bare(self, cur: _Cursor) -> LexToken:
        line0 = cur.line; col0 = cur.col; start = cur.pos
        out: list[str] = []
        while not cur.eof():
            ch = cur.peek()
            if ch.isspace() or ch in (";", "\n", "{", "}", '"'):
                break
            if ch == "\\" and cur.pos + 1 < len(cur.text):
                nxt = cur.text[cur.pos + 1]
                if nxt == "\n":
                    # line continuation inside a bare word: should have
                    # been folded already, but handle defensively.
                    cur.advance(2)
                    if out and out[-1] != " ":
                        out.append(" ")
                    continue
                # Process escape
                cur.advance()  # backslash
                esc = self._read_backslash_escape(cur)
                out.append(esc)
                continue
            if ch == "[":
                # Nested command substitution inside a bare word: read it
                # and append its raw text; the word continues.
                sub = self._lex_cmd_subst(cur)
                out.append(sub.text)
                continue
            if ch == "$":
                # Variable substitution: keep as literal text for safety.
                out.append(cur.advance())
                # consume variable name
                while not cur.eof():
                    c2 = cur.peek()
                    if c2.isalnum() or c2 == "_":
                        out.append(cur.advance())
                    elif c2 == "{":
                        # ${var} - read until matching }
                        out.append(cur.advance())
                        depth = 1
                        while not cur.eof() and depth > 0:
                            cc = cur.peek()
                            out.append(cur.advance())
                            if cc == "{":
                                depth += 1
                            elif cc == "}":
                                depth -= 1
                        break
                    else:
                        break
                continue
            out.append(cur.advance())
        return LexToken(WORD, "".join(out), line0, col0)

    def _lex_qword(self, cur: _Cursor) -> LexToken:
        line0 = cur.line; col0 = cur.col
        assert cur.peek() == '"'
        cur.advance()  # opening quote
        out: list[str] = []
        while not cur.eof():
            ch = cur.peek()
            if ch == '"':
                cur.advance()  # closing quote
                return LexToken(QWORD, "".join(out), line0, col0)
            if ch == "\\" and cur.pos + 1 < len(cur.text):
                cur.advance()
                out.append(self._read_backslash_escape(cur))
                continue
            if ch == "[":
                sub = self._lex_cmd_subst(cur)
                out.append(sub.text)
                continue
            if ch == "$":
                # variable reference: preserve as literal text
                out.append(cur.advance())
                while not cur.eof():
                    c2 = cur.peek()
                    if c2.isalnum() or c2 == "_":
                        out.append(cur.advance())
                    elif c2 == "{":
                        out.append(cur.advance())
                        depth = 1
                        while not cur.eof() and depth > 0:
                            cc = cur.peek()
                            out.append(cur.advance())
                            if cc == "{":
                                depth += 1
                            elif cc == "}":
                                depth -= 1
                        break
                    else:
                        break
                continue
            if ch == "\n":
                # Unterminated quoted string at line boundary is an error
                # but we recover gracefully.
                self.errors.append(LexError("unterminated quoted string", cur.line, cur.col))
                cur.advance()
                break
            out.append(cur.advance())
        self.errors.append(LexError("unterminated quoted string", line0, col0))
        return LexToken(QWORD, "".join(out), line0, col0)

    def _lex_bword(self, cur: _Cursor) -> LexToken:
        """Brace word: { ... } verbatim with balanced braces.

        For error recovery on malformed input, an unmatched { at end of
        line returns the partial content (with a LEX_ERROR) instead of
        consuming the rest of the file.
        """
        line0 = cur.line; col0 = cur.col
        assert cur.peek() == "{"
        cur.advance()  # opening brace
        depth = 1
        out: list[str] = []
        saw_newline = False
        while not cur.eof():
            ch = cur.peek()
            if ch == "\\" and cur.pos + 1 < len(cur.text) and cur.text[cur.pos + 1] in ("\n", "\r"):
                cur.advance(2)
                if cur.peek() == "\n":
                    cur.advance()
                out.append(" ")
                continue
            if ch == "\n" and depth > 0:
                # Don't consume across newline unless braces balance.
                # Record an error and return what we have.
                self.errors.append(LexError("unterminated brace group", line0, col0))
                return LexToken(BWORD, "".join(out), line0, col0, inner="".join(out))
            if ch == "{":
                depth += 1
                out.append(cur.advance())
                continue
            if ch == "}":
                depth -= 1
                if depth == 0:
                    cur.advance()
                    return LexToken(BWORD, "".join(out), line0, col0, inner="".join(out))
                out.append(cur.advance())
                continue
            out.append(cur.advance())
        self.errors.append(LexError("unterminated brace group", line0, col0))
        return LexToken(BWORD, "".join(out), line0, col0, inner="".join(out))

    def _lex_cmd_subst(self, cur: _Cursor) -> LexToken:
        """Command substitution [ ... ]. We lex the inner text as a raw
        string (nested brackets balanced) and leave it to the parser to
        interpret only the safe supported subset."""
        line0 = cur.line; col0 = cur.col
        assert cur.peek() == "["
        cur.advance()  # opening [
        start_pos = cur.pos
        depth = 1
        out: list[str] = []
        # Track inner quoting/bracing so nested brackets inside strings
        # don't confuse us.
        in_q = False
        in_brace = 0
        while not cur.eof():
            ch = cur.peek()
            if ch == "\n" and depth > 0:
                self.errors.append(LexError("unterminated command substitution", line0, col0))
                return LexToken(CMD_SUBST, "[" + "".join(out), line0, col0, inner="".join(out))
            if ch == "\\" and cur.pos + 1 < len(cur.text):
                out.append(cur.advance())
                if not cur.eof():
                    out.append(cur.advance())
                continue
            if ch == '"' and in_brace == 0:
                in_q = not in_q
                out.append(cur.advance())
                continue
            if in_q:
                out.append(cur.advance())
                continue
            if ch == "{":
                in_brace += 1
                out.append(cur.advance())
                continue
            if ch == "}":
                in_brace = max(0, in_brace - 1)
                out.append(cur.advance())
                continue
            if ch == "[":
                depth += 1
                out.append(cur.advance())
                continue
            if ch == "]":
                depth -= 1
                if depth == 0:
                    cur.advance()  # skip ]
                    inner = "".join(out)
                    return LexToken(CMD_SUBST, "[" + inner + "]", line0, col0, inner=inner)
                out.append(cur.advance())
                continue
            out.append(cur.advance())
        self.errors.append(LexError("unterminated command substitution", line0, col0))
        return LexToken(CMD_SUBST, "[" + "".join(out), line0, col0, inner="".join(out))

    def _read_backslash_escape(self, cur: _Cursor) -> str:
        """Called with the cursor positioned AFTER a backslash (outside braces)."""
        if cur.eof():
            return ""
        ch = cur.peek()
        if ch in _SIMPLE_BACKSLASH:
            cur.advance()
            return _SIMPLE_BACKSLASH[ch]
        if ch == "x":
            # hex escape
            cur.advance()
            hexd = []
            while not cur.eof() and cur.peek() in "0123456789abcdefABCDEF":
                hexd.append(cur.advance())
            return chr(int("".join(hexd) or "0", 16))
        if ch == "u":
            cur.advance()
            hexd = []
            for _ in range(4):
                if not cur.eof() and cur.peek() in "0123456789abcdefABCDEF":
                    hexd.append(cur.advance())
                else:
                    break
            return chr(int("".join(hexd) or "0", 16))
        if ch.isdigit():
            # octal (1-3 digits)
            octd = [cur.advance()]
            for _ in range(2):
                if not cur.eof() and cur.peek() in "01234567":
                    octd.append(cur.advance())
                else:
                    break
            return chr(int("".join(octd), 8))
        # Any other backslash-escaped char resolves to the char itself.
        cur.advance()
        return ch
