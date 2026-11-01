def __getattr__(name):
    # pluto_tx.__version__, computed on first use (see version.py) -- a plain
    # import of the package stays free of side effects.
    if name == "__version__":
        from .version import version
        return version()
    raise AttributeError(name)
