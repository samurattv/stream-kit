"""Связь с PRISM/OBS по протоколу obs-websocket 5."""

import threading

class ObsLink:
    """Обёртка над obsws-python: сама переподключается при обрыве."""

    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.client = None
        self.lock = threading.Lock()

    def connect(self):
        import obsws_python as obsws
        self.client = obsws.ReqClient(
            host=self.cfg["ws_host"],
            port=int(self.cfg["ws_port"]),
            password=self.cfg["ws_password"] or "",
            timeout=5,
        )
        return self.client.get_version().obs_version

    def call(self, fn_name, *args):
        with self.lock:
            for attempt in (1, 2):
                try:
                    if self.client is None:
                        self.connect()
                    return getattr(self.client, fn_name)(*args)
                except Exception as e:
                    self.client = None
                    if attempt == 2:
                        raise e

    def scenes(self):
        return [s["sceneName"] for s in self.call("get_scene_list").scenes]
