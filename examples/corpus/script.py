"""Small real-ish Python file used as held-out corpus text."""

import re

TOKEN = re.compile(r"[a-z]+")
CACHE = {}


def scan(text):
    total = 0
    for ch in text:
        if ch == "x":
            total += 1
    return total


def stats(text):
    words = TOKEN.findall(text.lower())
    return [len(words), sum(map(len, words)), len(CACHE)]
