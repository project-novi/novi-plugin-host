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

from .config import Config, PluginConfig, PluginMetadata
from .entry import EntryConfig, entry_main

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


def load_plugin_metadata(dir: Path, default: dict) -> PluginMetadata | None:
    try:
        meta = default
        with (dir / 'plugin.yaml').open() as f:
            meta.update(yaml.safe_load(f))
        return PluginMetadata.model_validate(meta)
    except FileNotFoundError:
        lg.error('plugin config not found')
    except ValidationError:
        lg.exception('invalid plugin config')

    return None


@dataclass
class PluginDesc:
    dir: Path
    main: 'EntryMain'
    default_meta: dict[str, Any]


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
            default_meta=config,
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
            default_meta=config,
        )


class PluginState:
    plugin_dir: Path
    config: PluginConfig
    metadata: PluginMetadata
    entry_config: EntryConfig
    dependencies: set[str]
    depended_by: set[str]

    process: mp.Process | None = None

    def __init__(
        self,
        plugin_dir: Path,
        config: PluginConfig,
        metadata: PluginMetadata,
        entry_config: EntryConfig,
    ):
        self.plugin_dir = plugin_dir
        self.config = config
        self.metadata = metadata
        self.entry_config = entry_config
        self.dependencies = set()
        self.depended_by = set()

    @property
    def identifier(self):
        return self.metadata.identifier

    def spawn(self):
        lg.info('loading plugin', identifier=self.metadata.identifier)
        registered = mp.Event()
        child = mp.Process(
            target=entry_main,
            args=(self.entry_config, self.plugin_dir, registered),
        )
        child.start()
        registered.wait()
        atexit.register(child.terminate)
        self.process = child


def load_plugins(
    config: Config, session: Session
) -> tuple[dict[str, PluginState], list[str]]:
    plugin_data_dir = Path('data')
    server = config.server

    plugin_descs = chain(
        find_package_plugins(), find_simple_plugins(config.plugins_path)
    )
    plugins: dict[str, PluginState] = {}
    for desc in plugin_descs:
        try:
            plugin_meta = desc.default_meta

            with (desc.dir / 'plugin.yaml').open() as f:
                content = yaml.safe_load(f)
                if content is not None:
                    plugin_meta.update(content)

            plugin_meta = PluginMetadata.model_validate(plugin_meta)

        except FileNotFoundError:
            lg.error('plugin metadata not found')
            continue
        except ValidationError:
            lg.exception('invalid plugin metadata')
            continue

        lg.debug('plugin metadata', metadata=plugin_meta.model_dump())

        identifier = plugin_meta.identifier
        if identifier in plugins:
            raise ValueError(f'duplicate plugin identifier: {identifier}')

        plugin_config = config.plugin_config.get(identifier, PluginConfig())
        if plugin_config.disabled:
            lg.info('plugin disabled', identifier=identifier)
            continue

        user = plugin_user(
            session,
            identifier,
            plugin_meta.permissions | plugin_config.extra_permissions,
        )
        if plugin_config.is_admin:
            user.set('@user.role:admin')

        identity = session.login_as(user.id)

        entry_config = EntryConfig(
            identifier=identifier,
            server=server,
            identity=identity.token,
            config_template=(desc.dir / plugin_meta.config_template).resolve(),
            ipfs_gateway=config.ipfs_gateway,
            main=desc.main,
        )

        plugin_dir = plugin_data_dir / identifier
        plugin_dir.mkdir(parents=True, exist_ok=True)

        plugins[identifier] = PluginState(
            plugin_dir, plugin_config, plugin_meta, entry_config
        )

    dependencies = {}
    for identifier, state in plugins.items():
        for req in state.metadata.requirements:
            if not req.startswith('depends:'):
                lg.warn('unknown requirement', requirement=req)

            dep = req[len('depends:') :]
            if dep not in plugins:
                raise ValueError(f'missing dependency {dep}')

            plugins[dep].depended_by.add(identifier)
            state.dependencies.add(dep)

        dependencies[identifier] = state.dependencies

    mp.set_start_method('spawn')

    topo = toposort_flatten(dependencies)

    for identifier in topo:
        plugins[identifier].spawn()

    return plugins, topo
