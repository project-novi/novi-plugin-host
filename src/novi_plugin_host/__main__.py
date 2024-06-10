import grpc
import yaml

from novi import Client

from . import load_plugins
from .config import Config
from .misc import init_log


def init(config: Config):
    config.plugins_path.mkdir(parents=True, exist_ok=True)


with open('config.yaml') as f:
    config = Config.model_validate(yaml.safe_load(f))

init_log()
init(config)
with grpc.insecure_channel(
    config.server, options=(('grpc.default_authority', 'localhost'),)
) as channel:
    client = Client(channel)
    identity = client.use_master_key(config.master_key)
    with client.session(lock=None, identity=identity) as session:
        session.identity = identity

        children = load_plugins(config, session)
        for child in children:
            child.wait()
