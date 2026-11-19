"""Incremental sentence chunker for streamed model text (feeds the TTS queue).

``feed()`` takes text deltas and returns the sentences completed so far;
``flush()`` returns whatever is left. Sentences end at ``. ! ? ؟ ،`` or a
newline. A ``.`` is only a boundary when followed by whitespace, so decimals
("3.5") and dotted names survive; a ``.`` at the very end of the buffer waits for
the next character (or ``flush``). Fenced code blocks (```) are never emitted,
because they are not worth speaking.
"""
from typing import List

_TERMINATORS = frozenset(".!?؟،")
_TRAILING_CLOSERS = frozenset("\"'”’)]»")
_FENCE = "```"


class SentenceChunker:
    def __init__(self) -> None:
        self._buf = ""
        self._in_code = False

    def feed(self, text: str) -> List[str]:
        if not text:
            return []
        self._buf += text
        return self._drain(final=False)

    def flush(self) -> List[str]:
        out = self._drain(final=True)
        rest, self._buf = self._buf.strip(), ""
        if rest:
            out.extend(self._emit(rest))
        return out

    # -- internals ----------------------------------------------------
    def _drain(self, final: bool) -> List[str]:
        out: List[str] = []
        while True:
            end = self._find_boundary(final)
            if end is None:
                return out
            sentence, self._buf = self._buf[:end], self._buf[end:]
            out.extend(self._emit(sentence.strip()))

    def _emit(self, sentence: str) -> List[str]:
        """Drop empty/punctuation-only text and anything inside or opening a code fence."""
        fences = sentence.count(_FENCE)
        was_in_code = self._in_code
        if fences % 2:
            self._in_code = not self._in_code
        if was_in_code or fences:
            return []
        if not any(ch.isalnum() for ch in sentence):
            return []
        return [sentence]

    def _find_boundary(self, final: bool):
        """Index just past the first complete sentence in the buffer, or None."""
        buf = self._buf
        n = len(buf)
        i = 0
        while i < n:
            ch = buf[i]
            if ch == "\n":
                return i + 1
            if ch not in _TERMINATORS:
                i += 1
                continue
            j = i + 1
            while j < n and (buf[j] in _TERMINATORS or buf[j] in _TRAILING_CLOSERS):
                j += 1
            only_dots = all(c == "." or c in _TRAILING_CLOSERS for c in buf[i:j])
            if j >= n:
                # A trailing "." may be a decimal point ("3." + "5"): wait for more text.
                if only_dots and not final:
                    return None
                return n
            if only_dots and not buf[j].isspace():
                i = j  # "3.5", "example.com": not a sentence end
                continue
            return j
        return None
