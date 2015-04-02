"""
The :mod:`lexer` module concerns itself with tokenizing Python source.
"""

from __future__ import absolute_import, division, print_function, unicode_literals
from . import source, diagnostic
import re

class Lexer:
    """
    The :class:`Lexer` class extracts tokens and comments from
    a :class:`pyparser.source.Buffer`.

    :class:`Lexer` is an iterable.

    :ivar version: (tuple of (*major*, *minor*))
        the version of Python, determining the grammar used
    :ivar source_buffer: (:class:`pyparser.source.Buffer`)
        the source buffer
    :ivar offset: (integer) character offset into ``source_buffer``
        indicating where the next token will be recognized
    """

    _reserved_2_6 = frozenset([
        u'!=', u'%', u'%=', u'&', u'&=', u'(', u')', u'*', u'**', u'**=', u'*=', u'+', u'+=',
        u',', u'-', u'-=', u'.', u'/', u'//', u'//=', u'/=', u':', u';', u'<', u'<<', u'<<=',
        u'<=', u'<>', u'=', u'==', u'>', u'>=', u'>>', u'>>=', u'@', u'[', u']', u'^', u'^=', u'`',
        u'and', u'as', u'assert', u'break', u'class', u'continue', u'def', u'del', u'elif',
        u'else', u'except', u'exec', u'finally', u'for', u'from', u'global', u'if', u'import',
        u'in', u'is', u'lambda', u'not', u'or', u'pass', u'print', u'raise', u'return', u'try',
        u'while', u'with', u'yield', u'{', u'|', u'|=', u'}', u'~'
    ])

    _reserved_3_0 = _reserved_2_6 \
        - set([u'<>', u'`', u'exec', u'print']) \
        | set([u'->', u'...', u'False', u'None', u'nonlocal', u'True'])

    _reserved_3_1 = _reserved_3_0 \
        | set([u'<>'])

    _reserved = {
        (2, 6): _reserved_2_6,
        (2, 7): _reserved_2_6,
        (3, 0): _reserved_3_0,
        (3, 1): _reserved_3_1,
        (3, 2): _reserved_3_1,
        (3, 3): _reserved_3_1,
        (3, 4): _reserved_3_1,
    }
    """
    A map from a tuple (*major*, *minor*) corresponding to Python version to
    :class:`frozenset`\s of keywords.
    """

    def __init__(self, source_buffer, version):
        self.source_buffer = source_buffer
        self.version = version

        self.offset = 0
        self.comments = []
        self.queue = []
        self.parentheses = []
        self.curly_braces = []
        self.square_braces = []

        try:
            reserved = self._reserved[version]
        except KeyError:
            raise NotImplementedError("pyparser.lexer.Lexer cannot lex Python %s" % str(version))

        # Sort for the regexp to obey longest-match rule.
        re_reserved  = sorted(reserved, reverse=True, key=len)
        re_keywords  = "|".join([kw for kw in re_reserved if kw.isalnum()])
        re_operators = "|".join([re.escape(op) for op in re_reserved if not op.isalnum()])

        # To speed things up on CPython, we use the re module to generate a DFA
        # from our token set and execute it in C. Every result yielded by
        # iterating this regular expression has exactly one non-empty group
        # that would correspond to a e.g. lex scanner branch.
        # The only thing left to Python code is then to select one from this
        # small set of groups, which is much faster than dissecting the strings.
        #
        # A lexer has to obey longest-match rule, but a regular expression does not.
        # Therefore, the cases in it are carefully sorted so that the longest
        # ones come up first. The exception is the identifier case, which would
        # otherwise grab all keywords; it is made to work by making it impossible
        # for the keyword case to match a word prefix, and ordering it before
        # the identifier case.
        self.lex_token = re.compile(u"""
        [ \t\f]* # initial whitespace
        ( # 1
            (\\\\)? # ?2 line continuation
            ([\n]|[\r][\n]|[\r]) # 3 newline
        |   (\#.+) # 4 comment
        |   ( # 5 floating point or complex literal
                (?: [0-9]* \.  [0-9]+
                |   [0-9]+ \.?
                ) [eE] [+-]? [0-9]+
            |   [0-9]* \. [0-9]+
            |   [0-9]+ \.
            ) ([jJ])? # ?6 complex suffix
        |   ([0-9]+) [jJ] # 7 complex literal
        |   (?: # integer literal
                ( [1-9]   [0-9]* )       # 8 dec
            |     0[oO] ( [0-7]+ )       # 9 oct
            |     0[xX] ( [0-9A-Fa-f]+ ) # 10 hex
            |     0[bB] ( [01]+ )        # 11 bin
            |   ( [0-9]   [0-9]* )       # 12 bare oct
            )
            [Ll]?
        |   ([BbUu]?[Rr]?) # ?13 string literal options
            (""\"|"|'''|') # 14 string literal start
        |   ((?:{keywords})\\b|{operators}) # 15 keywords and operators
        |   ([A-Za-z_][A-Za-z0-9_]*) # 16 identifier
        )
        """.format(keywords=re_keywords, operators=re_operators), re.VERBOSE)

    def next(self):
        """
        Returns token at ``offset`` as a tuple (*range*, *token*, *data*)
        and advances ``offset`` to point past the end of the token,
        where:

        - *range* is a :class:`pyparser.source.Range` that includes
          the token but not surrounding whitespace,
        - *token* is a string containing one of Python keywords or operators,
          ``newline``, ``'``, ``'''``, ``"``, ``""\"``,
          ``float``, ``int``, ``complex``, ``ident``, ``indent`` or ``dedent``
        - *data* is the flags as lowercase string if *token* is a quote,
          the numeric value if *token* is ``float``, ``int`` or ``complex``,
          the identifier if *token* is ``ident`` and ``None`` in any other case.
        """
        if len(self.queue) == 0:
            return self._lex()

        return self.queue.pop(0)

    def _lex(self):
        if self.offset == len(self.source_buffer.source):
            raise StopIteration

        # We need separate next and _lex because lexing can sometimes
        # generate several tokens, e.g. INDENT
        match = self.lex_token.match(
            self.source_buffer.source, self.offset)
        if match is None:
            diag = diagnostic.Diagnostic(
                "fatal", u"unexpected {character}",
                {"character": repr(self.source_buffer.source[self.offset]).lstrip(u"u")},
                source.Range(self.source_buffer, self.offset, self.offset + 1))
            raise diagnostic.DiagnosticException(diag)
        self.offset = match.end(0)

        tok_range = source.Range(self.source_buffer, *match.span(1))
        if match.group(3) is not None: # newline
            if len(self.parentheses) + len(self.square_braces) + len(self.curly_braces) > 0:
                # 2.1.6 Implicit line joining
                return self._lex()
            if match.group(2) is not None:
                # 2.1.5. Explicit line joining
                return self._lex()
            return tok_range, "newline", None

        elif match.group(4) is not None: # comment
            self.comments.append((tok_range, match.group(4)))
            return self._lex()

        elif match.group(5) is not None: # floating point or complex literal
            if match.group(6) is None:
                return tok_range, "float", float(match.group(5))
            else:
                return tok_range, "complex", float(match.group(5)) * 1j

        elif match.group(7) is not None: # complex literal
            return tok_range, "complex", int(match.group(7)) * 1j

        elif match.group(8) is not None: # integer literal, dec
            literal = match.group(8)
            self._check_long_literal(tok_range, match.group(1))
            return tok_range, "int", int(literal)

        elif match.group(9) is not None: # integer literal, oct
            literal = match.group(9)
            self._check_long_literal(tok_range, match.group(1))
            return tok_range, "int", int(literal, 8)

        elif match.group(10) is not None: # integer literal, hex
            literal = match.group(10)
            self._check_long_literal(tok_range, match.group(1))
            return tok_range, "int", int(literal, 16)

        elif match.group(11) is not None: # integer literal, bin
            literal = match.group(11)
            self._check_long_literal(tok_range, match.group(1))
            return tok_range, "int", int(literal, 2)

        elif match.group(12) is not None: # integer literal, bare oct
            literal = match.group(12)
            if len(literal) > 1 and self.version >= (3, 0):
                error = diagnostic.Diagnostic(
                    "error", u"in Python 3, decimal literals must not start with a zero", {},
                    source.Range(self.source_buffer, tok_range.begin_pos, tok_range.begin_pos + 1))
                raise diagnostic.DiagnosticException(error)
            return tok_range, "int", int(literal, 8)

        elif match.group(14) is not None: # string literal start
            options = match.group(13).lower()
            return tok_range, match.group(14), options

        elif match.group(15) is not None: # keywords and operators
            self._match_pair_delim(tok_range, match.group(15))
            return tok_range, match.group(15), None

        elif match.group(16) is not None: # identifier
            return tok_range, "ident", match.group(16)

        assert False

    def _check_long_literal(self, range, literal):
        if literal[-1] in 'lL' and self.version >= (3, 0):
            error = diagnostic.Diagnostic(
                "error", u"in Python 3, long integer literals were removed", {},
                source.Range(self.source_buffer, range.end_pos - 1, range.end_pos))
            raise diagnostic.DiagnosticException(error)

    def _match_pair_delim(self, range, kwop):
        if kwop == '(':
            self.parentheses.append(range)
        elif kwop == '[':
            self.square_braces.append(range)
        elif kwop == '{':
            self.curly_braces.append(range)
        elif kwop == ')':
            self._check_innermost_pair_delim(range, '(')
            self.parentheses.pop()
        elif kwop == ']':
            self._check_innermost_pair_delim(range, '[')
            self.square_braces.pop()
        elif kwop == '}':
            self._check_innermost_pair_delim(range, '{')
            self.curly_braces.pop()

    def _check_innermost_pair_delim(self, range, expected):
        ranges = []
        if len(self.parentheses) > 0:
            ranges.append(('(', self.parentheses[-1]))
        if len(self.square_braces) > 0:
            ranges.append(('[', self.square_braces[-1]))
        if len(self.curly_braces) > 0:
            ranges.append(('{', self.curly_braces[-1]))

        ranges.sort(key=lambda k: k[1].begin_pos)
        compl_kind, compl_range = ranges[-1]
        if compl_kind != expected:
            note = diagnostic.Diagnostic(
                "note", u"'{delimiter}' opened here",
                {"delimiter": compl_kind},
                compl_range)
            error = diagnostic.Diagnostic(
                "fatal", u"mismatched '{delimiter}'",
                {"delimiter": range.source()},
                range, notes=[note])
            raise diagnostic.DiagnosticException(error)

    def __iter__(self):
        return self

    def __next__(self):
        return self.next()
