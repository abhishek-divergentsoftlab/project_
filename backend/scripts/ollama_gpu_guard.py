"""Make sure the chat model sits fully on the GPU (standard library only).

On this unified-memory box, loading the model while Linux's page cache is full makes CUDA report little free
memory, and llama.cpp then moves the MoE experts to the CPU (``ollama ps`` shows "83%/17% CPU/GPU", 6 tok/s
instead of 50). Dropping the model files from the page cache first fixes it, without root:

    unload (keep_alive 0) -> posix_fadvise(DONTNEED) on every blob -> reload with the app's num_ctx and
    keep_alive 24h -> verify /api/ps size_vram == size

Behaviour: loaded and fully on GPU -> nothing; loaded but split -> fix; not loaded -> drop cache, then load.
``--dry-run`` only reports what it would do. Exit status 0 when the model ends up fully on the GPU (or dry run),
1 otherwise. Called by start.sh once Ollama answers.

    python scripts/ollama_gpu_guard.py [--dry-run] [--model M] [--num-ctx N] [--base URL]
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DEFAULT_BLOBS = "/usr/share/ollama/.ollama/models/blobs"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip("'\"")
    except OSError:
        pass
    return values


def call(base: str, path: str, body: dict | None = None, timeout: float = 15.0) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base.rstrip("/") + path, data=data, headers={"Content-Type": "application/json"} if data else {}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local Ollama
        return json.loads(resp.read() or b"{}")


def loaded_state(base: str, model: str) -> tuple[bool, int, int]:
    """(loaded, size, size_vram) of ``model`` from /api/ps."""
    for entry in call(base, "/api/ps").get("models", []):
        if model in (entry.get("name"), entry.get("model")):
            return True, int(entry.get("size", 0)), int(entry.get("size_vram", 0))
    return False, 0, 0


def fully_on_gpu(size: int, vram: int) -> bool:
    return size > 0 and vram >= size


def drop_page_cache(blobs: str) -> tuple[int, int]:
    """posix_fadvise(DONTNEED) on every readable file under ``blobs``; returns (files, bytes)."""
    files = total = 0
    try:
        names = sorted(os.listdir(blobs))
    except OSError as exc:
        print(f"  cannot list {blobs}: {exc}")
        return 0, 0
    for name in names:
        path = os.path.join(blobs, name)
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            continue
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            files += 1
            total += os.fstat(fd).st_size
        except OSError:
            pass
        finally:
            os.close(fd)
    return files, total


def unload(base: str, model: str) -> None:
    call(base, "/api/generate", {"model": model, "keep_alive": 0}, timeout=60)
    for _ in range(30):
        if not loaded_state(base, model)[0]:
            return
        time.sleep(1)


def load(base: str, model: str, num_ctx: int, timeout: float) -> None:
    options = {"num_ctx": num_ctx} if num_ctx > 0 else {}
    call(base, "/api/generate", {"model": model, "prompt": "", "keep_alive": "24h", "options": options}, timeout=timeout)


def main(argv: list[str] | None = None) -> int:
    env = {**read_env(BACKEND / ".env"), **os.environ}
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="report the decision, change nothing")
    parser.add_argument("--base", default=env.get("OLLAMA_BASE_URL", "http://localhost:11434"))
    parser.add_argument("--model", default=env.get("AI_CHAT_MODEL") or env.get("OLLAMA_MODEL") or "gpt-oss:120b")
    parser.add_argument("--num-ctx", type=int, default=int(env.get("AI_CHAT_NUM_CTX") or 0))
    parser.add_argument("--blobs", default=env.get("OLLAMA_BLOBS_DIR", DEFAULT_BLOBS))
    parser.add_argument("--load-timeout", type=float, default=300.0)
    args = parser.parse_args(argv)

    try:
        loaded, size, vram = loaded_state(args.base, args.model)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"ollama-gpu-guard: Ollama not reachable at {args.base} ({exc})")
        return 1

    if loaded and fully_on_gpu(size, vram):
        print(f"ollama-gpu-guard: OK {args.model} fully on GPU ({vram / 1e9:.1f} of {size / 1e9:.1f} GB)")
        return 0

    if loaded:
        pct = 100 * vram / size if size else 0
        action = f"split ({pct:.0f}% on GPU): unload, drop page cache, reload"
    else:
        action = "not loaded: drop page cache, then load"
    if args.dry_run:
        print(f"ollama-gpu-guard: DRY-RUN {args.model} is {action}")
        return 0

    print(f"ollama-gpu-guard: {args.model} is {action}")
    try:
        if loaded:
            unload(args.base, args.model)
        files, total = drop_page_cache(args.blobs)
        print(f"  page cache dropped for {files} files ({total / 1e9:.1f} GB)")
        load(args.base, args.model, args.num_ctx, args.load_timeout)
        loaded, size, vram = loaded_state(args.base, args.model)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"ollama-gpu-guard: FAILED ({exc})")
        return 1

    if loaded and fully_on_gpu(size, vram):
        print(f"ollama-gpu-guard: FIXED {args.model} fully on GPU ({vram / 1e9:.1f} of {size / 1e9:.1f} GB)")
        return 0
    pct = 100 * vram / size if size else 0
    print(f"ollama-gpu-guard: STILL SPLIT {args.model} loaded={loaded} {pct:.0f}% on GPU")
    return 1


if __name__ == "__main__":
    sys.exit(main())
