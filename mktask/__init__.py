"""mktask — work task prioritizer built on mkio and mkui."""

__version__ = "0.2.0"


def serve(config="mktask.toml", host=None, port=None, db_path=None, user=None):
    from mktask.__main__ import serve as _serve
    _serve(config, host=host, port=port, db_path=db_path, user=user)
