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
  Its environment (the app's API token) goes into a file only root can read,
  <base>/env/<label>.env, which /bin/sh sources before running the program:
  a plist, and launchctl print, show the environment to every account.
* copy: copy files root may read but the user may not (an app's data, e.g.
  OTBR's Thread credentials) between folders in the user's home.
* user_service / user_bootstrap / user_kickstart / user_bootout /
  user_unservice: manage LaunchDaemons "<prefix>.supervisor", "<prefix>.core"
  and "<prefix>.app.<slug>" that run as the Supervisor's user, not as root.
  They let Home Assistant run with nobody logged in: a LaunchAgent lives only
  as long as the user's GUI session. The user chooses what they run, as with
  a LaunchAgent, and they get nothing the user lacks: root only writes the
  plist and loads it. launchd starts /bin/sh as the user, which reads the
  environment from a file only the user can read (a plist, and launchctl
  print, show the environment to every account: it holds API tokens) and
  opens the logs itself, as the user.
  These verbs never touch a plist that is not a user service of this user,
  nor a loaded job that runs as anyone else, so they cannot replace, stop or
  remove a root daemon.

Runs on Apple's /usr/bin/python3 (3.9, standard library only): an interpreter
the user cannot modify. Test mode (--test DIR) runs unprivileged against the
user's launchd domain and a scratch folder.
"""

import argparse
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time

VERSION = 3
RE_SLUG = re.compile(r"^[a-z0-9_]{1,64}$")
RE_VERSION = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
RE_PREFIX = re.compile(r"^ha-macos(-[a-z0-9]{1,16})?$")
RE_ENV_KEY = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
RE_USER_SERVICE = re.compile(r"^(supervisor|core|app\.[a-z0-9_]{1,64})$")
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# What a root app may get from the Supervisor's environment, and nothing else:
# variables such as BASH_ENV, ENV or SHELLOPTS make a root shell run user code.
ROOT_ENV_KEYS = ("SUPERVISOR", "HASSIO", "SUPERVISOR_TOKEN", "HASSIO_TOKEN", "TZ")
ROOT_ENV_PREFIXES = ("APP_", "PORT_")
# A user service's program: sh, as the user, sources the environment file and
# runs the program with its output appended to the logs. A POSIX sh exits as
# soon as "." fails, so the file is checked first: exit 78 (EX_CONFIG) says so.
_SOURCE_ENV = (
    '[ -r "$1" ] || { echo "$0: cannot read $1" >&2; exit 78; }; '
    'set -a; . "$1"; set +a; '
)
USER_WRAPPER = (
    'out=$2; err=$3; exec >>"$out" 2>>"$err"; ' + _SOURCE_ENV + 'shift 3; exec "$@"'
)
# A root service's program: sh, as root, sources the root-only environment file
# the helper wrote and runs the program (launchd opens the log).
ROOT_WRAPPER = _SOURCE_ENV + 'shift; exec "$@"'


class Refused(Exception):
    """A request the helper will not carry out."""


class Helper:
    def __init__(self, uid, base, daemons, domain, chown):
        self.uid = uid
        self.user = _user_name(uid)
        self.home = os.path.realpath(os.path.expanduser("~%s" % self.user))
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
        if not RE_PREFIX.fullmatch(prefix or ""):
            raise Refused("bad prefix")
        if not RE_SLUG.fullmatch(slug or ""):
            raise Refused("bad slug")
        root = os.path.join(self.base, "apps", prefix, slug)
        if version is None:
            return root
        if not RE_VERSION.fullmatch(version or ""):
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
            return {
                "loaded": False,
                "running": False,
                "pid": None,
                "exit": None,
                "user": None,
            }
        state = re.search(r"^\s*state = (\S+)", text, re.M)
        pid = re.search(r"^\s*pid = (\d+)", text, re.M)
        exit_code = re.search(r"^\s*last exit code = (-?\d+)", text, re.M)
        user = re.search(r"^\s*username = (\S+)", text, re.M)
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
            # Only jobs with a UserName show one; root daemons show none.
            "user": user.group(1) if user else None,
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
            if (
                not RE_ENV_KEY.fullmatch(key)
                or not isinstance(value, str)
                or "\0" in value
            ):
                raise Refused("bad environment")
            # Never let the user account choose what root loads or runs: only
            # the Supervisor's own variables pass (no DYLD_*, BASH_ENV, ...).
            if key in ROOT_ENV_KEYS or key.startswith(ROOT_ENV_PREFIXES):
                env[key] = value
        args = req.get("args") or []
        if not all(isinstance(arg, str) and "\0" not in arg for arg in args):
            raise Refused("bad arguments")
        workdir = self._inside_home(req["working_directory"])
        log = self._inside_home(req["log_file"])
        label = self._label(prefix, slug)
        # The variables (API tokens among them) go into a file only root can
        # read: a plist, and launchctl print, show its environment to every
        # account. The plist keeps only PATH and HOME.
        env_file = self._write_root_env(label, env)
        data = {
            "Label": label,
            "ProgramArguments": ["/bin/sh", "-c", ROOT_WRAPPER, label, env_file]
            + [program]
            + args,
            "EnvironmentVariables": {"PATH": SYSTEM_PATH, "HOME": root},
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
        label = self._label(req["prefix"], req["slug"])
        path = self._plist(label)
        if os.path.exists(path):
            os.remove(path)
        env_file = self._root_env_file(label)
        if os.path.lexists(env_file):
            os.remove(env_file)
        return {}

    # Root services' environment files

    def _root_env_dir(self):
        """Return base/env, a folder only root can open (refuse anything else)."""
        path = os.path.join(self.base, "env")
        os.makedirs(path, mode=0o700, exist_ok=True)
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode):
            raise Refused("%s is not a folder" % path)
        if self.chown and info.st_uid != 0:
            raise Refused("%s is not root's" % path)
        if info.st_mode & 0o077:
            os.chmod(path, 0o700)
        return path

    def _root_env_file(self, label):
        return os.path.join(self.base, "env", label + ".env")

    def _write_root_env(self, label, env):
        """Write env as sh assignments, readable by root only; return the path."""
        path = os.path.join(self._root_env_dir(), label + ".env")
        tmp = path + ".tmp"
        if os.path.lexists(tmp):
            os.remove(tmp)
        text = "".join(
            "%s=%s\n" % (key, shlex.quote(value)) for key, value in sorted(env.items())
        )
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(text)
        os.chmod(tmp, 0o600)
        os.rename(tmp, path)
        return path

    # User services

    def _user_label(self, prefix, name):
        if not RE_PREFIX.fullmatch(prefix or ""):
            raise Refused("bad prefix")
        if not RE_USER_SERVICE.fullmatch(name or ""):
            raise Refused("bad service name")
        return "%s.%s" % (prefix, name)

    def _read_plist(self, path):
        try:
            with open(path, "rb") as file:
                return plistlib.load(file)
        except FileNotFoundError:
            return None

    def _is_user_service(self, data, label):
        return (
            isinstance(data, dict)
            and data.get("Label") == label
            and data.get("UserName") == self.user
        )

    def _user_service(self, req):
        """Return (label, plist path) of a defined user service, or refuse."""
        label = self._user_label(req["prefix"], req["name"])
        path = self._plist(label)
        if not self._is_user_service(self._read_plist(path), label):
            raise Refused("%s is not a user service" % label)
        return label, path

    def _require_user_job(self, label):
        """Refuse if label is loaded and runs as anyone but the user."""
        status = self._status(label)
        if status["loaded"] and status["user"] != self.user:
            raise Refused("%s runs as %s" % (label, status["user"] or "root"))
        return status

    def cmd_user_service(self, req):
        label = self._user_label(req["prefix"], req["name"])
        path = self._plist(label)
        existing = self._read_plist(path)
        if existing is not None and not self._is_user_service(existing, label):
            raise Refused("%s exists and is not a user service" % label)
        # A root job loaded under this label (its plist gone) would be
        # replaced by this plist at the next boot.
        self._require_user_job(label)
        program = req.get("program")
        if (
            not isinstance(program, list)
            or not program
            or not all(isinstance(arg, str) and "\0" not in arg for arg in program)
        ):
            raise Refused("bad program")
        # Where the program lives doesn't matter: it runs as the user, who can
        # run anything as themselves (a venv's python links out of the home).
        if not os.path.isabs(program[0]) or not os.access(program[0], os.X_OK):
            raise Refused("program must be an absolute path to an executable")
        # The user's sh opens these, not root: inside the home for tidiness.
        env_file = self._inside_home(req["env_file"])
        stdout = self._inside_home(req["stdout"])
        stderr = self._inside_home(req.get("stderr") or stdout)
        data = {
            "Label": label,
            "UserName": self.user,
            "ProgramArguments": ["/bin/sh", "-c", USER_WRAPPER, label, env_file]
            + [stdout, stderr]
            + program,
            "EnvironmentVariables": {"HOME": self.home},
            "WorkingDirectory": self._inside_home(req["working_directory"]),
            "RunAtLoad": bool(req.get("run_at_load")),
            "KeepAlive": bool(req.get("keep_alive")),
            # Without a ProcessType launchd throttles a job's CPU and I/O
            # (man launchd.plist); Interactive is what apps get: no limits.
            "ProcessType": "Interactive",
        }
        throttle = req.get("throttle_interval")
        if throttle is not None:
            if (
                not isinstance(throttle, int)
                or isinstance(throttle, bool)
                or not 1 <= throttle <= 3600
            ):
                raise Refused("bad throttle interval")
            data["ThrottleInterval"] = throttle
        os.makedirs(self.daemons, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as file:
            plistlib.dump(data, file)
        os.chmod(tmp, 0o644)
        os.rename(tmp, path)
        return {"label": label}

    def cmd_user_bootstrap(self, req):
        label, path = self._user_service(req)
        if self._require_user_job(label)["loaded"]:
            return {}
        code, text = self._launchctl("bootstrap", self.domain, path)
        if code != 0:
            raise Refused("launchctl bootstrap: %s" % text.strip())
        return {}

    def cmd_user_kickstart(self, req):
        label, _ = self._user_service(req)
        if not self._require_user_job(label)["loaded"]:
            raise Refused("%s is not loaded" % label)
        code, text = self._launchctl("kickstart", "%s/%s" % (self.domain, label))
        if code != 0:
            raise Refused("launchctl kickstart: %s" % text.strip())
        return {}

    def cmd_user_bootout(self, req):
        """Unload; the Supervisor stops the process first (it runs as the user)."""
        label = self._user_label(req["prefix"], req["name"])
        if not self._require_user_job(label)["loaded"]:
            return {}
        self._launchctl("bootout", "%s/%s" % (self.domain, label))
        return {}

    def cmd_user_unservice(self, req):
        label = self._user_label(req["prefix"], req["name"])
        path = self._plist(label)
        data = self._read_plist(path)
        if data is not None and not self._is_user_service(data, label):
            raise Refused("%s is not a user service" % label)
        if self._require_user_job(label)["loaded"]:
            self._launchctl("bootout", "%s/%s" % (self.domain, label))
            deadline = time.monotonic() + 30
            while self._status(label)["loaded"]:
                if time.monotonic() >= deadline:
                    raise Refused("%s is still loaded; plist kept" % label)
                time.sleep(0.5)
        if data is not None:
            os.remove(path)
        return {}

    def _open_dir(self, path, create=False):
        """Open a folder inside the home as a descriptor, following no links.

        Each component is opened from the home down with O_NOFOLLOW, so a
        symlink put anywhere on the way (also after the check) makes this
        fail instead of leading root out of the home. Folders it creates
        belong to the user.
        """
        real = self._inside_home(path)
        parts = [p for p in os.path.relpath(real, self.home).split(os.sep) if p != "."]
        fd = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, 0o755, dir_fd=fd)
                        if self.chown:
                            os.chown(part, self.uid, -1, dir_fd=fd, follow_symlinks=False)
                    except FileExistsError:
                        pass
                try:
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                except OSError as err:
                    raise Refused("%s: %s" % (path, err.strerror))
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    def cmd_copy(self, req):
        src_dir = self._open_dir(req["source"])
        try:
            dst_dir = self._open_dir(req["dest"], create=True)
        except BaseException:
            os.close(src_dir)
            raise
        try:
            for name in os.listdir(src_dir):
                self._copy_file(src_dir, dst_dir, name)
        finally:
            os.close(src_dir)
            os.close(dst_dir)
        return {}

    def _copy_file(self, src_dir, dst_dir, name):
        """Copy one regular file; skip links and anything else (copy2 rules).

        An existing destination keeps its owner (the Supervisor creates empty
        files first so that the copies stay readable to it); a new one is
        root's. Links at either end are never followed.
        """
        flags = os.O_NOFOLLOW | os.O_NONBLOCK  # no links; a FIFO never blocks
        try:
            src = os.open(name, os.O_RDONLY | flags, dir_fd=src_dir)
        except OSError:
            return  # a symlink (ELOOP) or gone
        try:
            info = os.fstat(src)
            # A hard link could name any file root may read: copy only files
            # with a single name.
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return
            try:
                dst = os.open(name, os.O_WRONLY | os.O_CREAT | flags, 0o600, dir_fd=dst_dir)
            except OSError as err:
                raise Refused("cannot write %s: %s" % (name, err.strerror))
            try:
                # Truncate only once it is known to be a plain file of its own:
                # a hard link there would make root overwrite its other name.
                target = os.fstat(dst)
                if not stat.S_ISREG(target.st_mode) or target.st_nlink != 1:
                    raise Refused("%s is not a plain file" % name)
                os.ftruncate(dst, 0)
                while True:
                    chunk = os.read(src, 1 << 20)
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(dst, view):]
                # Never setuid, setgid or sticky: root writes this file.
                os.fchmod(dst, stat.S_IMODE(info.st_mode) & 0o777)
                os.utime(dst, ns=(info.st_atime_ns, info.st_mtime_ns))
            finally:
                os.close(dst)
        finally:
            os.close(src)

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
                    request.get("slug") or request.get("name", ""),
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
