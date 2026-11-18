"""Read dotenv values as data, with no shell evaluation or interpolation."""

import base64
import json
from urllib.parse import quote


def env_get(path, key):
    try:
        with open(path, "r", encoding="utf-8", newline="\n") as stream:
            for line in stream:
                if line.startswith(key + "="):
                    value = line.split("=", 1)[1].removesuffix("\n").removesuffix("\r")
                    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                        value = value[1:-1]
                    return value
    except FileNotFoundError:
        return None
    return None


def split_csv(value):
    return [item.strip() for item in value.split(",")]


def urlencode(value):
    return quote(value, safe="-._~")


def b64(value):
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def json_string(value):
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c").replace(
        ">", "\\u003e").replace("&", "\\u0026")
