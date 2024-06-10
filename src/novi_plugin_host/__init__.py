import atexit
import json
import subprocess as sp
import sys
import yaml

from dataclasses import dataclass
from importlib.util import find_spec
from itertools import chain
from pathlib import Path
from pydantic import ValidationError
from structlog import get_logger

from novi import Session

from .config import Config, PluginConfig

from typing import Any, Dict, Iterator, List, Optional, Set, TYPE_CHECKING

if sys.version_info < (3, 10):
    from importlib_metadata import entry_points
else:
    from importlib.metadata import entry_points

if TYPE_CHECKING:
    from .entry import EntryMain

lg = get_logger()

plugins = {}


def plugin_user(session: Session, identifier: str, permissions: Set[str]):
    users = session.query(
        f'@user @user.role:plugin @user.name={json.dumps(identifier)}'
    )
    if not users:
        user = session.create_object(
            {
                '@user': None,
                '@user.name': identifier,
                '@user.password': None,
                '@user.role:plugin': None,
            }
        )
    else:
        user = users[0]

    user.replace(
        {f'@user.perm:{perm}': None for perm in permissions},
        scopes={'@user.perm'},
    )

    return user


def load_plugin_config(dir: Path, default: dict) -> Optional[PluginConfig]:
    try:
        config = default
        with (dir / 'plugin.yaml').open() as f:
            config.update(yaml.safe_load(f))
        return PluginConfig.model_validate(config)
    except FileNotFoundError:
        lg.error('plugin config not found')
    except ValidationError:
        lg.exception('invalid plugin config')

    return None


@dataclass
class PluginDesc:
    dir: Path
    main: 'EntryMain'
    default_config: Dict[str, Any]


def find_package_plugins() -> Iterator[PluginDesc]:
    from .entry import EntryMain

    entries = entry_points(group='novi.plugin')
    for entry in entries:
        lg.info('loading plugin from module', entry=entry)

        dir = Path(find_spec(entry.module).origin).parent

        config = {
            'name': entry.name,
            'identifier': f'{entry.module}.{entry.name}',
        }
        if entry.dist:
            meta = entry.dist.metadata
            if 'Description' in meta:
                config['description'] = meta['Description']
            if 'Keywords' in meta:
                config['keywords'] = list(meta['Keywords'].split(','))
            if 'Version' in meta:
                config['version'] = meta['Version']
            if 'License' in meta:
                config['license'] = meta['License']
            if 'Home-page' in meta:
                config['homepage'] = meta['Home-page']

        yield PluginDesc(
            dir=dir,
            main=EntryMain(entry_point=(entry.name, entry.value, entry.group)),
            default_config=config,
        )


def find_simple_plugins(plugin_dir: Path) -> Iterator[PluginDesc]:
    from .entry import EntryMain

    for dir in plugin_dir.iterdir():
        if not dir.is_dir():
            continue
        if any(not (dir / f).is_file() for f in ('main.py', 'plugin.yaml')):
            continue

        lg.info('loading plugin from directory', dir=dir)

        config = {
            'name': dir.name,
            'identifier': 'simple.' + dir.name,
        }
        yield PluginDesc(
            dir=dir,
            main=EntryMain(file_path=str((dir / 'main.py').resolve())),
            default_config=config,
        )


def load_plugins(config: Config, session: Session) -> List[sp.Popen]:
    from .entry import EntryConfig

    plugin_data_dir = Path('data')
    server = config.server

    plugin_descs = chain(
        find_package_plugins(), find_simple_plugins(config.plugins_path)
    )
    children = []
    for desc in plugin_descs:
        try:
            plugin_config = desc.default_config

            with (desc.dir / 'plugin.yaml').open() as f:
                content = yaml.safe_load(f)
                if content is not None:
                    plugin_config.update(content)

            plugin_config = PluginConfig.model_validate(plugin_config)

        except FileNotFoundError:
            lg.error('plugin config not found')
            continue
        except ValidationError:
            lg.exception('invalid plugin config')
            continue

        lg.debug('plugin config', config=plugin_config.model_dump())

        identifier = plugin_config.identifier
        if identifier in plugins:
            raise ValueError(f'duplicate plugin identifier: {identifier}')

        user = plugin_user(session, identifier, plugin_config.permissions)
        identity = session.login_as(user.id)

        entry_config = EntryConfig(
            identifier=identifier,
            server=server,
            identity=identity.token,
            config_template=str(
                (desc.dir / plugin_config.config_template).resolve()
            ),
            ipfs_gateway=config.ipfs_gateway,
            main=desc.main,
        )

        plugin_dir = plugin_data_dir / identifier
        plugin_dir.mkdir(parents=True, exist_ok=True)
        child = sp.Popen(
            [sys.executable, '-m', f'{__name__}.entry'],
            cwd=str(plugin_dir),
            stdin=sp.PIPE,
        )
        child.stdin.write(entry_config.model_dump_json().encode() + b'\n')
        child.stdin.flush()
        atexit.register(child.terminate)
        children.append(child)

    return children
