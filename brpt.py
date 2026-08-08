#!/usr/bin/env python3
"""BRPT cubic Frobenius probable-primality test"""

import argparse
from math import gcd, isqrt, floor

LN2 = 0.6931471805599453


def jacobi_symbol(a, n):
    """Return the Jacobi symbol (a/n), for odd positive n"""
    a = a % n
    sign = 1
    while a:
        twos = (a & -a).bit_length() - 1
        a >>= twos
        if twos & 1 and (n & 7) in (3, 5):
            sign = -sign
        if (a & 3) == (n & 3) == 3:
            sign = -sign
        a, n = n % a, a
    return sign if n == 1 else 0


def cubic_power(exponent, a, b, n):
    """Return X**exponent modulo X**3 + a*X + b and n"""
    r0, r1, r2 = 0, 1, 0

    for bit in bin(exponent)[3:]:
        r2_sq, twice_r1r2 = r2 * r2, (r1 * r2) << 1
        s0 = r0 * r0 - b * twice_r1r2
        s1 = ((r0 * r1) << 1) - a * twice_r1r2 - b * r2_sq
        s2 = ((r0 * r2) << 1) + r1 * r1 - a * r2_sq

        if bit == "1":
            s0, s1, s2 = -b * s2, s0 - a * s2, s1

        r0, r1, r2 = s0 % n, s1 % n, s2 % n

    return r0, r1, r2


def cubic_check(xn, a, b, n):
    """Return True/False when conclusive, or None to try another pair."""
    x0, x1, x2 = xn
    v = x1 - 1
    v2, x2_sq = v * v, x2 * x2

    t = x0 - a * x2
    q1 = t * t + a * v2
    q2 = v2 - 3 * x0 * x2 + a * x2_sq
    norm = x0 * q1 - b * v * q2 + b * b * x2_sq * x2
    divisor = gcd(norm, n)

    if divisor != 1:
        return None if divisor == n else False

    twice_x1x2 = (x1 * x2) << 1
    s0 = (x0 * x0 - b * twice_x1x2) % n
    s1 = (((x0 * x1) << 1) - a * twice_x1x2 - b * x2_sq) % n
    s2 = (((x0 * x2) << 1) + x1 * x1 - a * x2_sq) % n

    p0, p1, p2 = s0 * x0, s1 * x1, s2 * x2
    c01 = (s0 + s1) * (x0 + x1) - p0 - p1
    c02 = (s0 + s2) * (x0 + x2) - p0 - p2
    c12 = (s1 + s2) * (x1 + x2) - p1 - p2

    return not (
        (p0 + a * x0 + b * (1 - c12)) % n
        or (c01 + a * (x1 - c12) - b * p2) % n
        or (c02 + p1 + a * (x2 - p2)) % n
    )


def find_pair(n):
    """Return the first successful pair (a, b) and X**n, or None."""
    for radius in range(1, max(5, 3 + floor(1.3 * isqrt(int(n.bit_length() * LN2))) + 1)):
        for a in range(-radius, radius + 1):
            if gcd(a, n) != 1:
                continue

            a3_4 = -4 * a**3
            for b in range(-radius, 0 if abs(a) == radius else 1 - radius):
                if gcd(b, n) != 1:
                    continue

                discriminant = a3_4 - 27 * b**2
                if (symbol := jacobi_symbol(discriminant, n)) != 1:
                    if not symbol and discriminant % n:
                        return None
                    continue

                xn = cubic_power(n, a, b, n)
                if (result := cubic_check(xn, a, b, n)) is not None:
                    return ((a, b), xn) if result else None

    return None


def brpt(n):
    """Return True when n passes BRPT, otherwise False."""
    return n in (2, 3, 5) if n < 7 else (
        n % 6 in (1, 5)
        and pow(2, n - 1, n) == 1
        and find_pair(n) is not None
    )


def main():
    parser = argparse.ArgumentParser(
        description="Apply the BRPT cubic Frobenius probable-primality test.",
        epilog=(
            "Examples:\n"
            "  python brpt.py 17\n"
            "  python brpt.py 2**127 - 1\n"
            '  python brpt.py "pow(2, 521) - 1"'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "expression",
        nargs="+",
        help="Integer expression; only integer arithmetic and pow() are available.",
    )
    expression = " ".join(parser.parse_args().expression)

    try:
        candidate = eval(expression, {"__builtins__": {}}, {"pow": pow})
    except Exception as exc:
        raise ValueError(f"Invalid integer expression: {expression}") from exc

    if not isinstance(candidate, int):
        raise ValueError("The expression must evaluate to an integer")

    print("PRIME" if brpt(candidate) else "COMPOSITE")


if __name__ == "__main__":
    main()