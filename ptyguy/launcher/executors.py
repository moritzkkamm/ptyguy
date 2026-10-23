"""Executors: where a job runs. Same three methods for local and (later) SLURM."""
import os
import subprocess
import sys


def _insert(run, placeholder, value):
    """Quote value, unless the user already put the placeholder inside quotes, e.g.
    matlab -batch "recon('{job}')"  -> inserted as is."""
    i = run.find(placeholder)
    if i > 0 and run[i - 1] in "'\"":
        return run.replace(placeholder, value)
    return run.replace(placeholder, '"%s"' % value)


def render_command(run, job_json):
    """{job} -> path of job.json, {python} -> the Python PtyGuy runs in (same env)."""
    return _insert(_insert(run, "{python}", sys.executable), "{job}", str(job_json))


class LocalExecutor:
    """Runs jobs as subprocesses on this machine."""

    def start(self, command, cwd, log_path, env=None):
        log = open(log_path, "w")            # a file, never a PIPE (blocks when full)
        full_env = dict(os.environ, **(env or {}))
        proc = subprocess.Popen(command, shell=True, cwd=str(cwd), stdout=log,
                                stderr=subprocess.STDOUT, env=full_env)
        proc._ptyguy_log = log               # closed in poll() when the job ends
        return proc

    def poll(self, proc):
        """None while running, else the exit code."""
        code = proc.poll()
        if code is not None and not proc._ptyguy_log.closed:
            proc._ptyguy_log.close()
        return code

    def pid(self, proc):
        return proc.pid

    def cancel(self, proc):
        proc.terminate()
