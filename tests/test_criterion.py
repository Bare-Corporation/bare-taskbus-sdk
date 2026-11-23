"""Round-trip tests for the canonical criterion grammar (row 762).

Each case is a real directive STRING: it is split, parsed, rendered back into
canonical form, and then parsed again. A directive kind passes only when the
canonical rendering re-parses to the SAME (kind, args) it started from - which
is the property the supervisor depends on, because it registers a LABEL and
matches evidence against that same label later.

The row-761 defect failed exactly here: the label was rendered with ";" between
arguments, and ";" is the directive separator, so the rendered label parsed as
two directives instead of one.
"""
import unittest

from bare_taskbus.criterion import (
    GRAMMAR_VERSION,
    criterion_label,
    directives,
    format_check,
    parse_check,
    split_checks,
)

PATH = "/home/bare-ai/bare-necessities-workspace/artefacts/row762.json"
MD5 = "0123456789abcdef0123456789abcdef"
SHA = "bd81f52dce41c996f42dd6f690d78ecaad230a03da4942bdc72e136085810a08"
SHA2 = "5c140c4a9dc2deb7a5dbaf9c4c22d9fe53ff2620" + "0" * 24

# The nine directive kinds the row's original harness covered, PLUS the case it
# said it did not: a file_excludes TEXT that itself contains "=".
CASES = [
    ("exit_zero", "exit_zero"),
    ("file_exists", "file_exists:" + PATH),
    ("file_excludes_eq_in_text", "file_excludes:" + PATH + "=RESULT=FAIL"),
    ("file_excludes_plain", "file_excludes:" + PATH + "=DEPRECATED_MARKER"),
    ("file_md5", "file_md5:" + PATH + "=" + MD5),
    ("file_sha256", "file_sha256:" + PATH + "=" + SHA),
    ("file_changed", "file_changed:" + PATH + "=" + SHA),
    ("http_200", "http_200:https://api.bare-ai.net/v1/models"),
    ("http_status", "http_status:https://api.bare-ai.net/v1/models=200"),
    ("git_ls_remote",
     "git_ls_remote:git@github.com:Bare-Corporation/bare-ai-agent.git@main=" + SHA2),
]


def parsed_one(spec):
    """Parse exactly one directive, refusing anything that splits unevenly."""
    checks = split_checks(spec)
    if len(checks) != 1:
        raise AssertionError("%r split into %d directives" % (spec, len(checks)))
    check, problem = parse_check(checks[0])
    if problem:
        raise AssertionError("%r did not parse: %s" % (spec, problem))
    return check


def round_trip(spec):
    """spec -> parse -> canonical label -> parse. Returns (first, second)."""
    first = parsed_one(spec)
    canonical = format_check(first[0], first[1])
    second = parsed_one(canonical)
    return first, second


class CriterionRoundTrip(unittest.TestCase):
    def test_every_directive_kind_round_trips(self):
        passed = 0
        for name, spec in CASES:
            with self.subTest(directive=name):
                first, second = round_trip(spec)
                self.assertEqual(first, second,
                                 "%s did not survive its own canonical form" % name)
                passed += 1
        self.assertEqual(passed, len(CASES))

    def test_grammar_version_is_declared(self):
        self.assertTrue(GRAMMAR_VERSION)

    def test_two_argument_directive_uses_equals_not_semicolon(self):
        # THE ROW-761 ASSERTION: this is the exact shape that used to break.
        rendered = format_check("file_exists", [PATH, MD5])
        self.assertEqual(rendered, "file_exists:" + PATH + "=" + MD5)
        self.assertNotIn(";", rendered)
        # And the broken shape really was broken: it parses as two directives.
        self.assertEqual(len(split_checks("file_exists:" + PATH + ";" + MD5)), 2)

    def test_label_never_contains_the_separator_inside_one_check(self):
        label = criterion_label([{"kind": "file_sha256", "args": [PATH, SHA]}])
        self.assertEqual(split_checks(label), [label])

    def test_multi_directive_label_round_trips(self):
        # http_200 canonicalises to http_status:URL=200, so the expectation is
        # built from the PARSED checks rather than the raw input spelling.
        non_zero = [(name, spec) for name, spec in CASES if spec != "exit_zero"]
        criteria = []
        for _, spec in non_zero:
            check = parsed_one(spec)
            criteria.append({"kind": check[0], "args": list(check[1])})
        expected = " ; ".join(
            format_check(c["kind"], c["args"]) for c in criteria)
        label = criterion_label(criteria)
        self.assertEqual(label, expected)
        checks = split_checks(label)
        self.assertEqual(len(checks), len(non_zero))
        for spec in checks:
            self.assertIsNone(parse_check(spec)[1])

    def test_equals_inside_a_file_excludes_text_is_preserved(self):
        first = parsed_one("file_excludes:" + PATH + "=RESULT=FAIL")
        self.assertEqual(first[0], "file_excludes")
        self.assertEqual(first[1], (PATH, "RESULT=FAIL"))
        second = parsed_one(format_check(first[0], first[1]))
        self.assertEqual(first, second)

    def test_tilde_path_is_refused_at_parse_time(self):
        check, problem = parse_check("file_exists:~/.bare-ai/lib/x.py")
        self.assertIsNone(check)
        self.assertIn("tilde", problem)

    def test_unknown_directive_is_refused(self):
        check, problem = parse_check("nonsense:whatever")
        self.assertIsNone(check)
        self.assertIn("unrecognised", problem)

    def test_directives_helper_matches_the_parser(self):
        pairs = directives("file_exists:" + PATH + " ; exit_zero")
        self.assertEqual(pairs[0][0], "file_exists")
        self.assertEqual(pairs[1][0], "exit_zero")
        prose = directives("this is not a directive")
        self.assertEqual(prose[0][0], "prose")
        # A spec the parser refuses is surfaced as prose, never half-accepted.
        refused = directives("file_exists:~/.bare-ai/x")
        self.assertEqual(refused[0][0], "prose")


if __name__ == "__main__":
    unittest.main()
