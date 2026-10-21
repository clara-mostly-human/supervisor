#!/usr/bin/python3
"""Root helper for the native macOS Supervisor.

The Supervisor runs as the logged-in user. Apps that need root (OpenThread
Border Router: utun interfaces, IPv6 forwarding, pf anchors) are installed and
run through this helper, a LaunchDaemon that listens on a Unix socket only the
Supervisor's user can open.

What it does, and nothing else:

* install: copy an app version the Supervisor built (in the user's home) into
  a root-owned folder, so the user account cannot change what runs as root
  afterwards. Symlinks are copied as files.
* service / start / stop / status / unservice: manage the LaunchDaemon
  "<prefix>.app.<slug>", whose program must be inside that root-owned folder.
* copy: copy files root may read but the user may not (an app's data, e.g.
  OTBR's Thread credentials) between folders in the user's home.

Runs on Apple's /usr/bin/python3 (3.9, standard library only): an interpreter
the user cannot modify. Test mode (--test DIR) runs unprivileged against the
user's launchd domain and a scratch folder.
"""

import argparse
import json
import os
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

VERSION = 1
RE_SLUG = re.compile(r"^[a-z0-9_]{1,64}$")
RE_VERSION = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
RE_PREFIX = re.compile(r"^ha-macos(-[a-z0-9]{1,16})?$")
RE_ENV_KEY = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


class Refused(Exception):
    """A request the helper will not carry out."""


class Helper:
    def __init__(self, uid, base, daemons, domain, chown):
        self.uid = uid
        self.home = os.path.realpath(os.path.expanduser("~%s" % _user_name(uid)))
        self.base = base
        self.daemons = daemons
        self.domain = domain
        self.chown = chown

    # Validation

    def _inside_home(self, path):
        real = os.path.realpath(path)
        if real != self.home and not real.startswith(self.home + os.sep):
            raise Refused("%s is outside %s" % (path, self.home))
        return real

    def _app_root(self, prefix, slug, version=None):
        if not RE_PREFIX.match(prefix or ""):
            raise Refused("bad prefix")
        if not RE_SLUG.match(slug or ""):
            raise Refused("bad slug")
        root = os.path.join(self.base, "apps", prefix, slug)
        if version is None:
            return root
        if not RE_VERSION.match(version or ""):
            raise Refused("bad version")
        return os.path.join(root, version)

    def _label(self, prefix, slug):
        self._app_root(prefix, slug)
        return "%s.app.%s" % (prefix, slug)

    def _plist(self, label):
        return os.path.join(self.daemons, label + ".plist")

    # launchctl

    def _launchctl(self, *args):
        proc = subprocess.run(
            ["/bin/launchctl"] + list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        return proc.returncode, proc.stdout.decode(errors="replace")

    def _status(self, label):
        code, text = self._launchctl("print", "%s/%s" % (self.domain, label))
        if code != 0:
            return {"loaded": False, "running": False, "pid": None, "exit": None}
        state = re.search(r"^\s*state = (\S+)", text, re.M)
        pid = re.search(r"^\s*pid = (\d+)", text, re.M)
        exit_code = re.search(r"^\s*last exit code = (-?\d+)", text, re.M)
        signalled = re.search(r"^\s*last terminating signal = ", text, re.M)
        running = bool(state and state.group(1) == "running")
        code_value = int(exit_code.group(1)) if exit_code else None
        if not running and not code_value and signalled:
            code_value = -1
        return {
            "loaded": True,
            "running": running,
            "pid": int(pid.group(1)) if pid else None,
            "exit": code_value,
        }

    # Commands

    def cmd_ping(self, req):
        return {"version": VERSION, "test": self.chown is False}

    def cmd_install(self, req):
        dest = self._app_root(req["prefix"], req["slug"], req["version"])
        source = self._inside_home(req["source"])
        if not os.path.isdir(source):
            raise Refused("source is not a folder")
        tmp = dest + ".installing"
        if os.path.exists(tmp):
            shutil.rmtree(tmp)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        # symlinks=False: copy what links point to, never the links.
        shutil.copytree(source, tmp, symlinks=False)
        for root, dirs, files in os.walk(tmp):
            for name in dirs + files:
                path = os.path.join(root, name)
                if self.chown:
                    os.chown(path, 0, 0)
                mode = os.stat(path).st_mode & 0o7777
                os.chmod(path, mode & ~0o022)
        if self.chown:
            os.chown(tmp, 0, 0)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        os.rename(tmp, dest)
        return {"path": dest}

    def cmd_remove(self, req):
        version = req.get("version")
        path = self._app_root(req["prefix"], req["slug"], version)
        if os.path.exists(path):
            shutil.rmtree(path)
        return {}

    def cmd_service(self, req):
        prefix, slug = req["prefix"], req["slug"]
        root = os.path.realpath(self._app_root(prefix, slug, req["version"]))
        program = os.path.realpath(os.path.join(root, req["program"]))
        if not program.startswith(root + os.sep) or not os.access(program, os.X_OK):
            raise Refused("program must be an executable inside the installed app")
        env = {}
        for key, value in (req.get("env") or {}).items():
            if not RE_ENV_KEY.match(key) or not isinstance(value, str):
                raise Refused("bad environment")
            # Never let the user account choose what root loads or runs:
            # no dynamic-loader variables, and only system folders on PATH.
            if key.startswith(("DYLD_", "LD_")) or key == "PATH":
                continue
            env[key] = value
        env["PATH"] = SYSTEM_PATH
        args = req.get("args") or []
        if not all(isinstance(arg, str) for arg in args):
            raise Refused("bad arguments")
        workdir = self._inside_home(req["working_directory"])
        log = self._inside_home(req["log_file"])
        label = self._label(prefix, slug)
        data = {
            "Label": label,
            "ProgramArguments": [program] + args,
            "EnvironmentVariables": env,
            "WorkingDirectory": workdir,
            "StandardOutPath": log,
            "StandardErrorPath": log,
            "RunAtLoad": bool(req.get("run_at_load")),
            "KeepAlive": False,
        }
        os.makedirs(self.daemons, exist_ok=True)
        path = self._plist(label)
        tmp = path + ".tmp"
        with open(tmp, "wb") as file:
            plistlib.dump(data, file)
        os.chmod(tmp, 0o644)
        os.rename(tmp, path)
        return {"label": label}

    def cmd_start(self, req):
        label = self._label(req["prefix"], req["slug"])
        if not self._status(label)["loaded"]:
            code, text = self._launchctl("bootstrap", self.domain, self._plist(label))
            if code != 0:
                raise Refused("launchctl bootstrap: %s" % text.strip())
        # Loading starts it only with RunAtLoad (boot: auto); kickstart always.
        code, text = self._launchctl("kickstart", "%s/%s" % (self.domain, label))
        if code != 0:
            raise Refused("launchctl kickstart: %s" % text.strip())
        return {}

    def cmd_stop(self, req):
        label = self._label(req["prefix"], req["slug"])
        timeout = min(float(req.get("timeout", 60)), 600)
        status = self._status(label)
        if not status["loaded"]:
            return {}
        if status["pid"]:
            self._launchctl("kill", "SIGTERM", "%s/%s" % (self.domain, label))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and self._status(label)["running"]:
                time.sleep(0.5)
        self._launchctl("bootout", "%s/%s" % (self.domain, label))
        return {}

    def cmd_status(self, req):
        return self._status(self._label(req["prefix"], req["slug"]))

    def cmd_unservice(self, req):
        self.cmd_stop(req)
        path = self._plist(self._label(req["prefix"], req["slug"]))
        if os.path.exists(path):
            os.remove(path)
        return {}

    def cmd_copy(self, req):
        source = self._inside_home(req["source"])
        dest = self._inside_home(req["dest"])
        if not os.path.isdir(source):
            raise Refused("source is not a folder")
        os.makedirs(dest, exist_ok=True)
        for name in os.listdir(source):
            src = os.path.join(source, name)
            if os.path.isfile(src) and not os.path.islink(src):
                shutil.copy2(src, os.path.join(dest, name))
        return {}

    def handle(self, request):
        command = request.get("command")
        method = getattr(self, "cmd_%s" % command, None)
        if method is None:
            raise Refused("unknown command %r" % command)
        return method(request)


def _user_name(uid):
    import pwd

    return pwd.getpwuid(uid).pw_name


def serve(helper, socket_path):
    if os.path.exists(socket_path):
        os.remove(socket_path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    # Only the Supervisor's user can connect.
    os.chmod(socket_path, 0o600)
    if helper.chown:
        os.chown(socket_path, helper.uid, 0)
    server.listen(4)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print("ha-macos-helper %d listening on %s" % (VERSION, socket_path), flush=True)
    while True:
        conn, _ = server.accept()
        with conn:
            conn.settimeout(30)
            request, data = {}, b""
            try:
                while not data.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                request = json.loads(data.decode())
                reply = {"ok": True, "result": helper.handle(request)}
            except Refused as err:
                reply = {"ok": False, "error": str(err)}
            except Exception as err:  # noqa: BLE001 - report, keep serving
                reply = {"ok": False, "error": "%s: %s" % (type(err).__name__, err)}
            outcome = "ok" if reply["ok"] else reply["error"]
            print(
                "%s %s %s -> %s"
                % (
                    time.strftime("%F %T"),
                    request.get("command"),
                    request.get("slug", ""),
                    outcome,
                ),
                flush=True,
            )
            try:
                conn.sendall((json.dumps(reply) + "\n").encode())
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--uid", type=int, required=True, help="the Supervisor's user")
    parser.add_argument("--socket", default="/var/run/ha-macos-helper.sock")
    parser.add_argument("--test", metavar="DIR", help="run unprivileged in DIR")
    args = parser.parse_args()
    if args.test:
        helper = Helper(
            args.uid,
            base=os.path.join(args.test, "base"),
            daemons=os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents"),
            domain="gui/%d" % os.getuid(),
            chown=False,
        )
    else:
        if os.geteuid() != 0:
            sys.exit("must run as root (or with --test)")
        helper = Helper(
            args.uid,
            base="/usr/local/ha-macos",
            daemons="/Library/LaunchDaemons",
            domain="system",
            chown=True,
        )
    serve(helper, args.socket)


if __name__ == "__main__":
    main()
