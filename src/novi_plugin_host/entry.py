import asyncio
import inspect
import multiprocessing as mp
import os
import runpy

from dataclasses import dataclass
from importlib.metadata import EntryPoint
from pathlib import Path

from novi.plugin import initialize, join, ajoin

from .misc import init_log


@dataclass
class EntryMain:
    entry_point: tuple[str, str, str] | None = None
    file_path: str | None = None

    async def run(self):
        if self.entry_point is not None:
            entry = EntryPoint(*self.entry_point)
            object = entry.load()
            if inspect.iscoroutinefunction(object):
                await object()
            elif inspect.isfunction(object):
                object()
            elif not inspect.ismodule(object):
                raise ValueError('invalid entry point')

        elif self.file_path is not None:
            runpy.run_path(self.file_path)


@dataclass
class EntryConfig:
    identifier: str

    server: str
    identity: str
    config_template: Path | None
    ipfs_gateway: str

    main: EntryMain


def entry_main(config: EntryConfig, plugin_dir: Path, registered: mp.Event):
    os.chdir(str(plugin_dir))
    init_log()

    # config = EntryConfig.model_validate_json(input())
    if not config.config_template.exists():
        config.config_template = None

    initialize(
        identifier=config.identifier,
        server=config.server,
        identity=config.identity,
        plugin_dir=Path(os.getcwd()),
        config_template=config.config_template,
        ipfs_gateway=config.ipfs_gateway,
    )

    asyncio.run(_async_entry_main(config.main, registered))


async def _async_entry_main(main: EntryMain, registered: mp.Event):
    try:
        await main.run()
        registered.set()
        join()
        await ajoin()
    except KeyboardInterrupt:
        pass
