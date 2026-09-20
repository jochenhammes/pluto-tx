"""Persistent MeshCore node identity (Ed25519 seed) of this app, stored per user.

The app is its own, new MeshCore node: it never imports the (64-byte, orlp-format) private key of another
device. The seed file lives in ~/.config/pluto-tx/meshcore_identity.json with mode 0600."""
import json
import os

from .meshcore_codec import Identity

DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".config", "pluto-tx", "meshcore_identity.json")
PATH_ENV = "PLUTO_TX_MESHCORE_IDENTITY"  # override (tests, several identities)


def load_or_create(path: str = None) -> Identity:
    path = path or os.environ.get(PATH_ENV) or DEFAULT_PATH
    try:
        with open(path) as f:
            return Identity(bytes.fromhex(json.load(f)["seed"]))
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        pass
    identity = Identity.generate()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"seed": identity.seed.hex(), "public_key": identity.public_key.hex()}, f)
    return identity
