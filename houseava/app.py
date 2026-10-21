"""Wire the pieces into a Router. Used by the CLI now and the satellite hub later."""

from __future__ import annotations

from .brain import Router
from .config import Config
from .lights import Lights
from .llm import Brain
from .memory import Memory
from .persona import system_prompt
from .plugs import Plugs


def build_router(cfg: Config | None = None, announce=None, music=None) -> Router:
    cfg = cfg or Config()
    brain = Brain(cfg)
    lights = Lights(cfg)
    plugs = Plugs(cfg)
    from .config import data_dir

    memory = Memory(data_dir() / "memory.json")

    def persona_system() -> str:
        # fold in any facts that look relevant to the latest user turn
        recent = memory.recent(n=1)
        q = recent[-1]["content"] if recent else ""
        return system_prompt(cfg, memory.recall(q) if q else [])

    return Router(cfg, brain, lights, plugs, memory, persona_system, announce=announce, music=music)
