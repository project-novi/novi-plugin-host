import logging
import structlog
import sys


def init_log():
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.stdlib.LoggerFactory(),
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.contextvars.merge_contextvars,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.TimeStamper(fmt='%Y-%m-%d %H:%M:%S'),
            structlog.dev.ConsoleRenderer(),
        ],
    )
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=logging.INFO,
    )
