import logging

import requests

import config


def alert(text: str) -> None:
    if not config.SLACK_WEBHOOK_URL:
        logging.warning("SLACK_WEBHOOK_URL unset — alert: %s", text)
        return
    try:
        requests.post(config.SLACK_WEBHOOK_URL, json={"text": f":email: mail: {text}"}, timeout=10)
    except Exception:
        logging.exception("slack alert failed")
