"""CSI dispatch helpers (no pyte)."""


def dispatch_call(fn, params, private=False):
    """CSI ? seqs pass private=True; many pyte handlers only take positional args."""
    if not private:
        return fn(*params)
    try:
        return fn(*params, private=True)
    except TypeError:
        return fn(*params)
