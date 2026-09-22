"""Upgrade tripwire for promptfoo.

Every behaviour this repo's harness depends on is asserted here against the REAL
promptfoo binary. If promptfoo changes shape under us, exactly one named test
breaks and its name says which contract died.

stdlib unittest only -- no pytest, no deps.

    PROMPTFOO_BIN="node /path/to/dist/src/main.js" python3 -m unittest -v \
        tests.test_promptfoo_contract

Env knobs (all optional, sane defaults):
    PROMPTFOO_BIN       command to run promptfoo (default: `promptfoo`)
    PROMPTFOO_PIN       version string this file was verified against
"""

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PIN = os.environ.get("PROMPTFOO_PIN", "0.123.1")
BIN = shlex.split(os.environ.get("PROMPTFOO_BIN", "promptfoo"))

# One isolated config dir for the whole file: NEVER the user's ~/.promptfoo.
_WORK = tempfile.mkdtemp(prefix="pf-contract-")
_CFGDIR = os.path.join(_WORK, "cfgdir")
os.makedirs(_CFGDIR, exist_ok=True)

ENV = {
    **os.environ,
    "PROMPTFOO_CONFIG_DIR": _CFGDIR,
    "PROMPTFOO_DISABLE_TELEMETRY": "1",
    "PROMPTFOO_DISABLE_UPDATE": "1",
    # Deliberately NOT set: PROMPTFOO_PASS_RATE_THRESHOLD (default 100),
    # PROMPTFOO_FAILED_TEST_EXIT_CODE (default 100). Tests that need them set
    # them per-run so an inherited value can't mask a regression.
}
for _leak in ("PROMPTFOO_PASS_RATE_THRESHOLD", "PROMPTFOO_FAILED_TEST_EXIT_CODE"):
    ENV.pop(_leak, None)


# A local, keyless, NON-DETERMINISTIC provider whose HTTP hits we can count.
# Server-side hit count is the only honest cache-replay evidence: promptfoo's
# stats.tokenUsage.numRequests counts ROWS, not requests, and is identical for
# a fully-cached replay and a fully-fresh run.
PORT = 19137
HITS = [0]


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        HITS[0] += 1
        body = json.dumps({"output": uuid.uuid4().hex}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


_SERVER = None


def setUpModule():
    global _SERVER
    _SERVER = ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
    threading.Thread(target=_SERVER.serve_forever, daemon=True).start()


def tearDownModule():
    if _SERVER:
        _SERVER.shutdown()
    shutil.rmtree(_WORK, ignore_errors=True)


def write(name, body):
    """Write a fixture into the isolated work dir, return its abs path."""
    path = os.path.join(_WORK, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(textwrap.dedent(body))
    return path


def run_eval(config, *extra, env=None, out="out.json"):
    """Run `promptfoo eval`. Returns (exit_code, parsed_output_or_None, proc)."""
    out_path = os.path.join(_WORK, out)
    if os.path.exists(out_path):
        os.remove(out_path)
    proc = subprocess.run(
        BIN + ["eval", "-c", config, "-o", out_path] + list(extra),
        capture_output=True,
        text=True,
        cwd=_WORK,
        env={**ENV, **(env or {})},
    )
    data = None
    if os.path.exists(out_path):
        with open(out_path) as fh:
            data = json.load(fh)
    return proc.returncode, data, proc


ECHO_PASS = """\
    providers: [echo]
    prompts: ['{{text}}']
    tests:
      - description: alpha
        vars: {text: hello}
        assert: [{type: contains, value: hello}]
      - description: beta
        vars: {text: world}
        assert: [{type: contains, value: world}]
"""

ECHO_FAIL = """\
    providers: [echo]
    prompts: ['{{text}}']
    tests:
      - description: alpha
        vars: {text: hello}
        assert: [{type: contains, value: hello}]
      - description: beta
        vars: {text: world}
        assert: [{type: contains, value: NOPE}]
"""


class TestPromptfooContract(unittest.TestCase):
    maxDiff = None

    # ------------------------------------------------------------------ 8 --
    def test_00_version_pin(self):
        """The whole file is only evidence for ONE version. Say so loudly."""
        proc = subprocess.run(BIN + ["--version"], capture_output=True, text=True, env=ENV)
        got = proc.stdout.strip().splitlines()[-1].strip()
        self.assertEqual(
            got,
            PIN,
            f"\n\n*** promptfoo is {got!r}, this contract file was verified against {PIN!r}.\n"
            f"*** Do NOT bump the pin to silence this. Re-run this whole file against\n"
            f"*** {got} and treat every other failure here as a real behaviour change.\n",
        )

    # ------------------------------------------------------------------ 1 --
    def test_01_exit_zero_when_all_pass(self):
        code, data, proc = run_eval(write("pass.yaml", ECHO_PASS), "--no-cache")
        self.assertEqual(code, 0, proc.stderr[-2000:])
        self.assertEqual(data["results"]["stats"]["successes"], 2)

    def test_02_exit_100_on_failing_assertion(self):
        code, data, proc = run_eval(write("fail.yaml", ECHO_FAIL), "--no-cache")
        self.assertEqual(code, 100, proc.stderr[-2000:])
        self.assertEqual(data["results"]["stats"]["failures"], 1)

    def test_03_exit_1_on_malformed_config(self):
        bad = write("bad.yaml", "providers: [echo\nprompts: '{{text}}'\n")
        code, _, proc = run_eval(bad, "--no-cache")
        self.assertEqual(code, 1, proc.stderr[-2000:])

    def test_04_failed_test_exit_code_override(self):
        code, _, proc = run_eval(
            write("fail.yaml", ECHO_FAIL),
            "--no-cache",
            env={"PROMPTFOO_FAILED_TEST_EXIT_CODE": "42"},
        )
        self.assertEqual(code, 42, proc.stderr[-2000:])

    # ------------------------------------------------------------------ 2 --
    def test_05_provider_error_is_error_not_failure_and_still_exits_100(self):
        """THE infra-vs-quality tripwire.

        A provider blowing up depresses passRate exactly like a bad answer.
        Exit code alone cannot tell "the model got worse" from "our API key
        rotated". stats.errors vs stats.failures is the only signal.
        """
        write(
            "boom.py",
            """\
            def call_api(prompt, options, context):
                raise RuntimeError("simulated provider outage")
            """,
        )
        code, data, proc = run_eval(
            write(
                "err.yaml",
                """\
                providers: ['file://boom.py']
                prompts: ['{{text}}']
                tests:
                  - description: alpha
                    vars: {text: hello}
                    assert: [{type: contains, value: hello}]
                """,
            ),
            "--no-cache",
        )
        stats = data["results"]["stats"]
        self.assertEqual(code, 100, proc.stderr[-2000:])
        self.assertEqual(stats["errors"], 1, f"stats={stats}")
        self.assertEqual(stats["failures"], 0, f"stats={stats}")
        self.assertEqual(stats["successes"], 0, f"stats={stats}")

    # ------------------------------------------------------------------ 3 --
    def test_06_zero_tests_exits_zero(self):
        """A typo'd --filter-pattern is a GREEN build. passRate is NaN and
        `NaN < threshold` is false. Our harness must assert its own expected
        case count; promptfoo will never do it for us."""
        code, data, proc = run_eval(
            write("pass.yaml", ECHO_PASS), "--no-cache", "--filter-pattern", "zzz-matches-nothing"
        )
        self.assertEqual(code, 0, proc.stderr[-2000:])
        rows = data["results"]["results"]
        self.assertEqual(rows, [], f"expected no rows, got {len(rows)}")
        stats = data["results"]["stats"]
        self.assertEqual(stats["successes"] + stats["failures"] + stats["errors"], 0)

    # ------------------------------------------------------------------ 4 --
    def test_07_output_json_shape(self):
        _, data, _ = run_eval(write("pass.yaml", ECHO_PASS), "--no-cache")
        self.assertEqual(
            set(data),
            {
                "evalId",
                "results",
                "config",
                "shareableUrl",
                "metadata",
                "vars",
                "runtimeOptions",
            },
            "top-level key set changed",
        )
        self.assertEqual(data["results"]["version"], 3, "output schema version bumped")
        self.assertIsInstance(data["results"]["results"], list, "doubled-key row array gone")
        row = data["results"]["results"][0]
        # Only the keys our parser actually dereferences.
        for key in (
            "score",
            "success",
            "failureReason",
            "testIdx",
            "promptIdx",
            "vars",
            "metadata",
            "response",
            "gradingResult",
            "namedScores",
            "latencyMs",
            "tokenUsage",
            "provider",
        ):
            self.assertIn(key, row, f"row key {key!r} disappeared")

    # ------------------------------------------------------------------ 5 --
    def test_08_assert_set_flattening_still_broken(self):
        """componentResults under an assert-set is a FLATTENED list: each child
        appears twice (nested + promoted) and the wrapper has no 'assertion'
        key. Any naive `c["assertion"]["type"]` walk raises TypeError.

        If promptfoo ever fixes this, THIS TEST FAILS -- and we get to delete
        the leaf-filtering workaround. That is a good failure."""
        _, data, _ = run_eval(
            write(
                "assertset.yaml",
                """\
                providers: [echo]
                prompts: ['{{text}}']
                tests:
                  - description: alpha
                    vars: {text: hello world}
                    assert:
                      - type: assert-set
                        assert:
                          - type: contains
                            value: hello
                          - type: contains
                            value: world
                """,
            ),
            "--no-cache",
        )
        comps = data["results"]["results"][0]["gradingResult"]["componentResults"]
        wrappers = [c for c in comps if "assertion" not in c or c["assertion"] is None]
        leaves = [c for c in comps if c.get("assertion") and "componentResults" not in c]
        self.assertTrue(wrappers, "wrapper-without-'assertion' element is gone (fixed upstream?)")
        self.assertGreater(
            len(comps), 2, "children no longer duplicated (flattening fixed upstream?)"
        )
        types = sorted(c["assertion"]["type"] for c in leaves)
        self.assertEqual(types, ["contains", "contains"], f"leaf filter broke: {types}")

    # ------------------------------------------------------------------ 6 --
    def test_09_repeat_cache_namespacing_and_cross_run_replay(self):
        """Two halves of one contract, proven with SERVER-SIDE hit counts:

        (a) WITHIN a run, cache is namespaced per repeat index -- --repeat with
            cache ON does NOT replay one value N times. 2 rows => 2 real hits.
        (b) ACROSS runs, an identical re-run replays everything: 0 new hits.
            Which is exactly why --no-cache is mandatory for --repeat sampling.
        """
        cfg = write(
            "http.yaml",
            f"""\
            providers:
              - id: 'http://127.0.0.1:{PORT}/'
                config:
                  method: POST
                  headers: {{'Content-Type': 'application/json'}}
                  body: {{prompt: '{{{{prompt}}}}'}}
                  transformResponse: 'json.output'
            prompts: ['{{{{text}}}}']
            tests:
              - description: alpha
                vars: {{text: hello}}
            """,
        )
        before = HITS[0]
        _, first, proc = run_eval(cfg, "--repeat", "2", out="rep1.json")
        outs1 = [r["response"]["output"] for r in first["results"]["results"]]
        hits1 = HITS[0] - before
        self.assertEqual(len(outs1), 2, "--repeat 2 no longer yields 2 rows")
        self.assertEqual(hits1, 2, f"(a) repeat cache namespacing gone: {hits1} server hits")
        self.assertEqual(len(set(outs1)), 2, f"(a) one value replayed N times: {outs1}")

        _, second, _ = run_eval(cfg, "--repeat", "2", out="rep2.json")
        outs2 = [r["response"]["output"] for r in second["results"]["results"]]
        self.assertEqual(
            HITS[0] - before, 2, "(b) identical re-run hit the server -- cross-run replay gone"
        )
        self.assertEqual(sorted(outs1), sorted(outs2), "(b) replayed values differ")
        self.assertTrue(
            all(r["response"].get("cached") for r in second["results"]["results"]),
            "(b) rows not marked cached=true",
        )

    def test_09b_python_provider_never_replays_across_runs(self):
        """Gotcha guard. The python provider's cache key embeds context.vars,
        which contains a per-run __evalId -- so a file://x.py provider gets a
        cross-run cache hit NEVER, regardless of --no-cache. Any experiment
        about caching written with a python provider measures nothing.

        If this starts passing-by-replay, our cache reasoning needs revisiting.
        """
        write(
            "nondet.py",
            """\
            import uuid

            def call_api(prompt, options, context):
                return {"output": uuid.uuid4().hex}
            """,
        )
        cfg = write(
            "nondet.yaml",
            """\
            providers: ['file://nondet.py']
            prompts: ['{{text}}']
            tests:
              - description: alpha
                vars: {text: hello}
            """,
        )
        _, a, _ = run_eval(cfg, out="py1.json")
        _, b, _ = run_eval(cfg, out="py2.json")
        out_a = a["results"]["results"][0]["response"]["output"]
        out_b = b["results"]["results"][0]["response"]["output"]
        self.assertNotEqual(
            out_a, out_b, "python provider now replays across runs (__evalId left the cache key?)"
        )
        self.assertFalse(b["results"]["results"][0]["response"].get("cached"))

    # ------------------------------------------------------------------ 7 --
    def test_10_repeat_index_provider_visible_assertion_invisible(self):
        """__repeatIndex is in the provider's context['vars'] but is STRIPPED
        from assertion/grader input by design. Provider-echo is therefore the
        ONLY way to label a sample with its repeat. If this flips, our grouping
        silently changes shape."""
        write(
            "echoidx.py",
            """\
            def call_api(prompt, options, context):
                return {
                    "output": "ok",
                    "metadata": {"repeatIndex": context["vars"].get("__repeatIndex")},
                }
            """,
        )
        write(
            "sees_idx.py",
            """\
            def get_assert(output, context):
                keys = sorted(k for k in context["vars"] if k.startswith("__"))
                return {"pass": True, "score": 1.0, "reason": "dunder_vars=" + repr(keys)}
            """,
        )
        _, data, _ = run_eval(
            write(
                "idx.yaml",
                """\
                providers: ['file://echoidx.py']
                prompts: ['{{text}}']
                tests:
                  - description: alpha
                    vars: {text: hello}
                    assert:
                      - type: python
                        value: 'file://sees_idx.py'
                """,
            ),
            "--no-cache",
            "--repeat",
            "2",
        )
        rows = data["results"]["results"]
        seen = sorted(r["metadata"]["repeatIndex"] for r in rows)
        self.assertEqual(seen, [0, 1], f"provider lost __repeatIndex: {seen}")
        self.assertEqual(
            sorted(r["response"]["metadata"]["repeatIndex"] for r in rows),
            [0, 1],
            "response.metadata no longer carries provider metadata",
        )
        reasons = {c["reason"] for r in rows for c in r["gradingResult"]["componentResults"]}
        self.assertEqual(
            reasons,
            {"dunder_vars=[]"},
            f"__repeatIndex is now VISIBLE to assertions -- grouping strategy can change: {reasons}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
