import asyncio
import os
import random
import socket
import subprocess
from contextlib import suppress


def port_available(port):
    with socket.socket() as sock:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


class InstanceLock:
    def __init__(self, path):
        self.path, self.file = path, None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+b")
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            raise RuntimeError("worker_already_running") from None
        self.file = f

    def release(self):
        if self.file:
            self.file.close()
            self.file = None


class ManagedProcess:
    def __init__(self, command, cwd, job, env=None, diagnostics=None):
        self.command, self.cwd, self.job, self.env = command, cwd, job, env
        self.process = None
        self.diagnostics = diagnostics
        self.failures, self.next_start, self.started_at = 0, 0.0, 0.0

    @property
    def running(self):
        return bool(self.process and self.process.returncode is None)

    async def tick(self):
        now = asyncio.get_running_loop().time()
        if self.running:
            if now - self.started_at >= 60:
                self.failures = 0
            return
        if self.process:
            exit_code = self.process.returncode
            self.failures += 1
            self.next_start = now + min(60, 2 ** min(self.failures - 1, 6)) + random.uniform(0, .5)
            if self.diagnostics:
                self.diagnostics.emit("tunnel_exited", exit_code=exit_code, failures=self.failures, retry_seconds=round(self.next_start-now, 3))
            self.process = None
        if now < self.next_start:
            return
        try:
            self.process = await asyncio.create_subprocess_exec(*self.command, cwd=str(self.cwd), env=self.env,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.job.assign(self.process.pid)
            self.started_at = now
            if self.diagnostics: self.diagnostics.emit("tunnel_started")
        except (OSError, ValueError) as exc:
            await self.stop()
            self.failures += 1
            self.next_start = now + min(60, 2 ** min(self.failures - 1, 6)) + random.uniform(0, .5)
            if self.diagnostics:
                from .runtime_diagnostics import exception_info
                self.diagnostics.emit("tunnel_start_failed", **exception_info(exc), retry_seconds=round(self.next_start-now, 3))

    async def stop(self):
        if self.running:
            with suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
            if self.diagnostics: self.diagnostics.emit("tunnel_stopped", exit_code=self.process.returncode)
        self.process = None
