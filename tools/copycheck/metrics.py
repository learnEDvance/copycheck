"""Character-level copy-fidelity scoring for a benchmark.

Python 3.14, stdlib only.
"""


def levenshtein(a: str, b: str) -> int:
    """Standard edit distance (insert/delete/substitute cost 1).

    Memory-efficient: only two rows of the DP table are kept, so inputs up to
    ~10k characters are fine.
    """
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    cur = [0] * (len(b) + 1)
    for i, ca in enumerate(a, 1):
        cur[0] = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev, cur = cur, prev
    return prev[len(b)]


def _traceback(a: str, b: str):
    """Full-matrix backtrace -> (insertions, deletions, substitutions)."""
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(n + 1):
        dp[i][0] = i
    for i in range(1, n + 1):
        row = dp[i]
        prev_row = dp[i - 1]
        ca = a[i - 1]
        for j in range(1, m + 1):
            cost = 0 if ca == b[j - 1] else 1
            row[j] = min(prev_row[j] + 1, row[j - 1] + 1, prev_row[j - 1] + cost)
    i, j = n, m
    insertions = deletions = substitutions = 0
    while i > 0 or j > 0:
        if i > 0 and j > 0 and a[i - 1] == b[j - 1]:
            i -= 1
            j -= 1
        elif i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + 1:
            substitutions += 1
            i -= 1
            j -= 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            deletions += 1
            i -= 1
        else:
            insertions += 1
            j -= 1
    return insertions, deletions, substitutions


def _lcs(src: str, first_divergence: int, exact: bool) -> int:
    if exact:
        return len(src)
    return len(src) - first_divergence


def score(src: str, out: str) -> dict:
    edit_distance = levenshtein(src, out)
    insertions, deletions, substitutions = _traceback(src, out)
    exact = src == out

    common = min(len(src), len(out))
    lcp = 0
    while lcp < common and src[lcp] == out[lcp]:
        lcp += 1
    first_divergence = lcp  # identical => len(src); prefix => min(len); else first differing index

    return {
        "src_len": len(src),
        "out_len": len(out),
        "edit_distance": edit_distance,
        "cer": edit_distance / max(1, len(src)),
        "exact": exact,
        "insertions": insertions,
        "deletions": deletions,
        "substitutions": substitutions,
        "lcp": lcp,
        "lcs": _lcs(src, first_divergence, exact),
        "first_divergence": first_divergence,
        "exact_pct": round(100.0 if exact else 0.0, 2),
        "len_ratio": round(len(out) / len(src), 4) if len(src) else 0.0,
    }


if __name__ == "__main__":
    assert levenshtein("kitten", "sitting") == 3
    assert levenshtein("", "abc") == 3
    assert levenshtein("abc", "") == 3
    assert levenshtein("same", "same") == 0

    r = score("abc", "ac")
    assert r["edit_distance"] == 1 and r["deletions"] == 1
    assert r["lcp"] == 1 and r["lcs"] == 2 and r["first_divergence"] == 1

    r = score("hello", "helxo")
    assert r["substitutions"] == 1
    assert r["lcp"] == 3 and r["lcs"] == 2 and r["first_divergence"] == 3

    r = score("abcd", "abcd")
    assert r["exact"] is True and r["edit_distance"] == 0
    assert r["lcp"] == 4 and r["lcs"] == 4 and r["first_divergence"] == 4

    import random

    random.seed(1234)
    for _ in range(100):
        a = "".join(random.choice("abcde") for _ in range(random.randint(0, 12)))
        b = "".join(random.choice("abcde") for _ in range(random.randint(0, 12)))
        r = score(a, b)
        assert r["edit_distance"] == levenshtein(a, b)
        assert r["substitutions"] + r["insertions"] + r["deletions"] == r["edit_distance"]

    print("ALL METRICS TESTS PASSED")