"""Точка входа: py -m streamkit"""

import sys


class _NullStream:
    """Под pythonw.exe stdout и stderr отсутствуют — подставляем заглушку."""

    def write(self, *args):
        pass

    def flush(self):
        pass


def main():
    if sys.stdout is None:
        sys.stdout = _NullStream()
    if sys.stderr is None:
        sys.stderr = _NullStream()

    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass

    from .ui import App
    App().mainloop()


if __name__ == "__main__":
    main()
