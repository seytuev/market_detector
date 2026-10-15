"""Run the transferred TypeScript algorithms; cached calls never use a network."""
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import subprocess
from .contracts import ResearchError, encode

RUNTIME = Path(__file__).with_name("terminal-runtime.cjs")


@lru_cache(maxsize=256)
def _execute(payload):
    node = os.environ.get("RESEARCH_NODE_PATH") or shutil.which("node")
    if not node or not RUNTIME.is_file():
        raise ResearchError("terminal_runtime_unavailable", "Модуль STVTerminal не собран или Node.js недоступен", 503)
    try:
        result = subprocess.run([node,str(RUNTIME)], input=payload+"\n", text=True, encoding="utf-8",
                                capture_output=True, timeout=45 if '"action":"collect_telegram"' in payload else 5,
                                creationflags=0x08000000 if os.name=="nt" else 0)
        response = json.loads(result.stdout.strip().splitlines()[-1])
        if not response.get("ok"):
            if '"action":"collect_telegram"' in payload:
                raise ResearchError("channel_unavailable","Публичное веб-превью канала недоступно. Сохранённые публикации оставлены.",503)
            raise ResearchError("terminal_validation", "Исходный модуль STVTerminal отклонил данные")
        return response["data"]
    except (OSError,ValueError,IndexError,subprocess.TimeoutExpired):
        raise ResearchError("terminal_runtime_error", "Не удалось выполнить модуль STVTerminal",503)


def execute(action, **payload):
    value=encode({"action":action,**payload})
    # Network reads must never be memoized by a stale cursor.
    return _execute.__wrapped__(value) if action=="collect_telegram" or len(value)>65536 else _execute(value)
