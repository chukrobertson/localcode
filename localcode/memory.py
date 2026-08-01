from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .models import Project
from .paths import bundled_mempalace_root, cache_dir, data_dir, palace_path, transcript_dir


@dataclass(slots=True)
class MemoryStatus:
    available: bool
    executable: str = ""
    version: str = ""
    initialized: bool = False
    detail: str = ""


class MemPalaceManager:
    def __init__(self) -> None:
        self._sync_lock = threading.Lock()
        self._queue_lock = threading.Lock()
        self._pending: dict[
            str, tuple[Project, list[Callable[[bool, str], None]], bool]
        ] = {}
        self._sync_thread: threading.Thread | None = None
        self._maintenance_threads: set[threading.Thread] = set()
        self._last_sync: dict[str, float] = {}

    @property
    def managed_venv(self) -> Path:
        return data_dir() / "mempalace" / "venv"

    @property
    def managed_site_packages(self) -> Path:
        return data_dir() / "mempalace" / "site-packages"

    @property
    def managed_home(self) -> Path:
        return data_dir() / "mempalace" / "home"

    def _env_file(self, venv: Path | None = None) -> Path:
        return (venv or self.managed_venv).parent / "mempalace.env"

    def executable(self) -> str | None:
        command = self._command()
        if not command:
            return None
        if command[:2] == [sys.executable, "-c"]:
            return f"{sys.executable} (managed MemPalace)"
        return command[0]

    def status(self) -> MemoryStatus:
        executable = self.executable()
        if not executable:
            return MemoryStatus(False, detail="MemPalace is bundled but not installed yet.")
        result = self._run(["--version"], timeout=20)
        version = (result.stdout or result.stderr).strip()
        initialized = (palace_path() / ".mempalace").exists() or palace_path().exists()
        return MemoryStatus(
            available=result.returncode == 0,
            executable=executable,
            version=version,
            initialized=initialized,
            detail=(result.stderr.strip() if result.returncode else "Ready"),
        )

    def install(self, progress: Callable[[str], None] | None = None) -> MemoryStatus:
        source = bundled_mempalace_root()
        if source is None:
            raise RuntimeError("The bundled MemPalace source checkout is missing.")
        venv = self.managed_venv
        venv.parent.mkdir(parents=True, exist_ok=True)
        if progress:
            progress("Creating an isolated MemPalace environment...")
        create = subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if create.returncode == 0:
            command = [str(venv / "bin" / "python"), "-m", "pip", "install", str(source)]
        else:
            pip_probe = subprocess.run(
                [sys.executable, "-m", "pip", "--version"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if pip_probe.returncode:
                raise RuntimeError(
                    "MemPalace setup needs either python3-venv or python3-pip. "
                    "Install one with: sudo apt install python3-venv python3-pip"
                )
            self.managed_site_packages.mkdir(parents=True, exist_ok=True)
            if progress:
                progress("python3-venv is unavailable; using an isolated package directory...")
            command = [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--upgrade",
                "--target",
                str(self.managed_site_packages),
                str(source),
            ]
        if progress:
            progress("Installing the bundled MemPalace checkout...")
        install = subprocess.run(command, capture_output=True, text=True, timeout=1800, check=False)
        if install.returncode:
            raise RuntimeError((install.stderr or install.stdout).strip()[-8000:])
        if progress:
            progress("MemPalace installed.")

        if create.returncode == 0:
            self._install_gpu_packages(venv, progress)

        return self.status()

    def _install_gpu_packages(
        self, venv: Path, progress: Callable[[str], None] | None = None
    ) -> None:
        try:
            gpu_check = subprocess.run(
                ["nvidia-smi"], capture_output=True, timeout=10, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return
        if gpu_check.returncode:
            return
        if progress:
            progress("NVIDIA GPU detected — installing CUDA ONNX Runtime...")
        use_venv = (venv / "bin" / "pip").is_file()
        pip_cmd = [str(venv / "bin" / "pip")] if use_venv else [
            sys.executable, "-m", "pip", "install", "--target", str(self.managed_site_packages),
        ]
        gpu_install = subprocess.run(
            [*pip_cmd, "onnxruntime-gpu"],
            capture_output=True, text=True, timeout=600, check=False,
        )
        if gpu_install.returncode:
            print("GPU ONNX Runtime install skipped (CUDA toolkit may not be available).")
            return
        env_path = data_dir() / "mempalace" / "mempalace.env"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("MEMPALACE_EMBEDDING_DEVICE=cuda\n")
        if progress:
            progress("CUDA acceleration configured for MemPalace embeddings.")

    def initialize_project(self, project: Project, *, model: str = "") -> tuple[bool, str]:
        if not self.executable():
            return False, "MemPalace is not installed."
        self._ensure_private_directories()
        with self._sync_lock:
            result = self._mine_project(project)
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output[-12000:]

    def recall(
        self,
        query: str,
        project: Project,
        *,
        results: int = 3,
        cancel: threading.Event | None = None,
    ) -> str:
        if not project.memory_enabled or not self.executable() or not palace_path().exists():
            return ""
        result = self._run(
            [
                "--palace",
                str(palace_path()),
                "search",
                query[:2000],
                "--wing",
                wing_name(project),
                "--results",
                str(max(1, min(8, results))),
            ],
            timeout=20,
            cancel=cancel,
        )
        if result.returncode:
            return ""
        return result.stdout.strip()[-16000:]

    def sync_project(self, project: Project, *, force: bool = False) -> tuple[bool, str]:
        if (not project.memory_enabled and not force) or not self.executable():
            return False, "MemPalace is unavailable or disabled."
        with self._sync_lock:
            self._ensure_private_directories()
            project_result = self._mine_project(project)
            transcript_root = transcript_dir(project.id)
            transcript_result = None
            if transcript_root.exists() and any(transcript_root.glob("*.jsonl")):
                transcript_result = self._run(
                    [
                        "--palace",
                        str(palace_path()),
                        "mine",
                        str(transcript_root),
                        "--mode",
                        "convos",
                        "--wing",
                        wing_name(project),
                    ],
                    timeout=1800,
                )
            prune_results = [
                self._run(
                    [
                        "--palace",
                        str(palace_path()),
                        "sync",
                        project.path,
                        "--wing",
                        wing_name(project),
                        "--apply",
                    ],
                    timeout=300,
                )
            ]
            if transcript_root.exists():
                prune_results.append(
                    self._run(
                        [
                            "--palace",
                            str(palace_path()),
                            "sync",
                            str(transcript_root),
                            "--wing",
                            wing_name(project),
                            "--apply",
                        ],
                        timeout=300,
                    )
                )
            all_results = [project_result, *prune_results]
            if transcript_result:
                all_results.append(transcript_result)
            success = all(result.returncode == 0 for result in all_results)
            outputs: list[str] = []
            for result in all_results:
                outputs.extend([result.stdout, result.stderr])
            return success, "\n".join(part for part in outputs if part).strip()[-16000:]

    def sync_in_background(
        self,
        project: Project,
        callback: Callable[[bool, str], None] | None = None,
        *,
        force: bool = False,
    ) -> threading.Thread | None:
        if (not project.memory_enabled and not force) or not self.executable():
            return None
        if not force:
            last = self._last_sync.get(project.id, 0)
            if time.monotonic() - last < 60:
                return None
        with self._queue_lock:
            queued = self._pending.get(project.id)
            callbacks = queued[1] if queued else []
            force = force or (queued[2] if queued else False)
            if callback:
                callbacks.append(callback)
            self._pending[project.id] = (project, callbacks, force)
            if self._sync_thread and self._sync_thread.is_alive():
                return self._sync_thread
            self._sync_thread = threading.Thread(
                target=self._sync_worker, name="mempalace-sync", daemon=True
            )
            self._sync_thread.start()
            return self._sync_thread

    def prune_project(self, project: Project) -> tuple[bool, str]:
        if not self.executable():
            return False, "MemPalace is unavailable."
        with self._sync_lock:
            self._ensure_private_directories()
            results = [
                self._run(
                    [
                        "--palace",
                        str(palace_path()),
                        "sync",
                        project.path,
                        "--wing",
                        wing_name(project),
                        "--apply",
                    ],
                    timeout=300,
                )
            ]
            transcript_root = transcript_dir(project.id)
            if transcript_root.exists():
                results.append(
                    self._run(
                        [
                            "--palace",
                            str(palace_path()),
                            "sync",
                            str(transcript_root),
                            "--wing",
                            wing_name(project),
                            "--apply",
                        ],
                        timeout=300,
                    )
                )
        output = "\n".join(
            part
            for result in results
            for part in (result.stdout, result.stderr)
            if part
        ).strip()
        return all(result.returncode == 0 for result in results), output[-16000:]

    def prune_in_background(
        self,
        project: Project,
        callback: Callable[[bool, str], None] | None = None,
    ) -> threading.Thread | None:
        if not self.executable():
            return None

        def worker() -> None:
            try:
                result = self.prune_project(project)
                if callback:
                    callback(*result)
            finally:
                with self._queue_lock:
                    self._maintenance_threads.discard(threading.current_thread())

        thread = threading.Thread(target=worker, name="mempalace-prune", daemon=True)
        with self._queue_lock:
            self._maintenance_threads.add(thread)
        thread.start()
        return thread

    def has_active_work(self) -> bool:
        with self._queue_lock:
            return bool(
                self._pending
                or (self._sync_thread and self._sync_thread.is_alive())
                or any(thread.is_alive() for thread in self._maintenance_threads)
            )

    def _sync_worker(self) -> None:
        while True:
            with self._queue_lock:
                if not self._pending:
                    self._sync_thread = None
                    return
                _project_id, (project, callbacks, force) = self._pending.popitem()
            try:
                result = self.sync_project(project, force=force)
            except Exception as error:
                result = (False, str(error))
            self._last_sync[project.id] = time.monotonic()
            for callback in callbacks:
                try:
                    callback(*result)
                except Exception:
                    pass

    def _mine_project(self, project: Project) -> subprocess.CompletedProcess[str]:
        return self._run(
            [
                "--palace",
                str(palace_path()),
                "mine",
                project.path,
                "--wing",
                wing_name(project),
            ],
            timeout=1800,
        )

    def _run(
        self,
        arguments: list[str],
        *,
        timeout: int,
        cancel: threading.Event | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = self._command()
        if not command:
            return subprocess.CompletedProcess(arguments, 127, "", "MemPalace is not installed.")
        self._ensure_private_directories()
        env = {key: value for key, value in os.environ.items() if not key.startswith("MEMPAL")}
        env.update(
            {
                "HOME": str(self.managed_home),
                "XDG_CACHE_HOME": str(cache_dir() / "mempalace"),
                "MEMPALACE_PALACE_PATH": str(palace_path()),
                "PYTHONUNBUFFERED": "1",
            }
        )
        env_file = self._env_file()
        if env_file.is_file():
            for line in env_file.read_text().strip().splitlines():
                if "=" in line and not line.startswith("#"):
                    var, _, value = line.partition("=")
                    env[var.strip()] = value.strip()
        env.pop("PYTHONPATH", None)
        try:
            if cancel is not None:
                return self._run_cancellable(command, arguments, env, timeout, cancel)
            return subprocess.run(
                [*command, *arguments],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as error:
            return subprocess.CompletedProcess(
                arguments,
                124,
                self._as_text(error.stdout),
                f"MemPalace timed out after {timeout} seconds.",
            )
        except OSError as error:
            return subprocess.CompletedProcess(arguments, 127, "", str(error))

    def _run_cancellable(
        self,
        command: list[str],
        arguments: list[str],
        env: dict[str, str],
        timeout: int,
        cancel: threading.Event,
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            [*command, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env=env,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                if cancel.is_set():
                    reason = "MemPalace recall cancelled."
                    returncode = 130
                elif time.monotonic() >= deadline:
                    reason = f"MemPalace timed out after {timeout} seconds."
                    returncode = 124
                else:
                    continue
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    stdout, stderr = process.communicate(timeout=2)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    stdout, stderr = process.communicate()
                return subprocess.CompletedProcess(
                    arguments,
                    returncode,
                    stdout,
                    (stderr + "\n" + reason).strip(),
                )

    def _command(self) -> list[str]:
        override = os.environ.get("LOCALCODE_MEMPALACE_BIN")
        if override:
            path = Path(override).expanduser()
            if path.is_file():
                return [str(path)]
        venv_executable = self.managed_venv / "bin" / "mempalace"
        if venv_executable.is_file():
            return [str(venv_executable)]
        if (self.managed_site_packages / "mempalace" / "__init__.py").is_file():
            target = repr(str(self.managed_site_packages))
            bootstrap = (
                "import os,runpy,sys;"
                "os.environ.pop('PYTHONPATH',None);"
                f"sys.path.insert(0,{target});"
                "runpy.run_module('mempalace',run_name='__main__')"
            )
            return [sys.executable, "-c", bootstrap]
        return []

    def _ensure_private_directories(self) -> None:
        for path in (
            data_dir() / "mempalace",
            self.managed_home,
            cache_dir() / "mempalace",
            palace_path().parent,
        ):
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o700)

    @staticmethod
    def _as_text(value: str | bytes | None) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value or ""


def wing_name(project: Project) -> str:
    folder_name = Path(project.path).expanduser().name or project.name
    base = re.sub(r"[^a-z0-9]+", "_", folder_name.casefold()).strip("_")
    suffix = re.sub(r"[^a-z0-9]", "", project.id.casefold())[:8] or "project"
    return f"{base or 'project'}_{suffix}"
