"""Canonical success_criterion grammar for the task bus.

ONE implementation, imported by every consumer: the poller (claim-time gate),
the runner (supervisor label) and the reviewer (artefact check). Before this
module existed the grammar was implemented three times - poller, runner and
reviewer - and they disagreed. The runner joined a two-argument directive with
";" so "file_exists:<path>=<hash>" became "file_exists:<path>;<hash>", and ";"
is also the directive separator, so a CORRECT run could never match the label
the supervisor had registered for it (row 761). The reviewer split on
whitespace as well as ";" and accepted arguments the poller refused.

GRAMMAR_VERSION is bumped whenever the parse, the formatter or the label
changes shape, and the runner logs it, so a node's behaviour is knowable from
its own output.

Nothing here executes a check. Evaluation (ssh, http, hashing) stays in the
poller, because that is a node-execution concern.
"""

import re

# Bumped when parse_check, format_check or criterion_label change shape.
GRAMMAR_VERSION = "1"

VERIFY_SEP = ";"
VERIFY_TIMEOUT = 25

_MD5_RE = re.compile(r"^[0-9a-f]{32}$", re.I)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.I)


def split_checks(text):
    return [p.strip() for p in (text or "").split(VERIFY_SEP) if p.strip()]


def _path_problem(kind, path):
    """Reject a path that this completion check could never evaluate.

    TILDE IS NOT EXPANDED. run_check() passes every path through shlex.quote()
    before it reaches the target shell, and a QUOTED tilde stays literal - so
    "~/.bare-ai/lib/x.py" is tested as a file literally named "~", and the check
    reports NOFILE even when the real file exists and is correct. That silently
    authors an ALWAYS-FAILING check, which is worse than a loud error because it
    looks exactly like a legitimate MISSING-ARTIFACT verdict.
    Caught 2026-09-17: a real criterion was authored with a tilde path and would
    have reported NOFILE on every node while the artifact was present.
    Refused at PARSE time so the mistake cannot be saved into a row at all.
    """
    if "~" in path:
        return ("%s path %r contains a tilde: a quoted tilde is NOT expanded by "
                "the target shell, so this check can never pass. Use an "
                "ABSOLUTE path instead (e.g. /home/bare-ai/...)." % (kind, path))
    return None


def parse_check(spec):
    """Parse ONE directive. Returns (check, None) or (None, problem). Strict."""
    low = spec.strip().lower()
    if low == "exit_zero":
        return ("exit_zero", ()), None
    if ":" not in spec:
        return None, "unrecognised directive %r (not exit_zero and has no colon)" % spec
    kind, rest = spec.split(":", 1)
    kind, rest = kind.strip().lower(), rest.strip()

    if kind == "file_exists":
        if not rest:
            return None, "file_exists needs a path"
        problem = _path_problem("file_exists", rest)
        if problem:
            return None, problem
        return ("file_exists", (rest,)), None

    if kind == "file_excludes":
        # NEGATIVE assertion (ticket 246): pass only when the file exists AND the literal text is
        # ABSENT. Added because exit_zero and file_exists both certified work that had not happened;
        # a refactor is only done when the thing being removed is actually gone.
        if "=" not in rest:
            return None, "file_excludes needs PATH=TEXT"
        # split on the FIRST "=": the grammar is <path>=<text>, and the TEXT may itself
        # contain "=" (e.g. RESULT=FAIL). rsplit here silently turned the text into part of
        # the path, so the check looked for a file named "<path>=RESULT" and failed with a
        # NOFILE message that read exactly like a missing artefact (row 515, 2026-11-06).
        path, lit = rest.split("=", 1)
        path, lit = path.strip(), lit.strip()
        if not path:
            return None, "file_excludes needs a path"
        if not lit:
            return None, "file_excludes needs a non-empty TEXT to look for"
        problem = _path_problem("file_excludes", path)
        if problem:
            return None, problem
        return ("file_excludes", (path, lit)), None

    if kind in ("file_md5", "file_sha256"):
        if "=" not in rest:
            return None, "%s needs PATH=HASH" % kind
        path, want = rest.rsplit("=", 1)
        path, want = path.strip(), want.strip().lower()
        rx = _MD5_RE if kind == "file_md5" else _SHA256_RE
        if not path:
            return None, "%s needs a path" % kind
        problem = _path_problem(kind, path)
        if problem:
            return None, problem
        if not rx.match(want):
            return None, "%s hash is not the right shape for %s" % (want, kind)
        return (kind, (path, want)), None

    if kind == "http_200":
        if not rest:
            return None, "http_200 needs a url"
        return ("http_status", (rest, 200)), None

    if kind == "http_status":
        if "=" not in rest:
            return None, "http_status needs URL=CODE"
        url, code = rest.rsplit("=", 1)
        url, code = url.strip(), code.strip()
        if not url or not code.isdigit():
            return None, "http_status needs URL=<numeric code>"
        return ("http_status", (url, int(code))), None

    if kind == "git_ls_remote":
        if "=" not in rest:
            return None, "git_ls_remote needs REPO@BRANCH=SHA"
        where, want = rest.rsplit("=", 1)
        where, want = where.strip(), want.strip().lower()
        if "@" not in where:
            return None, "git_ls_remote needs REPO@BRANCH"
        repo, branch = where.rsplit("@", 1)
        repo, branch = repo.strip(), branch.strip()
        if not repo or not branch or not want:
            return None, "git_ls_remote needs REPO@BRANCH=SHA"
        return ("git_ls_remote", (repo, branch, want)), None

    if kind == "file_changed":
        # CHANGE assertion (row 496). file_exists certifies a path that may already have
        # been there; file_sha256 pins an exact end state the author often cannot know in
        # advance. This asserts only that the content is no longer what it was - the one
        # thing an executor can prove about its own work without a verifier-owned artefact.
        if "=" not in rest:
            return None, "file_changed needs PATH=SHA256_BEFORE"
        path, before = rest.rsplit("=", 1)
        path, before = path.strip(), before.strip().lower()
        if not path:
            return None, "file_changed needs a path"
        problem = _path_problem("file_changed", path)
        if problem:
            return None, problem
        if not _SHA256_RE.match(before):
            return None, ("file_changed needs the BEFORE sha256 (64 hex chars), got %r"
                          % before)
        return ("file_changed", (path, before)), None

    return None, "unrecognised directive %r" % kind


def format_check(kind, args):
    """Render ONE directive in canonical form.

    THE RULE THAT ROW 761 BROKE: a two-argument directive is PATH=VALUE, never
    PATH;VALUE. ";" is the directive separator, so joining arguments with it
    made the rendered label ambiguous and unmatchable. The three shapes are:

        no arguments                     -> kind
        git_ls_remote(repo, branch, sha) -> kind:REPO @BRANCH=SHA
        exactly two arguments            -> kind:PATH=VALUE
        anything else                    -> kind:ARG;ARG   (lossless fallback)
    """
    kind = str(kind or "").strip()
    args = [str(a) for a in (args or [])]
    if not kind:
        return ""
    if not args:
        return kind
    if kind == "git_ls_remote" and len(args) >= 3:
        return kind + ":" + args[0] + " @" + args[1] + "=" + args[2]
    if len(args) == 2:
        return kind + ":" + args[0] + "=" + args[1]
    return kind + ":" + VERIFY_SEP.join(args)


def criterion_label(criteria):
    """One stable label for a parsed criterion list.

    The supervisor keys its state file, its brief and its evidence by this
    string, while the executor takes the parsed list. The same label must come
    back on every pass, so this stays deterministic and depends only on the
    parsed kinds and arguments.
    """
    parts = []
    for check in criteria or []:
        if not isinstance(check, dict):
            continue
        rendered = format_check(check.get("kind"), check.get("args") or [])
        if rendered:
            parts.append(rendered)
    return " ; ".join(parts)


def directives(text):
    """(kind, argument) pairs for a reviewer that inspects rather than parses.

    A spec that does not parse comes back as ("prose", spec) so the caller can
    refuse the whole criterion, which is how the reviewer already treated text
    it could not understand. Note the split is on ";" ONLY: splitting on
    whitespace, as the reviewer used to, breaks any argument that legitimately
    contains a space.
    """
    out = []
    for spec in split_checks(text):
        if ":" not in spec:
            low = spec.strip().lower()
            out.append((low, "") if low == "exit_zero" else ("prose", spec))
            continue
        check, problem = parse_check(spec)
        if problem or not check:
            out.append(("prose", spec))
            continue
        kind, rest = spec.split(":", 1)
        out.append((kind.strip().lower(), rest.strip()))
    return out
