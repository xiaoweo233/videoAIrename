"""内置 AI 服务生命周期管理：llama.cpp 视觉服务 与 Qwen3-ASR 转写服务。

设计原则：**只管理自己拉起的进程**。
如果端口上已经有服务（用户手动开的、或者先前启动留下的），标记为 `external` 并只做健康检查，
绝不去杀别人的进程。

服务启动要加载几 GB 模型，几十秒是正常的，所以状态机区分 `starting` 与 `running`，
启动日志实时推到前端，用户能看到「正在加载模型」而不是干等。
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config as config_mod
from . import paths
from .hub import hub
from .proc import CREATE_NO_WINDOW, _child_env

STOPPED, STARTING, RUNNING, ERROR, EXTERNAL = "stopped", "starting", "running", "error", "external"

LOG_TAIL_LINES = 40
HEALTH_INTERVAL = 2.0


@dataclass
class ServiceState:
    name: str
    label: str
    host: str = "127.0.0.1"
    port: int = 0
    status: str = STOPPED
    pid: int | None = None
    message: str = ""
    started_at: float = 0.0
    log_tail: deque = field(default_factory=lambda: deque(maxlen=LOG_TAIL_LINES))
    endpoint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "host": self.host,
            "port": self.port,
            "status": self.status,
            "pid": self.pid,
            "message": self.message,
            "started_at": self.started_at,
            "endpoint": self.endpoint or f"http://{self.host}:{self.port}",
            "logs": list(self.log_tail),
        }


def port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# 子进程自己崩掉时，日志里那行英文才是真正的原因。这里把它翻译成用户能照做的建议，
# 免得前端只显示一句「returncode=1」让人无从下手。
_CRASH_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("failed to allocate memory for prompt cache", "bad allocation"),
     "内存不足（提示词缓存分配失败）：请把「提示词缓存上限」调到 512–1024，或关掉另一个 AI 服务再试"),
    (("out of memory", "cuda error: out of memory", "cudamalloc failed"),
     "显存不足：请把「并发槽数」设为 1、「上下文长度」降到 4096，「卸载到显卡的层数」调小"),
    (("address already in use", "couldn't bind", "bind failed"),
     "端口被占用：请在设置里换一个端口"),
    (("error loading model", "failed to load model", "unknown model architecture"),
     "模型文件加载失败：请确认模型路径正确、下载完整（可点「补齐 AI 依赖与模型」校验）"),
    (("no such file or directory", "the system cannot find the file"),
     "缺少文件：请检查程序/模型路径，或点「补齐 AI 依赖与模型」下载"),
    (("cuda error", "no cuda-capable device", "cuda driver version is insufficient"),
     "CUDA 异常：请更新显卡驱动，或在设置里关闭「自动补 CUDA 库」改走 CPU"),
    (("outofmemoryerror", "torch.outofmemoryerror"),
     "PyTorch 显存不足：转写服务和视觉模型同时占卡了，先停掉另一个服务再跑"),
)


def crash_hint(log_tail) -> str:
    """从日志尾部找已知的崩溃原因，返回中文建议；找不到就返回空串。"""
    tail = "\n".join(log_tail or ()).lower()
    if not tail:
        return ""
    for needles, hint in _CRASH_HINTS:
        if any(needle in tail for needle in needles):
            return hint
    return ""


class ServiceManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._procs: dict[str, subprocess.Popen] = {}
        self._states: dict[str, ServiceState] = {}
        self._monitor: threading.Thread | None = None

    # ---------- 配置 ----------

    def _cfg(self) -> dict:
        return config_mod.load().get("services") or {}

    def _state(self, name: str) -> ServiceState:
        with self._lock:
            if name not in self._states:
                self._states[name] = ServiceState(name=name, label=name)
            return self._states[name]

    def _sync_from_config(self) -> None:
        cfg = self._cfg()
        llama = cfg.get("llama") or {}
        asr = cfg.get("asr") or {}
        with self._lock:
            llama_state = self._state("llama")
            llama_state.label = "视觉大模型 (llama.cpp)"
            llama_state.host = str(llama.get("host") or "127.0.0.1")
            llama_state.port = int(llama.get("port") or 8084)
            asr_state = self._state("asr")
            asr_state.label = "语音转写 (Qwen3-ASR)"
            asr_state.host = str(asr.get("host") or "127.0.0.1")
            asr_state.port = int(asr.get("port") or 7867)

    # ---------- 命令构建 ----------

    def _llama_command(self) -> tuple[list[str], str, dict[str, str]]:
        cfg = self._cfg().get("llama") or {}
        binary = paths.abs_path(str(cfg.get("bin") or ""))
        model = paths.abs_path(str(cfg.get("model") or ""))
        mmproj = paths.abs_path(str(cfg.get("mmproj") or ""))

        if not binary.is_file():
            raise FileNotFoundError(
                f"未找到 llama-server：{binary}\n请点击「补齐 AI 依赖与模型」下载。")
        if not model.is_file():
            raise FileNotFoundError(
                f"未找到视觉模型：{model}\n请点击「补齐 AI 依赖与模型」下载。")

        args = [
            str(binary),
            "-m", str(model),
            "--host", str(cfg.get("host") or "127.0.0.1"),
            "--port", str(int(cfg.get("port") or 8084)),
            "-c", str(int(cfg.get("ctx_size") or 8192)),
            "-ngl", str(int(cfg.get("gpu_layers") or 99)),
        ]
        # 并发槽数直接决定 KV cache 占用：4 个槽 = 4 份上下文显存。
        # 实测 9B + 4 槽 + 8k 上下文会在 11GB 卡上 OOM，所以显式传 -np。
        parallel = int(cfg.get("parallel") or 1)
        if parallel > 0:
            args += ["-np", str(parallel)]
        # 提示词缓存默认要 8GB 系统内存，16GB 内存的机器上和模型抢不过就 bad allocation。
        cache_ram = int(cfg.get("cache_ram", 1024) or 0)
        if cache_ram > 0:
            args += ["--cache-ram", str(cache_ram)]
        if mmproj.is_file():
            args += ["--mmproj", str(mmproj)]
        extra = str(cfg.get("extra_args") or "").strip()
        if extra:
            args += extra.split()

        return args, str(binary.parent), {}

    def _asr_command(self) -> tuple[list[str], str, dict[str, str]]:
        cfg = self._cfg().get("asr") or {}
        root = paths.abs_path(str(cfg.get("dir") or "../Qwen3_ASR"))
        app_dir = root / "app"
        if not (app_dir / "main.py").is_file():
            raise FileNotFoundError(
                f"未找到 Qwen3-ASR 项目：{app_dir}\n"
                "请在配置里把 services.asr.dir 指向 Qwen3_ASR 所在目录。")

        python = self._find_asr_python(root, str(cfg.get("python") or ""))
        if not python:
            raise FileNotFoundError(
                f"未找到 Qwen3-ASR 的 Python 解释器（在 {root} 下找过 WPy64-*/python/python.exe）")

        asr_model = paths.abs_path(str(cfg.get("asr_model") or "")) \
            if cfg.get("asr_model") else paths.ASR_MODEL_DIR
        aligner = paths.abs_path(str(cfg.get("aligner_model") or "")) \
            if cfg.get("aligner_model") else paths.ALIGNER_MODEL_DIR

        if not asr_model.is_dir():
            raise FileNotFoundError(
                f"未找到 Qwen3-ASR 模型目录：{asr_model}\n请点击「补齐 AI 依赖与模型」下载。")

        args = [
            str(python), "main.py",
            "--asr-checkpoint", str(asr_model),
            "--aligner-checkpoint", str(aligner),
            "--backend", "transformers",
            "--backend-kwargs", '{"max_inference_batch_size": 1,"max_new_tokens": 1024}',
            "--ip", str(cfg.get("host") or "127.0.0.1"),
            "--port", str(int(cfg.get("port") or 7867)),
        ]
        extra = str(cfg.get("extra_args") or "").strip()
        if extra:
            args += extra.split()

        # 便携 Python 需要自己的 Scripts 与项目 bin 在 PATH 上
        python_dir = python.parent
        env = {
            "HF_HUB_OFFLINE": "1",
            "PATH": os.pathsep.join([
                str(python_dir), str(python_dir / "Scripts"),
                str(root / "bin"), os.environ.get("PATH", ""),
            ]),
        }
        return args, str(app_dir), env

    @staticmethod
    def _find_asr_python(root: Path, configured: str) -> Path | None:
        if configured:
            candidate = paths.abs_path(configured)
            if candidate.is_file():
                return candidate
        for pattern in ("WPy64-*/python/python.exe", "*/python/python.exe", "python.exe"):
            for found in root.glob(pattern):
                if found.is_file():
                    return found
        return None

    # ---------- 健康检查 ----------

    def _health(self, name: str) -> tuple[bool, str]:
        state = self._state(name)
        import httpx

        base = f"http://{state.host}:{state.port}"
        try:
            if name == "llama":
                resp = httpx.get(base + "/v1/models", timeout=3)
                if resp.status_code < 400:
                    try:
                        count = len(resp.json().get("data") or [])
                    except ValueError:
                        count = 0
                    return True, f"已就绪（{count} 个模型）" if count else "已就绪"
                return False, f"HTTP {resp.status_code}"
            resp = httpx.get(base + "/config", timeout=3)
            if resp.status_code < 400:
                return True, "已就绪"
            return False, f"HTTP {resp.status_code}"
        except Exception:                           # noqa: BLE001
            return False, ""

    # ---------- 状态刷新 ----------

    def refresh(self) -> dict[str, ServiceState]:
        """刷新所有服务状态。会做一次网络探测，别在热路径里调。"""
        self._sync_from_config()
        for name in ("llama", "asr"):
            state = self._state(name)
            with self._lock:
                proc = self._procs.get(name)
                alive = proc is not None and proc.poll() is None
                reachable = port_open(state.host, state.port)
                healthy, message = self._health(name) if reachable else (False, "")

                if healthy:
                    state.status = RUNNING if alive else EXTERNAL
                    state.message = message if alive else message + "（外部进程）"
                    if not alive:
                        state.pid = None
                elif alive:
                    state.status = STARTING
                    state.message = "启动中，正在加载模型…"
                elif proc is not None:
                    code = proc.returncode
                    state.status = ERROR
                    state.message = f"进程已退出（returncode={code}）"
                    hint = crash_hint(state.log_tail)
                    if hint:
                        state.message += f" · {hint}"
                    state.pid = None
                else:
                    state.status = STOPPED
                    state.message = "未运行"
                    state.pid = None
        return dict(self._states)

    def status(self) -> dict[str, Any]:
        self._sync_from_config()
        with self._lock:
            return {name: state.as_dict() for name, state in self._states.items()}

    # ---------- 控制 ----------

    def start(self, name: str) -> dict[str, Any]:
        if name not in ("llama", "asr"):
            return {"ok": False, "message": f"未知服务：{name}"}

        self._sync_from_config()
        state = self._state(name)

        if port_open(state.host, state.port):
            healthy, message = self._health(name)
            if healthy:
                return {"ok": True, "message": f"{state.label} 已在运行（{message}）"}
            return {"ok": False,
                    "message": f"端口 {state.port} 已被占用，但不是可用的 {state.label} 服务"}

        try:
            args, cwd, env_extra = self._llama_command() if name == "llama" else self._asr_command()
        except FileNotFoundError as exc:
            with self._lock:
                state.status = ERROR
                state.message = str(exc)
            return {"ok": False, "message": str(exc)}

        hub.info(f"正在启动 {state.label} · {state.host}:{state.port}")
        try:
            proc = subprocess.Popen(
                args,
                cwd=cwd,
                env=_child_env(env_extra),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            with self._lock:
                state.status = ERROR
                state.message = f"启动失败：{exc}"
            return {"ok": False, "message": f"启动失败：{exc}"}

        with self._lock:
            self._procs[name] = proc
            state.pid = proc.pid
            state.status = STARTING
            state.message = "启动中，正在加载模型…"
            state.started_at = time.time()
            state.log_tail.clear()

        threading.Thread(target=self._pump_logs, args=(name, proc), daemon=True,
                         name=f"vair-log-{name}").start()
        self._ensure_monitor()
        self._broadcast(name)
        return {"ok": True, "message": f"{state.label} 正在启动，模型加载需要一些时间"}

    def stop(self, name: str) -> dict[str, Any]:
        with self._lock:
            proc = self._procs.get(name)
            state = self._state(name)

        if proc is None or proc.poll() is not None:
            if state.status == EXTERNAL:
                return {"ok": False,
                        "message": f"{state.label} 是外部启动的进程，本程序不会去终止它"}
            return {"ok": False, "message": f"{state.label} 当前没有由本程序启动的进程"}

        hub.info(f"正在停止 {state.label}…")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

        with self._lock:
            self._procs.pop(name, None)
            state.status = STOPPED
            state.pid = None
            state.message = "已停止"
        self._broadcast(name)
        return {"ok": True, "message": f"{state.label} 已停止"}

    def restart(self, name: str) -> dict[str, Any]:
        self.stop(name)
        time.sleep(0.6)
        return self.start(name)

    def stop_all(self) -> None:
        for name in list(self._procs):
            try:
                self.stop(name)
            except Exception:                       # noqa: BLE001
                pass

    # ---------- 日志与监视 ----------

    def _pump_logs(self, name: str, proc: subprocess.Popen) -> None:
        """把子进程输出按行读进环形缓冲并推 SSE。"""
        state = self._state(name)
        stream = proc.stdout
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                text = (line or "").rstrip()
                if not text:
                    continue
                with self._lock:
                    state.log_tail.append(text)
                hub.debug(f"[{name}] {text}")
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _ensure_monitor(self) -> None:
        if self._monitor is not None and self._monitor.is_alive():
            return
        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True,
                                         name="vair-service-monitor")
        self._monitor.start()

    def _monitor_loop(self) -> None:
        """后台轮询服务状态，状态变化时推 SSE（前端不用一直问）。"""
        last: dict[str, str] = {}
        while True:
            time.sleep(HEALTH_INTERVAL)
            try:
                states = self.refresh()
            except Exception:                       # noqa: BLE001
                continue
            for name, state in states.items():
                if last.get(name) != state.status:
                    last[name] = state.status
                    self._broadcast(name)
            # 没有任何托管进程且都停了，就别再轮询了（下次 start 会重新拉起）
            with self._lock:
                has_proc = any(p.poll() is None for p in self._procs.values())
            if not has_proc and not any(s.status in (STARTING, RUNNING) for s in states.values()):
                self._monitor = None
                return

    def _broadcast(self, name: str) -> None:
        with self._lock:
            state = self._states.get(name)
            payload = state.as_dict() if state else {}
        hub.publish("service", payload)

    # ---------- 启动时自动拉起 ----------

    def _wait_ready(self, name: str, timeout: float = 240.0) -> bool:
        """等到服务真正就绪（或失败）。模型加载要几十秒，必须等。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.refresh().get(name)
            if state is None:
                return False
            if state.status in (RUNNING, EXTERNAL):
                return True
            if state.status == ERROR:
                return False
            time.sleep(2.0)
        return False

    def ensure_running(self) -> None:
        """按顺序拉起：先视觉、后转写，**前一个就绪了才起下一个**。

        为什么必须串行：两个服务都要往同一块显卡里塞模型。同时启动的话，
        谁先抢到显存谁活，另一个直接 CUDA out of memory 崩掉——而崩溃方是随机的。
        串行之后失败方一定是后起的转写服务，视觉这条主链路永远能起来。
        """
        cfg = self._cfg()
        for name in ("llama", "asr"):
            section = cfg.get(name) or {}
            if not section.get("enable", True):
                continue
            # 已在跑（外部或残留）就别重复起
            self._sync_from_config()
            state = self._state(name)
            if port_open(state.host, state.port):
                hub.debug(f"{state.label} 已在 {state.host}:{state.port} 上运行，跳过启动")
                continue
            result = self.start(name)
            if not result.get("ok"):
                hub.warn(f"{state.label} 自动启动失败：{result.get('message')}")
                continue
            if self._wait_ready(name):
                hub.info(f"{state.label} 已就绪")
                continue
            state = self._state(name)
            hint = crash_hint(state.log_tail)
            hub.warn(f"{state.label} 自动启动未成功：{state.message}"
                     + (f"（{hint}）" if hint else "")
                     + "。可在设置 → AI 服务里先停掉另一个服务再单独启动它。")

    def autostart(self) -> None:
        if not self._cfg().get("autostart", True):
            hub.debug("已关闭 AI 服务自动拉起")
            return
        self.ensure_running()

    def stop_after_task(self) -> None:
        """整批任务落盘结束后释放 AI 服务（只停本程序拉起的进程）。"""
        if not self._cfg().get("stop_after_task", True):
            return
        with self._lock:
            owned = [name for name, proc in self._procs.items()
                     if proc is not None and proc.poll() is None]
        if not owned:
            return
        hub.info("任务已结束，正在关闭 AI 服务以释放显存…")
        for name in owned:
            try:
                self.stop(name)
            except Exception:                       # noqa: BLE001
                pass
        hub.info("AI 服务已关闭 · 下次开始任务会自动重新拉起（可在设置里关掉这个行为）")


services = ServiceManager()
