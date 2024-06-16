import grpc
import yaml

from structlog import get_logger
from pydantic import BaseModel

from novi import Client, Session, SessionMode
from novi.errors import InvalidArgumentError

from . import load_plugins
from .config import Config, PluginMetadata
from .misc import init_log

lg = get_logger('novi_plugin_host')


def init(config: Config):
    config.plugins_path.mkdir(parents=True, exist_ok=True)


with open('config.yaml') as f:
    config = Config.model_validate(yaml.safe_load(f))


plugins = {}
topo = []


class PluginInfo(BaseModel):
    metadata: PluginMetadata
    alive: bool


def list_plugins(session: Session) -> dict[str, PluginInfo]:
    session.check_permission('plugin.list')

    result = {}
    for identifier, state in plugins.items():
        result[identifier] = PluginInfo(
            metadata=state.metadata,
            alive=state.process.is_alive(),
        ).model_dump(mode='json')

    return result


def restart_plugin(plugin: str) -> dict[str, str]:
    session.check_permission('plugin.restart')

    lg.info('restarting plugin', plugin=plugin)

    state = plugins.get(plugin)
    if state is None:
        raise InvalidArgumentError(f'unknown plugin: {plugin}')

    to_restart = {state.identifier}
    q = [state]
    i = 0
    while i < len(q):
        plg = q[i]
        i += 1

        for dep in plg.depended_by:
            if to_restart.add(dep):
                q.append(plugins[dep])

    restarted = []
    for ident in topo[::-1]:
        if ident in to_restart:
            restarted.append(ident)
            plg = plugins[ident]
            plg.process.terminate()
            plg.process.join()

            plg.spawn()

    return {'restarted': restarted}


init_log()
init(config)
with grpc.insecure_channel(
    config.server, options=(('grpc.default_authority', 'localhost'),)
) as channel:
    client = Client(channel)
    identity = client.use_master_key(config.master_key)
    with client.session(SessionMode.IMMEDIATE, identity=identity) as session:
        session.identity = identity

        session.register_function('plugin.list', list_plugins)
        session.register_function('plugin.restart', restart_plugin)

        plugins, topo = load_plugins(config, session)
        try:
            for plugin in plugins.values():
                plugin.process.join()
        except KeyboardInterrupt:
            pass
