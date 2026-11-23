"""A recording stand-in for Streamlit. Pages call it exactly like `st`; tests read what was drawn and script the user's input."""


class _NS:
    def __getattr__(self, name):
        return lambda *a, **k: {"widget": name, "args": a, "kw": k}


class _Ctx:
    """Returned by columns()/tabs()/expander()/spinner(): works as `with x:` and as `x.markdown(...)`."""

    def __init__(self, st):
        self._st = st

    def __enter__(self):
        return self._st

    def __exit__(self, *a):
        return False

    def __getattr__(self, name):
        return getattr(self._st, name)


class FakeSt:
    def __init__(self, inputs=None, press=()):
        self.calls, self.session_state, self.inputs, self.press = [], {}, dict(inputs or {}), set(press)
        self.sidebar, self.column_config = self, _NS()

    # ---- recording helpers for tests
    def args(self, name):
        return [c[1] for c in self.calls if c[0] == name]

    def texts(self, *names):
        names = names or ("title", "subheader", "markdown", "caption", "info", "warning", "error", "success", "write", "header")
        return " ".join(str(a) for c in self.calls if c[0] in names for a in c[1])

    def has(self, name):
        return any(c[0] == name for c in self.calls)

    def labels(self, name="button"):
        return [c[1][0] for c in self.calls if c[0] == name and c[1]]

    # ---- layout
    def _rec(self, name, a, k):
        self.calls.append((name, a, k))

    def columns(self, spec, **k):
        self._rec("columns", (spec,), k)
        return [_Ctx(self) for _ in range(spec if isinstance(spec, int) else len(spec))]

    def tabs(self, labels, **k):
        self._rec("tabs", (labels,), k)
        return [_Ctx(self) for _ in labels]

    def expander(self, *a, **k):
        self._rec("expander", a, k)
        return _Ctx(self)

    def spinner(self, *a, **k):
        self._rec("spinner", a, k)
        return _Ctx(self)

    def container(self, *a, **k):
        return _Ctx(self)

    # ---- inputs (scripted)
    def button(self, label, key=None, **k):
        self._rec("button", (label,), {"key": key, **k})
        return key in self.press or label in self.press

    def multiselect(self, label, options=(), default=None, **k):
        self._rec("multiselect", (label, list(options)), k)
        return self.inputs.get(label, list(default) if default is not None else [])

    def selectbox(self, label, options=(), key=None, **k):
        self._rec("selectbox", (label,), {"key": key})
        return self.inputs.get(key, options[0] if len(options) else None)

    def text_input(self, label, value="", key=None, **k):
        self._rec("text_input", (label,), {"key": key})
        return self.inputs.get(key, value)

    def text_area(self, label, value="", key=None, **k):
        self._rec("text_area", (label,), {"key": key})
        return self.inputs.get(key, value)

    def number_input(self, label, value=0.0, key=None, **k):
        self._rec("number_input", (label,), {"key": key})
        return self.inputs.get(key, value)

    def checkbox(self, label, value=False, key=None, **k):
        self._rec("checkbox", (label,), {"key": key})
        return self.inputs.get(key, value)

    def radio(self, label, options=(), **k):
        self._rec("radio", (label, list(options)), k)
        return self.inputs.get("radio", options[0])

    def file_uploader(self, *a, key=None, **k):
        self._rec("file_uploader", a, {"key": key})
        return self.inputs.get(key)

    def set_page_config(self, **k):
        self._rec("set_page_config", (), k)

    def cache_resource(self, fn):
        return fn

    # ---- everything else (title, markdown, dataframe, metric, progress, json, ...) is just recorded
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def rec(*a, **k):
            self._rec(name, a, k)
        return rec
