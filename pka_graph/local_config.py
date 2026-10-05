"""Load the explicitly selected local AI configuration, without copying secrets."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def load_local_ai_config(config_path: Path) -> bool:
    if not config_path.exists():
        return False
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        base = config["api_base"].strip().rstrip("/")
        model = config["model"].strip()
        parsed = urlsplit(base)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or not model):
            raise ValueError()
        key_name = config["key_env_name"]
        matches = []
        for line in Path(config["key_env_file"]).read_text(encoding="utf-8-sig").splitlines():
            name, separator, value = line.strip().partition("=")
            if separator and name.strip() == key_name:
                value = value.strip()
                if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                    value = value[1:-1]
                matches.append(value)
        if len(matches) != 1 or not matches[0]:
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise RuntimeError("本地 AI 配置无效或指定密钥不可读取；请检查 ai.local.json。密钥未输出。") from None

    # This local file explicitly selects all adapters. Other budget variables
    # remain unchanged. No changes are made to the source env file.
    for prefix in ("PKA_QUIZ", "PKA_DISCOVERY", "PKA_NOTE", "PKA_REVIEW"):
        os.environ[prefix + "_ADAPTER"] = "http"
        os.environ[prefix + "_API_BASE"] = base
        os.environ[prefix + "_MODEL"] = model
        os.environ[prefix + "_API_KEY"] = matches[0]
    return True
