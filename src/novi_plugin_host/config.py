from pathlib import Path
from pydantic import BaseModel

from typing import List, Optional, Set


class Config(BaseModel):
    server: str = 'unix:/tmp/novi.socket'
    master_key: str
    storage_path: Path = Path('storage')
    plugins_path: Path = Path('plugins')


class PluginConfig(BaseModel):
    name: str
    identifier: str
    description: str = ''
    keywords: List[str] = []
    version: str = '0.1.0'
    license: Optional[str] = None
    homepage: Optional[str] = None

    config_template: str = 'config-template.yaml'

    permissions: Set[str] = set()
