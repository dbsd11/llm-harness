import logging
import sys
def setup_logging():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    # console_handler = logging.FileHandler("out.log")
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(filename)s:%(lineno)d - %(levelname)s - %(message)s')
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger

logger = setup_logging()