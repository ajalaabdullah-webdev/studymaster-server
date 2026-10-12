"""Convert LaTeX/maths notation into natural spoken English for read-aloud.

The note bank and generated notes contain LaTeX like ``ax^2 + bx + c`` or
``\\frac{a}{b}``. Feeding that straight to TTS reads "a x power 2 plus b x c".
This turns it into "a x squared plus b x plus c" so the voice sounds like a
teacher reading a formula aloud.

It is deliberately pragmatic (covers the WAEC/JAMB-common constructs), not a full
LaTeX parser.
"""

from __future__ import annotations

import re

_GREEK = {
    "alpha": "alpha", "beta": "beta", "gamma": "gamma", "delta": "delta",
    "theta": "theta", "lambda": "lambda", "mu": "mew", "pi": "pi", "rho": "row",
    "sigma": "sigma", "phi": "fie", "omega": "omega", "Delta": "delta",
    "Sigma": "sigma", "Omega": "omega", "pm": "plus or minus",
}
_SYMBOLS = {
    r"\\times": " times ", r"\\div": " divided by ", r"\\cdot": " times ",
    r"\\leq": " less than or equal to ", r"\\geq": " greater than or equal to ",
    r"\\neq": " not equal to ", r"\\approx": " approximately ",
    r"\\rightarrow": " gives ", r"\\to": " to ", r"\\Rightarrow": " implies ",
    r"\\infty": " infinity ", r"\\sum": " the sum of ", r"\\int": " the integral of ",
    r"\\log": " log ", r"\\ln": " natural log ", r"\\sin": " sine ",
    r"\\cos": " cosine ", r"\\tan": " tangent ", r"\\pm": " plus or minus ",
    r"\\%": " percent ", r"\\degree": " degrees ", r"\\circ": " degrees ",
}
_SUP_WORDS = {"2": "squared", "3": "cubed"}


def _superscript(expr: str) -> str:
    expr = expr.strip()
    if expr in _SUP_WORDS:
        return f" {_SUP_WORDS[expr]} "
    return f" to the power {expr} "


def latex_to_speech(text: str) -> str:
    s = text

    # Process inner constructs first so their braces are gone before \frac runs.
    # \sqrt[n]{x} and \sqrt{x}
    s = re.sub(r"\\sqrt\s*\[([^\]]*)\]\s*\{([^{}]*)\}", r" the \1-th root of \2 ", s)
    s = re.sub(r"\\sqrt\s*\{([^{}]*)\}", r" the square root of \1 ", s)
    # superscripts:  x^{2} / x^2
    s = re.sub(r"\^\s*\{([^{}]*)\}", lambda m: _superscript(m.group(1)), s)
    s = re.sub(r"\^\s*([A-Za-z0-9])", lambda m: _superscript(m.group(1)), s)
    # subscripts:  x_{i} / x_1  -> "x sub i"
    s = re.sub(r"_\s*\{([^{}]*)\}", r" sub \1 ", s)
    s = re.sub(r"_\s*([A-Za-z0-9])", r" sub \1 ", s)

    # \frac{a}{b} -> "a over b" (loop to collapse any nesting once inners are gone)
    for _ in range(3):
        new = re.sub(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r" \1 over \2 ", s)
        if new == s:
            break
        s = new

    # symbols
    for pat, word in _SYMBOLS.items():
        s = re.sub(pat, word, s)
    # greek + misc named commands
    s = re.sub(r"\\([A-Za-z]+)", lambda m: f" {_GREEK.get(m.group(1), m.group(1))} ", s)

    # operators / punctuation to words (within maths context)
    s = s.replace("=", " equals ").replace("+", " plus ")
    s = re.sub(r"(?<=[\w)])\s*-\s*(?=[\w(])", " minus ", s)  # binary minus
    s = s.replace("*", " times ").replace("/", " over ")
    s = s.replace("{", " ").replace("}", " ").replace("\\", " ")
    s = s.replace("(", " ").replace(")", " ")

    s = re.sub(r"\s+", " ", s).strip()
    return s


_MATH_SPAN = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$", re.S)


def speak_inline_math(text: str) -> str:
    """Replace $...$ / $$...$$ spans in a string with their spoken form."""
    def repl(m: re.Match) -> str:
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        return " " + latex_to_speech(inner) + " "

    return _MATH_SPAN.sub(repl, text)


def speak_residual(text: str) -> str:
    """Prose-safe: convert only *unambiguous* LaTeX tokens (\\frac, \\sqrt, ^, _,
    \\commands) left in ordinary text. Does NOT touch +, -, =, * (which are normal
    punctuation in prose), so hyphenated words stay intact."""
    s = text
    s = re.sub(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r" \1 over \2 ", s)
    s = re.sub(r"\\sqrt\s*\{([^{}]*)\}", r" the square root of \1 ", s)
    s = re.sub(r"([A-Za-z0-9])\^\s*\{([^{}]*)\}", lambda m: f"{m.group(1)}{_superscript(m.group(2))}", s)
    s = re.sub(r"([A-Za-z0-9])\^\s*([A-Za-z0-9])", lambda m: f"{m.group(1)}{_superscript(m.group(2))}", s)
    s = re.sub(r"([A-Za-z0-9])_\s*\{([^{}]*)\}", r"\1 sub \2 ", s)
    s = re.sub(r"([A-Za-z0-9])_\s*([A-Za-z0-9])", r"\1 sub \2 ", s)
    for pat, word in _SYMBOLS.items():
        s = re.sub(pat, word, s)
    s = re.sub(r"\\([A-Za-z]+)", lambda m: f" {_GREEK.get(m.group(1), m.group(1))} ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s
