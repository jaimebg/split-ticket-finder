"""bot.post_shutdown: a clean exit on every deploy restart."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import bot as bot_module


async def test_post_shutdown_cancels_the_scheduler_task(monkeypatch):
    """Without this, every restart logs 'Task was destroyed but it is
    pending!' -- once per deploy, now that deploys are automatic."""
    import providers.registry as registry

    async def no_providers():
        return None

    monkeypatch.setattr(registry, "close_all", no_providers)
    task = asyncio.create_task(asyncio.sleep(3600))
    app = SimpleNamespace(bot_data={"scheduler_task": task})

    await bot_module.post_shutdown(app)

    assert task.cancelled()


async def test_post_shutdown_without_a_scheduler_is_fine(monkeypatch):
    import providers.registry as registry

    async def no_providers():
        return None

    monkeypatch.setattr(registry, "close_all", no_providers)
    await bot_module.post_shutdown(SimpleNamespace(bot_data={}))
