from pathlib import Path
import yaml

BASE_DIR = Path(__file__).resolve().parent.parent


def load_config():
    path = BASE_DIR / "config.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    images = raw.setdefault("images", {})
    root = Path(images.get("root_dir", "../ai-relay/outputs/aplus_images_uae"))
    images["root_dir"] = (BASE_DIR / root).resolve() if not root.is_absolute() else root
    return raw
