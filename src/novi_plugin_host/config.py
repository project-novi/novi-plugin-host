from pathlib import Path
from pydantic import BaseModel


class Config(BaseModel):
    server: str = 'unix:/tmp/novi.socket'
    master_key: str
    ipfs_gateway: str = 'http://127.0.0.1:8080'
    plugins_path: Path = Path('plugins')


class PluginConfig(BaseModel):
    name: str
    identifier: str
    description: str = ''
    keywords: list[str] = []
    version: str = '0.1.0'
    license: str | None = None
    homepage: str | None = None

    config_template: str = 'config-template.yaml'

    permissions: set[str] = set()
    requirements: list[str] = []
