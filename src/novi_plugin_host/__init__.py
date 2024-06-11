import atexit
import json
import multiprocessing as mp
import yaml

from dataclasses import dataclass
from importlib.metadata import entry_points
from importlib.util import find_spec
from itertools import chain
from pathlib import Path
from pydantic import ValidationError
from structlog import get_logger
from toposort import toposort_flatten

from novi import Session

from .config import Config, PluginConfig
from .entry import entry_main

from collections.abc import Iterator
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .entry import EntryMain

lg = get_logger()

plugins = {}


def plugin_user(session: Session, identifier: str, permissions: set[str]):
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


def load_plugin_config(dir: Path, default: dict) -> PluginConfig | None:
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
    default_config: dict[str, Any]


def find_package_plugins() -> Iterator[PluginDesc]:
    from .entry import EntryMain

    entries = entry_points(group='novi.plugin')
    for entry in entries:
        lg.debug('loading plugin from module', entry=entry)

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

        lg.debug('loading plugin from directory', dir=dir)

        config = {
            'name': dir.name,
        }
        yield PluginDesc(
            dir=dir,
            main=EntryMain(file_path=str((dir / 'main.py').resolve())),
            default_config=config,
        )


def load_plugins(config: Config, session: Session) -> list[mp.Process]:
    from .entry import EntryConfig

    plugin_data_dir = Path('data')
    server = config.server

    @dataclass
    class PluginState:
        cwd: Path
        config: PluginConfig
        entry_config: EntryConfig

        def spawn(self):
            lg.info('loading plugin', identifier=self.config.identifier)
            registered = mp.Event()
            child = mp.Process(
                target=entry_main,
                args=(self.entry_config, self.cwd, registered),
            )
            child.start()
            registered.wait()
            atexit.register(child.terminate)
            return child

    plugin_descs = chain(
        find_package_plugins(), find_simple_plugins(config.plugins_path)
    )
    plugins: dict[str, PluginState] = {}
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
            config_template=(
                desc.dir / plugin_config.config_template
            ).resolve(),
            ipfs_gateway=config.ipfs_gateway,
            main=desc.main,
        )

        plugin_dir = plugin_data_dir / identifier
        plugin_dir.mkdir(parents=True, exist_ok=True)

        plugins[identifier] = PluginState(
            plugin_dir, plugin_config, entry_config
        )

    dependencies = {}
    for identifier, state in plugins.items():
        deps = set()
        for req in state.config.requirements:
            if not req.startswith('depends:'):
                lg.warn('unknown requirement', requirement=req)

            dep = req[len('depends:') :]
            if dep not in plugins:
                raise ValueError(f'missing dependency {dep}')

            deps.add(dep)

        dependencies[identifier] = deps

    mp.set_start_method('spawn')
    return [
        plugins[identifier].spawn()
        for identifier in toposort_flatten(dependencies)
    ]
