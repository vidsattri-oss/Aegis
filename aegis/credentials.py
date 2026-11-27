"""Provider secrets kept in process memory or the desktop Secret Service."""
import hashlib
import threading
from contextlib import contextmanager
from functools import lru_cache

from .contracts import require
from .store import encode


@lru_cache(maxsize=1)
def secret_service():
    try:
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import Gio, GLib, Secret
    except (ImportError, ValueError):
        return None
    # Keep the schema name so existing saved credentials remain accessible.
    schema = Secret.Schema.new("org.gyoharness.ProviderCredential", Secret.SchemaFlags.NONE,
                               {"slot": Secret.SchemaAttributeType.STRING})
    return Gio, GLib, Secret, schema


class DesktopKeyring:
    @property
    def available(self):
        return secret_service() is not None

    @contextmanager
    def operation(self):
        api = secret_service()
        require(api is not None, "The system keyring requires the Linux Secret Service bindings")
        gio, glib, secret, schema = api
        cancellable = gio.Cancellable()
        timer = threading.Timer(20, cancellable.cancel)
        timer.daemon = True
        timer.start()
        try:
            yield secret, schema, cancellable
        except glib.Error:
            raise ValueError("The system keyring could not be accessed. Open or unlock it and try again.") from None
        finally:
            timer.cancel()

    def store(self, slot, password):
        with self.operation() as (secret, schema, cancel):
            saved = secret.password_store_sync(schema, {"slot": slot}, secret.COLLECTION_DEFAULT,
                                               "Aegis provider credential", password, cancel)
            require(saved, "The system keyring did not save the provider credential")

    def get(self, slot):
        with self.operation() as (secret, schema, cancel):
            return secret.password_lookup_sync(schema, {"slot": slot}, cancel)

    def forget(self, slot):
        with self.operation() as (secret, schema, cancel):
            attrs = {"slot": slot}
            # Lookup unlocks a matching item if necessary. Clear acts on unlocked items.
            if secret.password_lookup_sync(schema, attrs, cancel) is not None:
                require(secret.password_clear_sync(schema, attrs, cancel), "The keyring item could not be removed")


def validate_key(value):
    require(isinstance(value, str) and len(value) <= 8192 and not any(ch in value for ch in "\r\n\0"),
            "Invalid provider credential")
    return value


class ProviderCredentials:
    def __init__(self, store, backend=None):
        self.store = store
        self.backend = backend if backend is not None else DesktopKeyring()
        self.loaded = {}
        self.lock = threading.RLock()

    def slot(self, provider):
        identity = (self.store.workspace_id(), provider["kind"], provider.get("endpoint"))
        return hashlib.sha256(encode(identity).encode()).hexdigest()

    def status(self, provider):
        slot = self.slot(provider)
        with self.lock:
            remembered = self.store.setting("keyring-" + slot, False)
            return {"has_key": bool(self.loaded.get(slot) or remembered), "credential_saved": remembered,
                    "keyring_available": self.backend.available}

    def get(self, provider):
        slot = self.slot(provider)
        with self.lock:
            key = self.loaded.get(slot)
            if not key and self.store.setting("keyring-" + slot, False):
                key = self.backend.get(slot)
                if not key:
                    self.store.set("keyring-" + slot, False)
                    raise ValueError("The saved credential is missing from the system keyring. Enter it again in Provider settings.")
            self.store.redact.add(key)
            return key

    def update(self, provider, *, key="", remember=False, forget=False, clear=False):
        key = validate_key(key)
        require(not (forget and (remember or key)) and not (clear and (key or remember)),
                "Choose either saving or clearing a credential")
        if key or remember or forget:
            require(provider["kind"] != "demo", "Choose a model provider before setting a credential")
        slot = self.slot(provider)
        marker = "keyring-" + slot
        with self.lock:
            if forget:
                if self.store.setting(marker, False):
                    self.backend.forget(slot)
                    self.store.set(marker, False)
                self.loaded.pop(slot, None)
            elif clear:
                self.loaded.pop(slot, None)
            elif remember:
                key = key or self.get(provider)
                require(key, "Enter or load a credential to remember it")
                self.backend.store(slot, key)
                self.store.set(marker, True)
                self.loaded.pop(slot, None)
            elif key:
                self.loaded[slot] = key
            self.store.redact.add(key)
        return self.status(provider)
