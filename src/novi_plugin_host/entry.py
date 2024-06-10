import asyncio
import inspect
import os
import runpy
import sys

from pathlib import Path
from pydantic import BaseModel

from novi.file import set_ipfs_gateway
from novi.plugin import initialize, join

from .misc import init_log

from typing import Optional, Tuple

if sys.version_info < (3, 10):
    from importlib_metadata import EntryPoint
else:
    from importlib.metadata import EntryPoint


class EntryMain(BaseModel):
    entry_point: Optional[Tuple[str, str, str]] = None
    file_path: Optional[str] = None

    def run(self):
        if self.entry_point is not None:
            entry = EntryPoint(*self.entry_point)
            object = entry.load()
            if inspect.iscoroutinefunction(object):
                asyncio.run(object())
            elif inspect.isfunction(object):
                object()
            elif not inspect.ismodule(object):
                raise ValueError('invalid entry point')

        elif self.file_path is not None:
            runpy.run_path(self.file_path)


class EntryConfig(BaseModel):
    identifier: str

    server: str
    identity: str
    config_template: Optional[Path]
    ipfs_gateway: str

    main: EntryMain


if __name__ == '__main__':
    init_log()

    config = EntryConfig.model_validate_json(input())
    if not config.config_template.exists():
        config.config_template = None

    set_ipfs_gateway(config.ipfs_gateway)

    initialize(
        identifier=config.identifier,
        server=config.server,
        identity=config.identity,
        plugin_dir=Path(os.getcwd()),
        config_template=config.config_template,
    )

    try:
        config.main.run()
        join()
    except KeyboardInterrupt:
        pass
