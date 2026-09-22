import os, signal, subprocess, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "regressgate"))
import runner
D = os.path.dirname(os.path.abspath(__file__))
env = dict(os.environ, PROMPTFOO_CONFIG_DIR=D+"/cfgdir", PROMPTFOO_DISABLE_TELEMETRY="1")
argv = runner.build_argv(os.environ["REGRESSGATE_NODE"], os.environ["REGRESSGATE_PROMPTFOO"],
                         D+"/fixture/slow.yaml", D+"/o_int.json", None, None)
p = subprocess.Popen(argv, env=env, cwd=D+"/fixture",
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(8)
p.send_signal(signal.SIGINT)
code = p.wait(timeout=60)
print("raw promptfoo exit after SIGINT =", code)
print("runner would classify:", runner.INTERRUPTED if code == 130 else "NOT 130 -> "+str(code))
